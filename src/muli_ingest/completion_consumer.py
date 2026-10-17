"""Consume explicit permanent facts without granting live media permissions."""
from copy import deepcopy
from . import handoff_protocol as wire
from . import completion_facts as facts


def apply(worker, fact, clock):
    worker.roots()
    event = facts.validate(fact)
    if wire.timestamp(fact['published_at']) > clock + 5:
        raise ValueError('completion_from_future')
    if (event['example_data'] is not (worker.e.mode == 'demo')
            or event['source_identity'] != wire.identity(worker.e.staging)):
        raise ValueError('completion_source_or_mode_changed')
    jid = event['job_id']
    previous = worker.e.store.get('archive_completion_seen', jid)
    if previous:
        if previous['fact_id'] != fact['fact_id']:
            raise ValueError('completion_fact_conflict')
        for item in previous['ack']['batches']:
            batch = worker.e.batch(item['batch_uid'])
            if (batch['batch_id'] != item['batch_id'] or batch['revision'] != item['revision']
                    or batch['state'] != 'COMPLETED'
                    or batch['result'] not in ('COPY_VERIFIED', 'COPY_SIZE_VERIFIED')):
                raise ValueError('completion_batch_changed')
        acknowledge(worker, jid, previous['ack'])
        return previous['ack']
    worker.record_tokens = {}
    prepared = []
    for part in event['batches']:
        binding = part['binding']
        batch, expected, manifest, ignored = worker.manifest(binding)
        incoming = {'schema': facts.SCHEMA, 'job_id': jid, 'fact_id': fact['fact_id'],
                    'binding': binding, 'files': part['files']}
        kind = 'archive_completion_job:' + batch['batch_uid']
        records = {r['job_id']: r for r in worker.e.store.all(kind)}
        if jid in records and records[jid] != incoming:
            raise ValueError('completion_job_conflict')
        records[jid] = incoming
        result = facts.projection(batch, list(records.values()), binding, expected,
                                  checked_at=wire.iso(clock), ignored=ignored)
        prepared.append((batch, kind, incoming, result))
    ack = {'schema': facts.ACK, 'job_id': jid, 'fact_id': fact['fact_id'],
           'status': 'recorded', 'historical_only': True, 'operation_authorized': False,
           'checked_at': wire.iso(clock),
           'batches': [{k: value[k] for k in ('batch_id', 'batch_uid', 'revision', 'status',
                       'confirmed_file_count', 'total_file_count', 'missing_file_count',
                       'ignored_file_count')} for _, _, _, value in prepared]}
    worker.roots()
    if any(wire.signature(p.lstat()) != token for p, token in worker.record_tokens.items()):
        raise ValueError('completion_manifest_changed_before_commit')
    with worker.e.lock, worker.e.store.transaction():
        for old, kind, record, projection in prepared:
            fresh = worker.e.batch(old['batch_uid'])
            if any(fresh.get(k) != old.get(k) for k in
                   ('batch_id', 'batch_uid', 'revision', 'state', 'result', 'completed_at')):
                raise ValueError('completion_batch_changed_before_commit')
            fresh['archive_completion'] = projection
            life = fresh['lifecycle']
            life['archive_completion_recorded'] = projection['status'] == 'confirmed'
            if projection['source_disposition'] in ('moved', 'partly_moved'):
                life['archive_sources_moved'] = True  # A protection, never deletion permission.
            # Do not set handoff_confirmed/archive_handoff_confirmed or extend
            # any old lease. Historical facts have no current cleanup authority.
            worker.e._save(fresh)
            worker.e.store.put(kind, jid, record)
            worker.e.store.event(old['batch_uid'], {'type': 'archive_completion_recorded',
                'time': wire.iso(clock), 'job_id': jid, 'fact_id': fact['fact_id'],
                'covered': projection['confirmed_file_count'], 'total': projection['total_file_count']})
        worker.e.store.put('archive_completion_seen', jid,
                           {'fact_id': fact['fact_id'], 'ack': ack})
    acknowledge(worker, jid, ack)
    return ack


def acknowledge(worker, jid, ack):
    from .archive_handoff import AcknowledgementPending
    try:
        worker.roots()
        wire.write(worker.acks, jid + '.completion.json', ack)
    except OSError as exc:
        raise AcknowledgementPending('completion_acknowledgement_pending') from exc


def tick(worker, clock):
    from .archive_handoff import AcknowledgementPending, FeedbackDeferred
    worker.roots()
    pending = worker.outbox / 'completion-pending'
    if wire.identity(pending) != worker.completion_pending_identity:
        raise ValueError('completion_pending_directory_replaced')
    # Only unresolved controls are listed. Confirmed history is never stat'ed.
    paths = sorted(pending.glob('*.json'))
    active = {p.name for p in paths}
    for name in list(worker.tokens):
        if name not in active:
            worker.tokens.pop(name, None)
    for path in paths:
        if not wire.HEX.fullmatch(path.stem):
            raise ValueError('invalid_completion_pending_name')
        token = wire.signature(path.lstat())
        if worker.tokens.get(path.name) == token:
            continue
        marker = wire.read(pending, path.name)
        if (marker.get('schema') != 'completion-pending/1'
                or marker.get('job_id') != path.stem
                or not wire.HEX.fullmatch(marker.get('fact_id', ''))):
            raise ValueError('invalid_completion_pending_marker')
        try:
            fact = wire.read(worker.outbox, path.stem + '.completion.json')
            if fact.get('fact_id') != marker['fact_id']:
                raise ValueError('completion_pending_fact_changed')
            apply(worker, fact, clock)
        except AcknowledgementPending:
            return  # Commit exists; only the same ack may be republished.
        except FeedbackDeferred:
            return
        except (OSError, ValueError, KeyError, TypeError) as exc:
            ack = {'schema': facts.ACK, 'job_id': path.stem, 'fact_id': marker['fact_id'],
                   'status': 'needs_review', 'checked_at': wire.iso(clock),
                   'historical_only': True, 'operation_authorized': False,
                   'batches': [], 'reason': str(exc)[:200]}
            acknowledge(worker, path.stem, ack)
        worker.tokens[path.name] = token
        break
