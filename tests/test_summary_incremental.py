import copy
from unittest.mock import patch
import pytest
from muli_ingest.engine import Engine
from muli_ingest.summary import SummaryAccumulator, contribution, summarize
from test_engine import engine, start


def scan(files):
    return {'total_bytes':sum(f['size_bytes'] for f in files),'selected_roots':['.'],
            'complete':True,'directories':['DCIM'],'excluded':[]}


def legacy_summary(files, s):
    # Independent expected counts using the pre-optimization categories.
    groups={k:[f for f in files if f['copy_status']==k] for k in ['verified','size_verified','copied_unverified','skipped_existing','failed']}
    copied=groups['verified']+groups['size_verified']+groups['copied_unverified']
    value={'selected_file_count':len(files),'selected_bytes':s['total_bytes'],
           'source_file_count':len(files) if s['selected_roots']==['.'] else None,
           'source_observed_file_count':len(files),'source_scan_complete':s['selected_roots']==['.'],
           'selection_scan_complete':s['complete'],'copy_required_count':len(files)-len(groups['skipped_existing']),
           'copied_file_count':len(copied),'verified_file_count':len(groups['verified']),
           'size_verified_file_count':len(groups['size_verified']),
           'awaiting_hash_file_count':len(groups['copied_unverified']),
           'previously_ingested_count':len(groups['skipped_existing']),
           'failed_file_count':len(groups['failed']),
           'pending_file_count':len(files)-len(copied)-len(groups['skipped_existing'])-len(groups['failed']),
           'copied_bytes':sum(f['size_bytes'] for f in copied),
           'previously_ingested_bytes':sum(f['size_bytes'] for f in groups['skipped_existing']),
           'photo_count':sum(f['media_type']=='photo' for f in files),
           'video_count':sum(f['media_type']=='video' for f in files),
           'other_count':sum(f['media_type']=='other' for f in files),
           'selected_directory_count':len(s['directories']),'excluded_entry_count':len(s['excluded'])}
    return value


def test_all_state_transitions_and_duplicate_result_equal_legacy():
    states=['pending','copying','copied_unverified','verified','size_verified','skipped_existing','failed']
    files=[{'relative_path':str(i),'copy_status':s,'size_bytes':i*123,'media_type':['photo','video','other'][i%3]} for i,s in enumerate(states)]
    s=scan(files);totals=SummaryAccumulator(files,s)
    for state in states+list(reversed(states)):
        for f in files:
            before=contribution(f);f['copy_status']=state
            assert totals.replace(before,f)==legacy_summary(files,s)
            # Resume may report an already completed file again.
            assert totals.replace(contribution(f),f)==legacy_summary(files,s)
    assert summarize(files,s)==legacy_summary(files,s)


@pytest.mark.parametrize('mode',['exact','size_only'])
def test_real_engine_reads_table_bounded_and_final_totals_match(engine,mode):
    e,source=engine
    e.update_settings(1,{'verification_mode':mode,'metadata_level':'minimal'})
    for i in range(40):(source/f'{i:04}.ARW').write_bytes(b'raw'+bytes([i])*19)
    actual=e.store.files
    with patch.object(e.store,'files',wraps=actual) as reads:
        b=start(e)
        assert b['state']=='COMPLETED',b
        assert reads.call_count<15,reads.call_count
    s=e.store.get('scan',b['scope']['scan_id'])
    assert b['summary']==legacy_summary(actual(b['batch_uid']),s)
    assert (e.staging/b['batch_id']/'ingest_complete.json').is_file()


def test_terminal_counter_corruption_never_reports_completed(engine):
    e,source=engine;(source/'a.ARW').write_bytes(b'abc')
    original=SummaryAccumulator.replace
    def corrupt(self,before,file):
        result=original(self,before,file)
        self.value['copied_bytes']+=1
        return result
    with patch.object(SummaryAccumulator,'replace',corrupt):b=start(e)
    assert b['state']=='FAILED'
    assert 'summary_mismatch' in b['error']
    assert not (e.staging/b['batch_id']/'ingest_complete.json').exists()
