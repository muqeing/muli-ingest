"""Synthetic camera disconnect/worker handoff/restart acceptance; no live media."""
import json
import os
import threading
import time
from pathlib import Path
import pytest
from muli_ingest import engine as mod, source_release
from muli_ingest.engine import Engine
from muli_ingest.retention import Retention
from muli_ingest.safeio import SafetyError

RCLONE = Path('/Users/zhangxuran/Documents/Codex/2026-09-27/adobe-bridge-chatgpt-conversation-6ab8d169-cc50-2/work/tools/rclone')

@pytest.fixture
def setup(tmp_path, monkeypatch):
    camera = tmp_path/'camera'; camera.mkdir()
    (camera/'one.JPG').write_bytes(b'fixture-photo')
    monkeypatch.setattr(mod,'extract_metadata_batch',lambda items,*a,**k: [{'status':'not_requested'} for _ in items])
    e=Engine(tmp_path/'state',tmp_path/'stage',[camera],rclone=str(RCLONE),mode='demo')
    e.update_settings(1,{'verification_mode':'size_only','metadata_level':'minimal'})
    yield e,camera
    e.close()

def start(e, source=None, background=False):
    sid=(source or e.sources()[0])['source_id']
    scan=e.scan(sid,['.'])
    return e.start(sid,scan['scan_id'],1,os.urandom(12).hex(),background=background)

def until(test, seconds=10):
    limit=time.monotonic()+seconds
    while time.monotonic()<limit:
        value=test()
        if value:return value
        time.sleep(.02)
    pytest.fail('bounded wait expired')

@pytest.mark.parametrize('mode',['size_only','exact'])
def test_disconnect_after_release_is_nas_only_and_not_format_ready(setup,mode):
    e,camera=setup; e.update_settings(e.settings()['version'],{'verification_mode':mode})
    original=e._extract_size_only_metadata
    checks=[]
    def metadata(b,cfg):
        assert e.batch(b['batch_uid'])['source_released'] is True
        assert b['batch_uid'] not in e.active_runs
        camera.rename(camera.with_name('unplugged'))
        e._check=lambda b:pytest.fail('camera accessed after release')
        checks.append(1)
        return original(b,cfg)
    e._extract_size_only_metadata=metadata
    result=start(e)
    assert result['state']=='COMPLETED',result
    assert len(checks)==1 and result['source_coverage']['format_action_available'] is False
    assert result['result']==('COPY_VERIFIED' if mode=='exact' else 'COPY_SIZE_VERIFIED')
    assert (e.staging/result['batch_id']/'ingest_complete.json').exists()


def test_next_sources_keep_copying_while_metadata_blocked(setup):
    e,camera=setup;e._effective_parallel_limit=lambda:1
    entered=threading.Event();release=threading.Event();original=e._extract_size_only_metadata
    def slow(b,cfg):
        entered.set(); assert release.wait(10); return original(b,cfg)
    e._extract_size_only_metadata=slow
    try:
        first=start(e,background=True);assert entered.wait(5)
        assert not e.active_runs and not e.futures[first['batch_uid']].done()
        # More than the source executor's four workers. Queued NAS work must
        # never consume those workers or their concurrency slots.
        ids=[]
        for i in range(5):
            next_camera=camera.parent/f'next-{i}';next_camera.mkdir();(next_camera/'next.JPG').write_bytes(bytes([i]))
            e._explicit_paths.append(next_camera);e._explicit_labels[str(next_camera)]=None;e._explicit_uuids[str(next_camera)]=None
            e.refresh_sources();source=next(x for x in e.sources() if x['path']==str(next_camera))
            other=start(e,source,True);ids.append(other['batch_uid'])
            until(lambda:e.batch(other['batch_uid']).get('source_released'))
        assert not e.active_runs
        for uid in [first['batch_uid'],*ids]:
            b=e.batch(uid)
            assert b['state']=='FINALIZING' and b['completed_at'] is None
            assert not (e.staging/b['batch_id']/'ingest_complete.json').exists()
            with pytest.raises(SafetyError,match='batch_not_completed'):Retention(e).handoff(uid,True)
            with pytest.raises(SafetyError):e.resume(uid)
    finally:release.set()
    for future in e.futures.values():future.result(timeout=10)
    assert all(e.batch(uid)['state']=='COMPLETED' for uid in ids)


def test_metadata_failure_resume_needs_no_original_device(setup):
    e,camera=setup;original=e._extract_size_only_metadata
    def fail(b,cfg):raise RuntimeError('metadata interrupted')
    e._extract_size_only_metadata=fail
    bad=start(e);assert bad['state']=='FAILED' and bad['source_released'] is True
    camera.rename(camera.with_name('unplugged'))
    e._extract_size_only_metadata=original
    e.refresh_sources=lambda:pytest.fail('must not rediscover camera for resume')
    e._check=lambda b:pytest.fail('must not access camera')
    good=e.resume(bad['batch_uid'],background=False)
    assert good['state']=='COMPLETED',good
    assert good['source_session_id']==bad['source_session_id']


def test_restart_resumes_committed_background_from_nas(setup):
    e,camera=setup
    e._extract_size_only_metadata=lambda *a:(_ for _ in ()).throw(RuntimeError('simulate metadata interruption'))
    b=start(e);b['state']='FINALIZING';b['error']=None;e._save(b)
    e.close();camera.rename(camera.with_name('unplugged'))
    new=Engine(e.state,e.staging,[camera],rclone=str(RCLONE),mode='demo')
    try:
        new.futures[b['batch_uid']].result(timeout=10)
        assert new.batch(b['batch_uid'])['state']=='COMPLETED',new.batch(b['batch_uid'])
    finally:new.close()


def test_incomplete_copy_does_not_release(setup):
    e,_=setup
    e.copier.copy_and_hash=lambda *a:(_ for _ in ()).throw(SafetyError('source_unavailable'))
    b=start(e)
    assert b['state']=='INTERRUPTED' and not b.get('source_released')
    assert not (e.staging/b['batch_id']/source_release.NAME).exists()


def test_pre_release_source_change_blocks_release(setup,monkeypatch):
    e,camera=setup;original=e._one_file
    def altered(b,f,root,cfg):
        original(b,f,root,cfg);(camera/'one.JPG').write_bytes(b'changed camera')
    e._one_file=altered;b=start(e)
    assert b['state']=='FAILED' and not b.get('source_released') and 'source_changed' in b['error']

@pytest.mark.parametrize('change',['marker','row','target','hardlink','symlink'])
def test_invalid_resume_evidence_never_completes(setup,change):
    e,camera=setup;original=e._extract_size_only_metadata
    e._extract_size_only_metadata=lambda *a:(_ for _ in ()).throw(RuntimeError('stop'))
    b=start(e);root=e.staging/b['batch_id'];target=root/'SOURCE_DATA/one.JPG'
    if change=='marker':(root/source_release.NAME).write_text('{}')
    elif change=='row':
        row=e.store.files(b['batch_uid'])[0];row['source_hash']='0'*64;e.store.put_file(b['batch_uid'],b['source_id'],row)
    elif change=='target':target.write_bytes(b'bad')
    elif change=='hardlink':os.link(target,root/'alias')
    elif change=='symlink':target.unlink();target.symlink_to(camera/'one.JPG')
    e._extract_size_only_metadata=original
    result=e.resume(b['batch_uid'],background=False)
    assert result['state']=='FAILED' and not (root/'ingest_complete.json').exists(),result


def test_unknown_release_db_commit_outcome_never_replays_copy(setup,monkeypatch):
    e,_=setup;original=e._save
    def save(b):
        if b.get('source_released'):raise OSError('simulated commit error')
        return original(b)
    e._save=save;b=start(e)
    assert not b.get('source_released') and (e.staging/b['batch_id']/source_release.NAME).exists()
    e._save=original;e._one_file=lambda *a:pytest.fail('ambiguous release replayed copy')
    result=e.resume(b['batch_uid'],background=False)
    assert result['state']=='FAILED' and 'existing_source_release_requires_review' in result['error']


def test_existing_completed_receipt_is_preserved(setup):
    e,_=setup;b=start(e);root=e.staging/b['batch_id'];before=(root/'ingest_complete.json').read_bytes()
    b['state']='FAILED';e._save(b)
    result=e.resume(b['batch_uid'],background=False)
    assert result['state']=='FAILED' and 'existing_receipt_requires_review' in result['error']
    assert (root/'ingest_complete.json').read_bytes()==before


def test_background_chunk_yields_to_active_copy(setup):
    e,_=setup;entered=threading.Event();continue_=threading.Event();original=e._extract_size_only_metadata
    def pause(b,cfg):entered.set();continue_.wait(5);return original(b,cfg)
    e._extract_size_only_metadata=pause
    b=start(e,background=True);assert entered.wait(5)
    e.active_runs.add('synthetic-new-source');continue_.set()
    time.sleep(.15)
    assert e.batch(b['batch_uid'])['state']=='FINALIZING'
    e._release_run_slot('synthetic-new-source')
    e.futures[b['batch_uid']].result(timeout=5)
    assert e.batch(b['batch_uid'])['state']=='COMPLETED'

@pytest.mark.parametrize('change',['unchanged','ctime','restored_mtime_tamper'])
def test_no_content_reread_except_attribute_change(setup,monkeypatch,change):
    e,_=setup;old_post=e._postprocess;calls=[];original=source_release.digest_fd
    def counted(*args,**kwargs):calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(source_release,'digest_fd',counted)
    def post(uid):
        b=e.batch(uid);target=e.staging/b['batch_id']/'SOURCE_DATA/one.JPG';st=target.stat()
        if change=='ctime':os.chmod(target,0o600)
        elif change=='restored_mtime_tamper':
            target.write_bytes(b'x'*st.st_size);os.utime(target,ns=(st.st_atime_ns,st.st_mtime_ns))
        return old_post(uid)
    e._postprocess=post;b=start(e)
    assert len(calls)==(0 if change=='unchanged' else 1)
    assert b['state']==('FAILED' if change=='restored_mtime_tamper' else 'COMPLETED'),b


def test_same_unreleased_source_remains_busy(setup):
    e,_=setup;entered=threading.Event();release=threading.Event();original=e._one_file
    def slow(*args):entered.set();assert release.wait(5);return original(*args)
    e._one_file=slow
    try:
        b=start(e,background=True);assert entered.wait(5)
        with pytest.raises(SafetyError,match='source_busy'):start(e)
    finally:release.set()
    e.futures[b['batch_uid']].result(timeout=5)


def test_reused_incremental_copy_does_not_rehash_during_metadata(setup,monkeypatch):
    e,_=setup;e.update_settings(e.settings()['version'],{'verification_mode':'exact'})
    first=start(e);assert first['state']=='COMPLETED'
    monkeypatch.setattr(source_release,'digest_fd',lambda *a,**k:pytest.fail('incremental target already verified before release'))
    second=start(e)
    assert second['state']=='COMPLETED',second
    assert second['summary']['previously_ingested_count']==1


def test_mutation_during_metadata_cannot_emit_success(setup):
    e,_=setup;original=e._extract_size_only_metadata
    def mutate(b,cfg):
        result=original(b,cfg)
        target=e.staging/b['batch_id']/'SOURCE_DATA/one.JPG';st=target.stat()
        target.write_bytes(b'x'*st.st_size);os.utime(target,ns=(st.st_atime_ns,st.st_mtime_ns))
        return result
    e._extract_size_only_metadata=mutate;b=start(e)
    assert b['state']=='FAILED' and 'released_target_changed' in b['error']
    assert not (e.staging/b['batch_id']/'ingest_complete.json').exists()


def test_same_source_can_start_again_after_release(setup):
    e,_=setup;entered=threading.Event();release=threading.Event();original=e._extract_size_only_metadata
    def blocked(b,cfg):entered.set();assert release.wait(5);return original(b,cfg)
    e._extract_size_only_metadata=blocked
    try:
        first=start(e,background=True);assert entered.wait(5)
        second=start(e,background=True)
        until(lambda:e.batch(second['batch_uid']).get('source_released'))
        assert first['batch_uid']!=second['batch_uid']
    finally:release.set()
    for f in e.futures.values():f.result(timeout=5)
