"""Fairness, cancellation and unchanged final evidence using synthetic media."""
import threading
import time
from pathlib import Path

import pytest
from test_source_release import start, until

from muli_ingest.engine import Engine
from muli_ingest.metadata_scheduler import MetadataScheduler
from muli_ingest.safeio import SafetyError
from muli_ingest.store import Store

RCLONE = Path('/usr/local/bin/rclone')


def tagged(uid, function):
    function.batch_uid = uid
    return function


def test_one_physical_turn_and_both_lanes_make_progress():
    scheduler = MetadataScheduler(lambda uid: uid == 'short')
    cancel = threading.Event();order = [];lock = threading.Lock()
    active = 0;maximum = 0;entered = threading.Event();release = threading.Event()
    def work(uid):
        def run():
            nonlocal active, maximum
            for n in range(20):
                with scheduler.turn(cancel=cancel):
                    with lock:active += 1;maximum = max(maximum, active);order.append(uid)
                    try:
                        if uid == 'regular' and n == 0:
                            entered.set();assert release.wait(3)
                        time.sleep(.001)
                    finally:
                        with lock:active -= 1
        return tagged(uid, run)
    try:
        normal=scheduler.submit(work('regular'));assert entered.wait(3)
        short=scheduler.submit(work('short'))
        until(lambda:scheduler.waiting[True] == 1)
        release.set();normal.result(5);short.result(5)
        assert maximum == 1
        assert order.count('short') == order.count('regular') == 20
        assert order.index('short') < 5
        # While both lanes have work, a long sequence of short turns cannot
        # indefinitely postpone the regular coordinator.
        first=order.index('short');last_regular=max(i for i,x in enumerate(order) if x=='regular')
        assert 'shortshortshortshortshort' not in ''.join(order[first:last_regular]).replace('regular','|')
    finally:
        release.set();scheduler.shutdown(cancel_futures=True)


def test_cancelled_waiter_does_not_release_another_threads_turn():
    scheduler=MetadataScheduler(lambda uid:uid=='short')
    stop=threading.Event();entered=threading.Event();release=threading.Event()
    def owner():
        with scheduler.turn(cancel=threading.Event()):entered.set();assert release.wait(3)
    def waiting():
        with scheduler.turn(cancel=stop):pytest.fail('cancelled waiter acquired turn')
    try:
        a=scheduler.submit(tagged('regular',owner));assert entered.wait(3)
        b=scheduler.submit(tagged('short',waiting));until(lambda:scheduler.waiting[True]==1)
        stop.set()
        with pytest.raises(SafetyError,match='operator_stop'):b.result(1)
        assert scheduler.busy
        release.set();a.result(3);assert not scheduler.busy
    finally:release.set();scheduler.shutdown(cancel_futures=True)


def test_checkpoint_can_yield_between_readers_without_overlapping_them():
    scheduler=MetadataScheduler(lambda uid:uid=='short')
    stop=threading.Event();entered=threading.Event();release=threading.Event();order=[]
    def normal():
        with scheduler.turn(cancel=stop):
            scheduler.checkpoint(cancel=stop)
            order.append('reader-one');entered.set();assert release.wait(3)
            scheduler.checkpoint(cancel=stop)
            order.append('reader-two')
    def quick():
        with scheduler.turn(cancel=stop):order.append('short-reader')
    try:
        a=scheduler.submit(tagged('regular',normal));assert entered.wait(3)
        b=scheduler.submit(tagged('short',quick));until(lambda:scheduler.waiting[True]==1)
        release.set();a.result(3);b.result(3)
        assert order == ['reader-one','short-reader','reader-two']
    finally:release.set();scheduler.shutdown(cancel_futures=True)


def test_cancellation_after_checkpoint_yield_preserves_the_new_owner():
    scheduler=MetadataScheduler(lambda uid:uid=='short')
    stop=threading.Event();entered=threading.Event();release=threading.Event()
    other=threading.Event();finish=threading.Event()
    def regular():
        with scheduler.turn(cancel=stop):
            scheduler.checkpoint(cancel=stop);entered.set();assert release.wait(3)
            scheduler.checkpoint(cancel=stop)
    def quick():
        with scheduler.turn(cancel=threading.Event()):other.set();assert finish.wait(3)
    try:
        a=scheduler.submit(tagged('regular',regular));assert entered.wait(3)
        b=scheduler.submit(tagged('short',quick));until(lambda:scheduler.waiting[True]==1)
        release.set();assert other.wait(3);stop.set()
        with pytest.raises(SafetyError,match='operator_stop'):a.result(1)
        assert scheduler.busy
        finish.set();b.result(3);assert not scheduler.busy
    finally:release.set();finish.set();scheduler.shutdown(cancel_futures=True)


def test_shutdown_wakes_waiters_and_rejects_new_turns():
    scheduler=MetadataScheduler(lambda uid:uid=='short')
    entered=threading.Event();release=threading.Event()
    def owner():
        with scheduler.turn(cancel=threading.Event()):entered.set();assert release.wait(3)
    def waiter():
        with scheduler.turn(cancel=threading.Event()):pytest.fail('shutdown waiter ran')
    a=scheduler.submit(tagged('regular',owner));assert entered.wait(3)
    b=scheduler.submit(tagged('short',waiter));until(lambda:scheduler.waiting[True]==1)
    scheduler.shutdown(wait=False,cancel_futures=True)
    with pytest.raises(SafetyError,match='operator_stop'):b.result(1)
    release.set();a.result(3)
    assert not scheduler.busy


def test_recursive_synchronous_wait_fails_instead_of_deadlocking():
    scheduler=MetadataScheduler(lambda uid:True)
    try:
        future=scheduler.submit(tagged('short',lambda:scheduler.run_and_wait('other',lambda:None)))
        with pytest.raises(SafetyError,match='metadata_reentrant_wait'):future.result(1)
    finally:scheduler.shutdown(cancel_futures=True)


def test_lane_choice_reads_bounded_index_keys_not_file_bodies(tmp_path):
    s=Store(tmp_path/'db');rows=[]
    try:
        with s.transaction():
            s.db.executemany('INSERT INTO files VALUES(?,?,?,?,?,?)',
                [('large',str(i),'s',None,'verified','not-json') for i in range(10000)])
        s.db.set_trace_callback(rows.append)
        assert s.file_count_up_to('large',129)==129
        assert s.file_count_up_to('missing',129)==0
        assert all('SELECT 1' in r and 'LIMIT 129' in r for r in rows)
        with pytest.raises(ValueError):s.file_count_up_to('large',True)
    finally:s.close()


def test_backlog_loads_only_two_working_contexts():
    scheduler=MetadataScheduler(lambda uid:uid.startswith('short'))
    release=threading.Event();entered=threading.Event();loaded=[]
    def task(uid):
        def run():
            loaded.append(uid)
            with scheduler.turn(cancel=threading.Event()):
                if uid=='regular-0':entered.set();assert release.wait(3)
        return tagged(uid,run)
    first=scheduler.submit(task('regular-0'));assert entered.wait(3)
    second=scheduler.submit(task('short-0'));until(lambda:len(loaded)==2)
    for n in range(1,100):
        scheduler.submit(task('regular-'+str(n)));scheduler.submit(task('short-'+str(n)))
    assert loaded == ['regular-0','short-0']
    scheduler.shutdown(wait=False,cancel_futures=True)
    with pytest.raises(SafetyError,match='operator_stop'):second.result(1)
    release.set();first.result(3)
    assert len(loaded)==2


@pytest.fixture
def fair_setup(tmp_path,monkeypatch):
    monkeypatch.setenv('MULI_METADATA_FAIRNESS','1')
    camera=tmp_path/'camera';camera.mkdir();(camera/'one.JPG').write_bytes(b'synthetic')
    monkeypatch.setattr('muli_ingest.engine.extract_metadata_batch',
                        lambda items,*a,**k:[{'status':'not_requested'} for _ in items])
    e=Engine(tmp_path/'state',tmp_path/'stage',[camera],rclone=str(RCLONE),mode='demo')
    e.update_settings(1,{'verification_mode':'size_only','metadata_level':'minimal'})
    yield e,camera
    e.close()


def test_short_batch_completes_before_regular_batch_and_retains_full_receipts(fair_setup,monkeypatch):
    e,camera=fair_setup
    for n in range(128):(camera/f'{n:04}.JPG').write_bytes(b'synthetic')
    entered=threading.Event();release=threading.Event();calls=[];completed=[]
    original_complete=e._complete_batch
    def complete(uid,*args):
        original_complete(uid,*args);completed.append(uid)
    e._complete_batch=complete
    def reader(items,*a,**k):
        bid=items[0][0].parts[-3];calls.append((bid,len(items)))
        if len(items)==32 and not entered.is_set():
            entered.set();assert release.wait(15)
        return [{'status':'not_requested'} for _ in items]
    monkeypatch.setattr('muli_ingest.engine.extract_metadata_batch',reader)
    large=start(e,background=True);assert entered.wait(15)
    small_camera=camera.parent/'small-camera';small_camera.mkdir();(small_camera/'small.JPG').write_bytes(b'short')
    e._explicit_paths.append(small_camera);e._explicit_labels[str(small_camera)]=None;e._explicit_uuids[str(small_camera)]=None
    e.refresh_sources();source=next(s for s in e.sources() if s['path']==str(small_camera))
    try:
        small=start(e,source,background=True)
        until(lambda:e.metadata_pool.waiting[True]>0,15)
        assert not (e.staging/large['batch_id']/'ingest_complete.json').exists()
        release.set();e.futures[small['batch_uid']].result(15)
        e.futures[large['batch_uid']].result(15)
        assert completed == [small['batch_uid'],large['batch_uid']]
        assert next(i for i,(bid,_) in enumerate(calls) if bid==small['batch_id']) < max(i for i,(bid,_) in enumerate(calls) if bid==large['batch_id'])
        for batch in (large,small):
            result=e.batch(batch['batch_uid']);assert result['state']=='COMPLETED',result
            assert result['summary']['failed_file_count']==result['summary']['pending_file_count']==0
            assert (e.staging/result['batch_id']/'ingest_complete.json').exists()
    finally:release.set()


def test_changed_destination_during_fair_metadata_cannot_emit_success(fair_setup):
    e,_=fair_setup;original=e._extract_size_only_metadata
    def changed(b,cfg):
        original(b,cfg)
        target=e.staging/b['batch_id']/'SOURCE_DATA/one.JPG';target.write_bytes(b'x'*target.stat().st_size)
    e._extract_size_only_metadata=changed
    b=start(e)
    assert b['state']=='FAILED' and 'released_target_changed' in b['error']
    assert not (e.staging/b['batch_id']/'ingest_complete.json').exists()


def test_invalid_flag_does_not_initialize_service_state(tmp_path,monkeypatch):
    monkeypatch.setenv('MULI_METADATA_FAIRNESS','yes')
    with pytest.raises(ValueError,match='MULI_METADATA_FAIRNESS'):
        Engine(tmp_path/'state',tmp_path/'stage',rclone='unused',mode='demo')
    assert not (tmp_path/'state').exists()


def test_synchronous_metadata_also_uses_the_bounded_coordinator(fair_setup):
    e,_=fair_setup;threads=[];original=e._postprocess
    def observe(uid):
        threads.append(threading.current_thread().name);return original(uid)
    e._postprocess=observe
    result=start(e,background=False)
    assert result['state']=='COMPLETED'
    assert len(threads)==1 and threads[0].startswith('metadata-short')


def test_synchronous_released_resume_never_waits_with_engine_lock(fair_setup):
    e,camera=fair_setup;original=e._extract_size_only_metadata
    def failed(*_):raise RuntimeError('synthetic metadata interruption')
    e._extract_size_only_metadata=failed
    bad=start(e);assert bad['state']=='FAILED' and bad['source_released']
    camera.rename(camera.with_name('disconnected'))
    e._extract_size_only_metadata=original
    original_wait=e._run_metadata_synchronously
    def unlocked(uid):
        assert not e.lock._is_owned(), 'cannot wait for a coordinator while holding the engine lock'
        return original_wait(uid)
    e._run_metadata_synchronously=unlocked
    result=e.resume(bad['batch_uid'],background=False)
    assert result['state']=='COMPLETED',result
