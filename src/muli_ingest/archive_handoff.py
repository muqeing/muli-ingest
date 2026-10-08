"""Consume trusted read-only archive outbox without holding copy locks for I/O."""
from copy import deepcopy
from pathlib import Path
import re
import threading
import time
from blake3 import blake3
from . import handoff_protocol as wire


# Camera system and index files live on every card but can never be archived:
# the archive accepts an auxiliary file only together with its main file, and
# these have no main file. Counting them as "missing" kept a fully archived
# batch at "partial" forever, so they are reported separately instead.
SYSTEM_ARTIFACT = re.compile(
    r'^(?:AVF_INFO|ALBM)/'
    r'|^MISC/[^/]+\.db$'
    r'|^(?:PRIVATE/)?(?:SONY/SONYCARD\.IND|M4ROOT/MEDIAPRO\.XML)$'
    r'|^PRIVATE/DATABASE/DATABASE\.BIN$'
)


def system_artifact(relative_path):
    """True for card metadata that carries no deliverable material."""
    return bool(SYSTEM_ARTIFACT.match(str(relative_path)))


def current_view(batch, clock=None):
    clock = time.time() if clock is None else clock
    view = deepcopy(batch.get('archive_handoff') or {'status':'pending','confirmed_file_count':0,
        'total_file_count':batch.get('summary',{}).get('selected_file_count',0),
        'missing_file_count':batch.get('summary',{}).get('selected_file_count',0),
        'checked_at':None,'expires_at':None,'reason':None,'source_disposition':'unknown','jobs':[]})
    try:
        valid = (view.get('revision')==batch['revision'] and batch['state']=='COMPLETED'
                 and view['total_file_count']>0 and wire.timestamp(view['expires_at'])>clock)
    except (KeyError,ValueError,TypeError):
        valid = False
    if view['status'] in ('confirmed','partial') and not valid:
        view.update(status='needs_review',reason='转存凭据已过期或批次版本变化，等待后台重新核对')
    view.setdefault('card_message','当前卡需重新连接并核对；本功能不会格式化卡')
    view.setdefault('cleanup_message','清理还需检查保留期、保护锁、其他批次引用及当前文件状态')
    return view


class AcknowledgementPending(OSError):
    pass


class StaleSequence(ValueError):
    pass


class FeedbackDeferred(Exception):
    pass


class IngestHandoff:
    def __init__(self, engine, outbox, acknowledgements):
        self.e=engine;self.outbox=Path(outbox);self.acks=Path(acknowledgements)
        wire.separate_channels((self.outbox,self.acks),(engine.staging,))
        self.identities=[wire.identity(p) for p in (self.outbox,self.acks)]
        self.stop=threading.Event();self.thread=None;self.tokens={}
        self.record_tokens={}
        self.apply_lock=threading.RLock()

    def roots(self):
        if [wire.identity(p) for p in (self.outbox,self.acks)]!=self.identities:
            raise ValueError('handoff_directory_replaced')

    def read_record(self,name):
        p=self.e.staging/name;token=wire.signature(p.lstat())
        raw=wire.read_raw(self.e.staging,name)
        if wire.signature(p.lstat())!=token:
            raise ValueError('manifest_changed_during_read')
        self.record_tokens[p]=token
        return raw

    def manifest(self, binding):
        bid=binding['batch_id'];uid=binding['batch_uid']
        batch=self.e.batch(uid)
        if (batch['state']!='COMPLETED' or batch['batch_id']!=bid or batch['revision']!=binding['revision']
                or batch['result'] not in ('COPY_VERIFIED','COPY_SIZE_VERIFIED')):
            raise ValueError('batch_version_changed')
        raw=self.read_record(bid+'/ingest_manifest.json')
        complete=self.read_record(bid+'/ingest_complete.json')
        m=wire.decode(raw);c=wire.decode(complete)
        if blake3(raw).hexdigest()!=binding['manifest_blake3'] or blake3(complete).hexdigest()!=binding['completion_blake3']:
            raise ValueError('manifest_digest_changed')
        if (m.get('schema_version')!='1.1' or c.get('algorithm')!='blake3' or c.get('json_blake3')!=binding['manifest_blake3']
                or any(m[k]!=binding[k] or c[k]!=binding[k] for k in ('manifest_id','revision'))
                or c['batch_uid']!=uid or m['batch']['batch_uid']!=uid or m['batch']['batch_id']!=bid
                or m['batch']['state']!='COMPLETED' or m['batch']['result']!=batch['result']
                or m['example_data'] is not (self.e.mode=='demo') or c['example_data'] is not m['example_data']
                or m['scan_errors'] or m['summary']['failed_file_count'] or m['summary']['pending_file_count']):
            raise ValueError('invalid_ingest_completion')
        if blake3(self.read_record(bid+'/ingest_manifest.md')).hexdigest()!=c['md_blake3']:
            raise ValueError('manifest_markdown_changed')
        files=m['files']
        if len(files)!=m['summary']['selected_file_count'] or not 0<len(files)<=100000:
            raise ValueError('empty_or_invalid_scope')
        expected={};paths=set();seen_ids=set();total=0;owners_by_uid={};ignored=[]
        for f in files:
            if self.stop.is_set():raise FeedbackDeferred()
            wire.relative(f['relative_path'])
            if f['file_id'] in seen_ids or f['relative_path'] in paths or not wire.HEX.fullmatch(f['hash']['source']) or f.get('error'):
                raise ValueError('invalid_file_set')
            if not f['hash'].get('readback_verified_at') or type(f['size_bytes']) is not int or f['size_bytes']<0 or f['hash'].get('algorithm')!='blake3':
                raise ValueError('invalid_file_evidence')
            paths.add(f['relative_path']);seen_ids.add(f['file_id']);total+=f['size_bytes']
            physical=bid+'/'+str(f.get('destination_relative_path'))
            if f['copy_status']=='skipped_existing':
                ref=f['existing_copy']
                if ref['batch_uid'] not in owners_by_uid:
                    owner=self.e.batch(ref['batch_uid'])
                    owner_raw=self.read_record(owner['batch_id']+'/ingest_manifest.json')
                    owner_manifest=wire.decode(owner_raw)
                    owner_complete=wire.decode(self.read_record(owner['batch_id']+'/ingest_complete.json'))
                    if (owner['state']!='COMPLETED' or owner_manifest['batch']['batch_uid']!=owner['batch_uid']
                            or owner_manifest['batch']['batch_id']!=owner['batch_id']
                            or owner_manifest['revision']!=owner['revision']
                            or owner['result']!='COPY_VERIFIED' or owner_complete['batch_uid']!=owner['batch_uid']
                            or owner_complete['revision']!=owner['revision']
                            or owner_complete['json_blake3']!=blake3(owner_raw).hexdigest()
                            or owner_complete['manifest_id']!=owner_manifest['manifest_id']):
                        raise ValueError('incremental_receipt_changed')
                    index={}
                    for row in owner_manifest['files']:
                        if row.get('destination_relative_path') and row['copy_status']=='verified':
                            index.setdefault(owner['batch_id']+'/'+row['destination_relative_path'],[]).append(row)
                    owners_by_uid[ref['batch_uid']]=(owner,owner_manifest,index)
                owner,owner_manifest,index=owners_by_uid[ref['batch_uid']]
                if owner['batch_id']!=ref['batch_id'] or ref['manifest_id']!=f"{owner['batch_uid']}-r{owner['revision']}" or owner_manifest['manifest_id']!=ref['manifest_id']:
                    raise ValueError('incremental_owner_changed')
                physical=ref['staging_relative_path']
                owners=index.get(physical,[])
                if len(owners)!=1 or owners[0]['hash']['source']!=f['hash']['source'] or owners[0]['size_bytes']!=f['size_bytes'] or owners[0]['hash'].get('destination')!=f['hash']['source'] or owners[0].get('hash_match') is not True or f['hash']['existing_destination']!=f['hash']['source']:
                    raise ValueError('incremental_owner_mismatch')
            elif f['copy_status']=='verified':
                if f['hash']['source']!=f['hash']['destination'] or f['hash_match'] is not True:
                    raise ValueError('unverified_ingest_file')
            elif f['copy_status']=='size_verified':
                if batch['result']!='COPY_SIZE_VERIFIED':
                    raise ValueError('invalid_size_only_file')
                # These rows only become covered by the archive producer's
                # full-content-bound receipt; batch size alone never confirms.
            else:
                raise ValueError('incomplete_ingest_file')
            wire.relative(physical)
            if system_artifact(f['relative_path']):
                ignored.append(f['relative_path'])
                continue
            expected[f['file_id']]={'file_id':f['file_id'],'relative_path':f['relative_path'],
                'size_bytes':f['size_bytes'],'original_blake3':f['hash']['source'],'physical_source_path':physical}
        if total!=m['summary']['selected_bytes']:
            raise ValueError('manifest_total_mismatch')
        return batch,expected,m,ignored

    @staticmethod
    def kind(uid):
        return 'archive_handoff_job:'+uid

    def aggregate(self,batch,binding,expected,incoming,clock,manifest,ignored=()):
        records={r['job_id']:r for r in self.e.store.all(self.kind(batch['batch_uid']))}
        records[incoming['job_id']]=incoming
        covered={};conflict=False;expires=[];jobs=[];had_issue=False
        for value in records.values():
            if value['binding']!=binding or value['status']!='verified' or wire.timestamp(value['expires_at'])<=clock:
                had_issue=True;continue
            expires.append(wire.timestamp(value['expires_at']))
            for row in value['files']:
                original=expected.get(row['file_id'])
                if original is None or any(row.get(k)!=v for k,v in original.items()):
                    conflict=True;continue
                old=covered.get(row['file_id'])
                if old and any(old[k]!=row[k] for k in ('target_path','archived_blake3','target_signature')):
                    conflict=True
                covered[row['file_id']]=row
            for project in sorted({r['project_name'] for r in value['files']}):
                jobs.append({'job_id':value['job_id'],'project_name':project})
        count=len(covered);total=len(expected)
        status='confirmed' if count==total and not conflict else ('needs_review' if conflict or had_issue else 'partial' if count else 'pending')
        modes={r['mode'] for r in covered.values()}
        disposition='moved' if modes=={'move'} and count==total else 'partly_moved' if 'move' in modes else 'retained' if modes else 'unknown'
        card='本次卡快照已完整备份并转存；当前卡需重新连接核对，不能直接据此格式化' if status=='confirmed' and batch['result']=='COPY_VERIFIED' and manifest.get('source_coverage',{}).get('status')=='FULLY_INGESTED' else '当前卡需重新连接并核对整卡备份范围；本功能不会格式化卡'
        return {'status':status,'revision':batch['revision'],'confirmed_file_count':count,'total_file_count':total,
                'missing_file_count':total-count,'ignored_file_count':len(list(ignored)),
                'ignored_files':list(ignored)[:50],
                'checked_at':wire.iso(clock),'expires_at':wire.iso(min(expires)) if expires else None,
                'reason':'存在过期、失效或冲突的归档凭据' if status=='needs_review' else None,
                'source_disposition':disposition,'jobs':jobs,'card_message':card,
                'cleanup_message':'已有来源被归档移出，不再进入旧中转清理流程' if 'move' in modes else '中转副本保留；清理仍受保留期、保护锁、引用及当前文件核验限制'}

    def apply(self,event,clock=None):
        # Serialize feedback only; the copy pool and Engine lock stay free.
        with self.apply_lock:
            return self._apply(event,clock)

    def _apply(self,event,clock=None):
        clock=time.time() if clock is None else clock
        self.roots();wire.validate_event(event,clock)
        if event['example_data'] is not (self.e.mode=='demo') or event['source_identity']!=wire.identity(self.e.staging):
            raise ValueError('source_or_mode_mismatch')
        self.record_tokens={}
        previous=self.e.store.get('archive_handoff_seen',event['job_id'])
        if previous:
            if event['sequence']<previous['sequence']:
                raise StaleSequence('out_of_order_event')
            if event['sequence']==previous['sequence']:
                if previous['event_id']!=event['event_id']:
                    raise ValueError('sequence_conflict')
                for part in event['batches']:
                    binding=part['binding'];batch=self.e.batch(binding['batch_uid'])
                    if batch['revision']!=binding['revision'] or batch['state']!='COMPLETED':
                        raise ValueError('batch_version_changed')
                self.acknowledge(event['job_id'],previous['ack'])
                return previous['ack']
        prepared=[]
        # All manifest and filesystem reads are outside the Engine/DB locks.
        for part in event['batches']:
            binding=part['binding'];batch,expected,m,ignored=self.manifest(binding)
            for row in part['files']:
                if row['file_id'] not in expected or any(row[k]!=v for k,v in expected[row['file_id']].items()):
                    raise ValueError('file_not_in_batch')
            value={k:event[k] for k in ('job_id','event_id','sequence','status','checked_at','expires_at')}
            value.update(binding=binding,files=part['files'])
            projection=self.aggregate(batch,binding,expected,value,clock,m,ignored)
            prepared.append((batch,binding,value,projection))
        ack={'schema':'muli-archive-handoff-ack/1','job_id':event['job_id'],'event_id':event['event_id'],
             'checked_at':wire.iso(clock),'expires_at':event['expires_at'],'status':'needs_review' if any(p[3]['status']=='needs_review' for p in prepared) else 'synced',
             'batches':[{'batch_id':b['batch_id'],'batch_uid':b['batch_uid'],'status':p['status'],'confirmed_file_count':p['confirmed_file_count'],'total_file_count':p['total_file_count'],
                        'missing_file_count':p['missing_file_count'],'ignored_file_count':p['ignored_file_count']} for b,_,_,p in prepared]}
        self.roots()
        if any(wire.signature(p.lstat())!=token for p,token in self.record_tokens.items()):
            raise ValueError('manifest_changed_before_commit')
        with self.e.lock, self.e.store.transaction():
            # CAS recheck prevents a late event from confirming a revised batch.
            for old,binding,value,projection in prepared:
                fresh=self.e.batch(old['batch_uid'])
                if any(fresh.get(k)!=old.get(k) for k in ('batch_id','batch_uid','revision','state','result','completed_at')):
                    raise ValueError('batch_changed_before_commit')
                fresh['archive_handoff']=projection
                life=fresh['lifecycle'];confirmed=projection['status']=='confirmed'
                life['archive_handoff_confirmed']=confirmed
                life['archive_handoff_expires_at']=projection['expires_at']
                life['archive_sources_moved']=bool(life.get('archive_sources_moved') or projection['source_disposition'] in ('moved','partly_moved'))
                if life.get('handoff_evidence')!='manual_confirmation':
                    life.update(handoff_confirmed=confirmed,handoff_evidence='archive_receipt',handoff_at=wire.iso(clock))
                self.e._save(fresh);self.e.store.put(self.kind(old['batch_uid']),event['job_id'],value)
                self.e.store.event(old['batch_uid'],{'type':'archive_handoff_checked','time':wire.iso(clock),'job_id':event['job_id'],'event_id':event['event_id'],'status':projection['status'],'covered':projection['confirmed_file_count'],'total':projection['total_file_count']})
            self.e.store.put('archive_handoff_seen',event['job_id'],{'sequence':event['sequence'],'event_id':event['event_id'],'ack':ack})
        # If this fails, retry reads the committed result and only republishes
        # the acknowledgement. No duplicate lifecycle or audit mutation.
        self.acknowledge(event['job_id'],ack)
        return ack

    def acknowledge(self,jid,ack):
        try:
            self.roots();wire.write(self.acks,jid+'.json',ack)
        except OSError as error:
            raise AcknowledgementPending('acknowledgement_pending') from error

    def expire(self,clock):
        for batch in self.e.batches():
            view=current_view(batch,clock)
            if batch.get('archive_handoff',{}).get('status')!='confirmed' or view['status']=='confirmed':
                continue
            with self.e.lock,self.e.store.transaction():
                fresh=self.e.batch(batch['batch_uid'])
                if fresh.get('archive_handoff')!=batch.get('archive_handoff'):
                    continue
                fresh['archive_handoff']=view;fresh['lifecycle']['archive_handoff_confirmed']=False
                if fresh['lifecycle'].get('handoff_evidence')=='archive_receipt':
                    fresh['lifecycle']['handoff_confirmed']=False
                self.e._save(fresh)
                self.e.store.event(fresh['batch_uid'],{'type':'archive_handoff_expired','time':wire.iso(clock)})

    def invalidate(self, jid, clock):
        previous=self.e.store.get('archive_handoff_seen',jid)
        if previous is None:
            return
        with self.e.lock,self.e.store.transaction():
            for item in previous['ack']['batches']:
                batch=self.e.batch(item['batch_uid'])
                value=self.e.store.get(self.kind(item['batch_uid']),jid)
                if value:
                    value.update(status='needs_review',files=[])
                    self.e.store.put(self.kind(item['batch_uid']),jid,value)
                view=batch.get('archive_handoff')
                if view and view['status']!='needs_review':
                    view.update(status='needs_review',reason='交接凭据未通过核对',checked_at=wire.iso(clock))
                    batch['lifecycle']['archive_handoff_confirmed']=False
                    if batch['lifecycle'].get('handoff_evidence')=='archive_receipt':
                        batch['lifecycle']['handoff_confirmed']=False
                    self.e._save(batch)
                    self.e.store.event(batch['batch_uid'],{'type':'archive_handoff_rejected','time':wire.iso(clock),'job_id':jid})

    def tick(self,clock=None):
        clock=time.time() if clock is None else clock
        self.roots();self.expire(clock)
        for p in sorted(self.outbox.glob('*.json')):
            if not wire.HEX.fullmatch(p.stem):
                continue
            token=wire.signature(p.lstat())
            if self.tokens.get(p.name)==token:
                continue
            try:
                event=wire.read(self.outbox,p.name)
                if event.get('job_id')!=p.stem:
                    raise ValueError('event_filename_mismatch')
                self.apply(event,clock)
            except StaleSequence:
                self.tokens[p.name]=token
                continue
            except AcknowledgementPending:
                # DB commit already succeeded. Retry only the missing ack.
                continue
            except FeedbackDeferred:
                return
            except (OSError,ValueError,KeyError,TypeError):
                self.invalidate(p.stem,clock)
                # A bad event must not starve the other independent batches.
                # Changed bytes trigger a new attempt; no mutation replay loop.
                self.tokens[p.name]=token
                continue
            self.tokens[p.name]=token
            break

    def start(self):
        def loop():
            while not self.stop.wait(10):
                try:self.tick()
                except (OSError,ValueError,KeyError,TypeError):pass
        self.thread=threading.Thread(target=loop,name='ingest-archive-handoff',daemon=True);self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():raise RuntimeError('handoff_worker_still_running')
