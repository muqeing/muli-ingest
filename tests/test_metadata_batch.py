import json
from pathlib import Path
from types import SimpleNamespace
import threading
from copy import deepcopy
import pytest
from muli_ingest import metadata as md
from muli_ingest.engine import Engine
from muli_ingest.store import Store


def photos(tmp_path, n=2):
    result=[]
    for i in range(n):
        p=tmp_path/f'照片 {i}.JPG';p.write_bytes(b'synthetic')
        result.append((p,'photo'))
    return result


def payload(path):
    return {'SourceFile':str(path),'EXIF:DateTimeOriginal':'2026:10:05 12:34:56',
            'EXIF:OffsetTimeOriginal':'+08:00','EXIF:ISO':int(path.stem.split()[-1])+100,
            'EXIF:ImageWidth':120,'EXIF:Make':'Sony'}


def test_batch_matches_individual_and_binds_reordered_results(tmp_path, monkeypatch):
    items=photos(tmp_path,8);calls=[]
    def reader(cmd,timeout):
        calls.append(cmd)
        paths=[Path(v) for v in cmd if Path(v).is_file()]
        return 'ok',json.dumps([payload(p) for p in reversed(paths)]).encode(),0
    monkeypatch.setattr(md,'_run_bounded',reader)
    expected=[md.extract_metadata(p,k) for p,k in items]
    calls.clear();actual=md.extract_metadata_batch(items)
    assert actual==expected and len(calls)==1
    assert 'QuickTimeUTC=0' in calls[0] and calls[0][1:3]==['-config','']


@pytest.mark.parametrize('bad',['duplicate','missing','unknown','shape','json'])
def test_unbound_batch_falls_back_without_wrong_file_attribution(tmp_path,monkeypatch,bad):
    items=photos(tmp_path);calls=[]
    def reader(cmd,timeout):
        paths=[Path(v) for v in cmd if Path(v).is_file()];calls.append(paths)
        rows=[payload(p) for p in paths]
        if len(paths)>1:
            if bad=='duplicate':rows[1]=rows[0]
            elif bad=='missing':rows.pop()
            elif bad=='unknown':rows[1]['SourceFile']='/unexpected.JPG'
            elif bad=='shape':rows={'wrong':rows}
            elif bad=='json':return 'ok',b'not-json',0
        return 'ok',json.dumps(rows).encode(),0
    monkeypatch.setattr(md,'_run_bounded',reader)
    result=md.extract_metadata_batch(items)
    assert len(calls)==3 and [v['capture']['iso'] for v in result]==[100,101]


def test_timeout_bounded_fallback_and_cancellation(tmp_path,monkeypatch):
    items=photos(tmp_path);calls=[]
    def reader(cmd,timeout):calls.append(timeout);return 'timeout',b'',-9
    monkeypatch.setattr(md,'_run_bounded',reader)
    result=md.extract_metadata_batch(items,timeout=0.1)
    assert calls==[0.1]*3 and all(r['warnings']==['exiftool_timeout'] for r in result)
    calls.clear()
    def stop():raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError,match='cancelled'):md.extract_metadata_batch(items,checkpoint=stop)
    assert calls==[30]


def test_minimal_and_unsupported_media_do_not_launch_tools(tmp_path,monkeypatch):
    items=photos(tmp_path)
    monkeypatch.setattr(md,'_run_bounded',lambda *a:pytest.fail('unexpected tool'))
    assert all(r['status']=='not_requested' for r in md.extract_metadata_batch(items,level='minimal'))
    assert md.extract_metadata_batch([(items[0][0],'other')])[0]['warnings']==['unsupported_media_type']
    with pytest.raises(ValueError):md.extract_metadata_batch(items*17)


def test_video_retains_both_readers(tmp_path,monkeypatch):
    video=tmp_path/'clip.mp4';video.write_bytes(b'synthetic');calls=[]
    def reader(cmd,t):
        calls.append(cmd)
        raw=[{'SourceFile':str(video)}] if cmd[0]=='exiftool' else {'format':{},'streams':[]}
        return 'ok',json.dumps(raw).encode(),0
    monkeypatch.setattr(md,'_run_bounded',reader)
    assert md.extract_metadata_batch([(video,'video')])[0]['status']=='ok'
    assert len(calls)==2 and calls[1][0]=='ffprobe' and 'file,pipe' in calls[1]


def fake_engine(tmp_path,n=65):
    e=Engine.__new__(Engine);e.staging=tmp_path/'stage';e.store=Store(tmp_path/'state.sqlite');e.lock=threading.RLock()
    b={'batch_uid':'test-uid','batch_id':'BATCH_20261005_999999','source_id':'synthetic','state':'COPYING','progress':{}}
    state=[deepcopy(b)];reports=[]
    e.batch=lambda uid:deepcopy(state[0]);e._check=lambda row:None
    def save(row):
        done=sum(f.get('metadata') is not None for f in e.store.files(b['batch_uid']))
        assert row['progress']['metadata_done']==done
        state[0]=deepcopy(row);reports.append(deepcopy(row))
    e._save=save
    for i in range(n):
        e.store.put_file(b['batch_uid'],'synthetic',{'relative_path':f'{i:04}.JPG','copy_status':'size_verified','media_type':'photo','source_hash':f'digest-{i}','metadata':None})
    return e,b,reports


def test_chunk_commits_keep_copy_fields_and_report_durable_counts(tmp_path,monkeypatch):
    e,b,reports=fake_engine(tmp_path);calls=[];sql=[]
    e.store.db.set_trace_callback(sql.append)
    def batch(items,*a,**kw):calls.append(len(items));return [{'status':'ok'} for _ in items]
    monkeypatch.setattr('muli_ingest.engine.extract_metadata_batch',batch)
    try:
        before=e.store.files(b['batch_uid']);cfg=SimpleNamespace(metadata_level='standard',metadata_timeout_seconds=30)
        e._extract_size_only_metadata(b,cfg)
        after=e.store.files(b['batch_uid'])
        assert calls==[32,32,1] and len([s for s in sql if s=='COMMIT'])==3
        assert [{k:v for k,v in f.items() if k!='metadata'} for f in before]==[{k:v for k,v in f.items() if k!='metadata'} for f in after]
        assert [r['progress']['metadata_done'] for r in reports]==[0,32,64,65]
        assert all(r['progress']['stage']=='METADATA' for r in reports)
        calls.clear();e._extract_size_only_metadata(b,cfg);assert calls==[]
    finally:e.store.close()


def test_short_batch_result_rolls_back_and_keeps_progress(tmp_path,monkeypatch):
    e,b,reports=fake_engine(tmp_path,4)
    monkeypatch.setattr('muli_ingest.engine.extract_metadata_batch',lambda *a,**k:[{'status':'ok'}])
    try:
        with pytest.raises(ValueError):e._extract_size_only_metadata(b,SimpleNamespace(metadata_level='standard',metadata_timeout_seconds=30))
        assert all(f['metadata'] is None for f in e.store.files(b['batch_uid']))
        assert reports[-1]['progress']['metadata_done']==0
    finally:e.store.close()


def test_protected_copy_verification_coordination_paths_unchanged():
    """Portable guard for unchanged media, source-release and copy transactions.

    The old check required private baseline directories and was deselected by
    conftest. Pins now name maintained pre-change source, so Linux collects it.
    New completion-index/status code does not expand this media-write scope.
    """
    import ast
    import hashlib
    root = Path(__file__).resolve().parents[1]
    expected = json.loads((root/'tests/fixtures/completion-safety.json').read_text())
    import importlib.util
    source = Path(next(iter(importlib.util.find_spec('muli_ingest').submodule_search_locations)))
    for name, digest in expected['files'].items():
        assert hashlib.sha256((source/name).read_bytes()).hexdigest() == digest, name
    for module, spec in expected['methods'].items():
        tree = ast.parse((source/module).read_text())
        methods = {n.name:n for c in tree.body if isinstance(c,ast.ClassDef) and c.name==spec['class']
                   for n in c.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
        for name, digest in spec['hashes'].items():
            assert hashlib.sha256(ast.dump(methods[name],include_attributes=False).encode()).hexdigest() == digest, name
