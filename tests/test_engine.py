import os
import threading
import time
from pathlib import Path

import pytest

from muli_ingest.engine import Engine
from muli_ingest.safeio import SafetyError
from muli_ingest.sources import source_record

RCLONE = Path(__file__).resolve().parents[3] / "work/tools/rclone"


@pytest.fixture
def engine(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    e = Engine(tmp_path / "state", tmp_path / "stage", [source], rclone=str(RCLONE), mode="demo")
    yield e, source
    e.close()


def start(e):
    sid = e.sources()[0]["source_id"]
    scan = e.scan(sid, ["."])
    batch = e.start(sid, scan["scan_id"], 1, "request-" + os.urandom(8).hex(), background=False)
    return e.batch(batch["batch_uid"])


def test_verified_copy_retention_and_incremental_readback(engine):
    e, source = engine
    (source / "DCIM").mkdir()
    (source / "DCIM/a.ARW").write_bytes(b"camera-data")
    first = start(e)
    assert first["state"] == "COMPLETED", first
    assert first["summary"]["verified_file_count"] == 1
    assert first["lifecycle"]["expires_at"] is not None
    target = Path(e.staging) / first["batch_id"] / "SOURCE_DATA/DCIM/a.ARW"
    assert target.read_bytes() == b"camera-data"
    (source / "new.XML").write_bytes(b"camera-sidecar")
    second = start(e)
    assert second["summary"]["previously_ingested_count"] == 1
    assert second["summary"]["verified_file_count"] == 1
    target.write_bytes(b"corrupt")
    third = start(e)
    assert third["summary"]["verified_file_count"] == 1
    assert third["summary"]["previously_ingested_count"] == 1
    assert target.read_bytes() == b"corrupt"


def test_discovery_does_not_copy_and_idempotent_manual_start(engine):
    e, source = engine
    (source / "x").write_bytes(b"x")
    sid = e.sources()[0]["source_id"]
    scan = e.scan(sid, ["."])
    assert list(e.staging.glob("BATCH_*")) == []
    a = e.start(sid, scan["scan_id"], 1, "same", background=False)
    b = e.start(sid, scan["scan_id"], 1, "same", background=False)
    assert a["batch_uid"] == b["batch_uid"]
    assert len(list(e.staging.glob("BATCH_*"))) == 1


def test_progress_reports_speed_average_and_eta(engine, monkeypatch):
    e, source = engine
    (source / "x").write_bytes(b"x")
    batch = start(e)
    batch["state"] = "COPYING"
    batch["summary"]["copied_bytes"] = 0
    batch["summary"]["previously_ingested_bytes"] = 0
    batch["progress"].update(
        stage="SOURCE_HASH",
        relative_path="x",
        bytes=0,
        total_bytes=1000,
        current_file_bytes=0,
        speed_bps=0,
        sampled_at=None,
        copy_started_at=None,
    )
    e._save(batch)
    samples = iter([100.0, 102.0])
    monkeypatch.setattr("muli_ingest.engine.time.time", lambda: next(samples))
    e._progress(batch["batch_uid"], "COPYING", "x", 100, 1000)
    e._progress(batch["batch_uid"], "COPYING", "x", 300, 1000)
    progress = e.batch(batch["batch_uid"])["progress"]
    assert progress["bytes"] == 300
    assert progress["speed_bps"] == 100
    assert progress["average_speed_bps"] == 150
    assert progress["eta_seconds"] == 7


def test_new_file_hashes_source_during_copy_without_prepass(engine, monkeypatch):
    e, source = engine
    payload = b"camera-data" * 1024
    (source / "new.mov").write_bytes(payload)
    prehash_calls = 0
    original = __import__("muli_ingest.engine", fromlist=["digest_fd"]).digest_fd

    def counted(*args, **kwargs):
        nonlocal prehash_calls
        prehash_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr("muli_ingest.engine.digest_fd", counted)
    batch = start(e)
    copied = e.staging / batch["batch_id"] / "SOURCE_DATA/new.mov"
    assert batch["state"] == "COMPLETED", batch
    assert copied.read_bytes() == payload
    assert prehash_calls == 0


def test_existing_path_still_prehashes_for_safe_incremental_reuse(engine, monkeypatch):
    e, source = engine
    (source / "repeat.mov").write_bytes(b"same-data")
    start(e)
    prehash_calls = 0
    original = __import__("muli_ingest.engine", fromlist=["digest_fd"]).digest_fd

    def counted(*args, **kwargs):
        nonlocal prehash_calls
        prehash_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr("muli_ingest.engine.digest_fd", counted)
    second = start(e)
    assert second["state"] == "COMPLETED", second
    assert second["summary"]["previously_ingested_count"] == 1
    assert prehash_calls == 1


def test_exact_mode_stages_entire_batch_before_destination_hashing(engine):
    e, source = engine
    (source / "a.mov").write_bytes(b"a" * 1024)
    (source / "b.mov").write_bytes(b"b" * 1024)
    batch = start(e)
    events = e.store.events(batch["batch_uid"])
    staged = [event["seq"] for event in events if event["type"] == "FILE_STAGED"]
    verified = [event["seq"] for event in events if event["type"] == "VERIFIED_TEMP"]
    files = e.store.files(batch["batch_uid"])
    assert len(staged) == len(verified) == 2
    assert max(staged) < min(verified)
    assert all(file["copy_status"] == "verified" for file in files)
    assert all(file.get("staged_temp") is None for file in files)
    assert batch["result"] == "COPY_VERIFIED"


def test_size_only_mode_reports_weaker_evidence_without_destination_hash(engine):
    e, source = engine
    (source / "DCIM").mkdir()
    (source / "DCIM/fast.mov").write_bytes(b"fast-copy")
    e.update_settings(1, {"verification_mode": "size_only"})
    batch = start(e)
    file = e.store.files(batch["batch_uid"])[0]
    assert batch["state"] == "COMPLETED"
    assert batch["result"] == "COPY_SIZE_VERIFIED"
    assert batch["summary"]["size_verified_file_count"] == 1
    assert batch["summary"]["verified_file_count"] == 0
    assert batch["source_coverage"]["status"] == "FULLY_COPIED_SIZE_CHECKED"
    assert file["copy_status"] == "size_verified"
    assert file["destination_hash"] is None
    assert file["hash_match"] is None
    assert (e.staging / batch["batch_id"] / "SOURCE_DATA/DCIM/fast.mov").read_bytes() == b"fast-copy"


def test_resume_does_not_rehash_already_verified_destination(engine, monkeypatch):
    e, source = engine
    (source / "resume.mov").write_bytes(b"verified-once")
    completed = start(e)
    root = e.staging / completed["batch_id"]
    (root / "ingest_complete.json").unlink()
    batch = e.batch(completed["batch_uid"])
    batch["state"] = "INTERRUPTED"
    batch["completed_at"] = None
    e._save(batch)
    destination_reads = 0
    original = __import__("muli_ingest.engine", fromlist=["digest_file"]).digest_file

    def counted(path, *args, **kwargs):
        nonlocal destination_reads
        if "SOURCE_DATA" in Path(path).parts:
            destination_reads += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr("muli_ingest.engine.digest_file", counted)
    resumed = e.resume(batch["batch_uid"], background=False)
    assert resumed["state"] == "COMPLETED"
    assert destination_reads == 0


def test_same_size_same_mtime_content_is_not_skipped(engine):
    e, source = engine
    f = source / "x"
    f.write_bytes(b"aaaa")
    st = f.stat()
    first = start(e)
    f.write_bytes(b"bbbb")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    second = start(e)
    assert second["summary"]["previously_ingested_count"] == 0
    assert second["summary"]["verified_file_count"] == 1
    assert (e.staging / first["batch_id"] / "SOURCE_DATA/x").read_bytes() == b"aaaa"


def test_stale_scan_and_missing_source_fail_without_success(engine):
    e, source = engine
    (source / "x").write_bytes(b"x")
    sid = e.sources()[0]["source_id"]
    scan = e.scan(sid, ["."])
    (source / "x").write_bytes(b"new")
    with pytest.raises(SafetyError):
        e.start(sid, scan["scan_id"], 1, "stale", background=False)


def test_failure_continues_and_does_not_issue_complete_receipt(engine):
    e, source = engine
    for name in ["a", "b", "c"]:
        (source / name).write_bytes(name.encode())
    original = e.copier.copy_and_hash

    def fail(fd, temp, settings, check, progress):
        if os.read(fd, 1) == b"b":
            raise OSError("injected source error")
        os.lseek(fd, 0, 0)
        return original(fd, temp, settings, check, progress)

    e.copier.copy_and_hash = fail
    e.update_settings(1, {"max_retries": 1, "retry_delay_seconds": 0.0})
    batch = start(e)
    assert batch["state"] == "FAILED"
    assert batch["summary"]["verified_file_count"] == 2
    assert batch["summary"]["failed_file_count"] == 1
    assert not (e.staging / batch["batch_id"] / "ingest_complete.json").exists()
    failed = next(f for f in e.store.files(batch["batch_uid"]) if f["copy_status"] == "failed")
    assert failed["attempt_count"] == 2


def test_bad_transport_hash_is_rejected_and_can_resume(engine):
    e, source = engine
    (source / "x").write_bytes(b"original")
    original = e.copier.copy_and_hash

    def corrupt(fd, temp, settings, check, progress):
        temp.write_bytes(b"corrupt!")

    e.copier.copy_and_hash = corrupt
    e.update_settings(1, {"max_retries": 0})
    bad = start(e)
    assert bad["state"] == "FAILED"
    assert not (e.staging / bad["batch_id"] / "SOURCE_DATA/x").exists()
    e.copier.copy_and_hash = original
    good = e.resume(bad["batch_uid"], background=False)
    assert good["state"] == "COMPLETED", good
    assert (e.staging / good["batch_id"] / "SOURCE_DATA/x").read_bytes() == b"original"


def test_final_receipt_preserved_on_ambiguous_restart(engine):
    e, source = engine
    (source / "x").write_bytes(b"x")
    b = start(e)
    root = e.staging / b["batch_id"]
    evidence = (root / "ingest_manifest.md").read_bytes()
    b["state"] = "INTERRUPTED"
    e._save(b)
    resumed = e.resume(b["batch_uid"], background=False)
    assert resumed["state"] == "FAILED"
    assert "existing_receipt_requires_review" in resumed["error"]
    assert (root / "ingest_manifest.md").read_bytes() == evidence


def test_empty_media_never_full_coverage(engine):
    e, source = engine
    (source / "DCIM").mkdir()
    b = start(e)
    assert b["source_coverage"]["status"] == "NOT_FULL"


def test_configured_local_source_can_share_target_filesystem(tmp_path):
    source = tmp_path / "mounted-source"
    source.mkdir()
    (source / "clip.mov").write_bytes(b"fixture")
    engine = Engine(
        tmp_path / "state",
        tmp_path / "stage",
        [source],
        rclone=str(RCLONE),
        mode="local",
        source_labels=["DJI Action SD"],
        source_uuids=["ABCD-1234"],
    )
    try:
        sources = engine.sources()
        assert len(sources) == 1
        assert sources[0]["path"] == str(source)
        assert sources[0]["label"] == "DJI Action SD"
        assert sources[0]["volume_uuid"] == "ABCD-1234"
        assert sources[0]["identity_confidence"] == "high"
        assert len(engine.scan(sources[0]["source_id"], ["."])["files"]) == 1
    finally:
        engine.close()


def test_dynamic_discovery_keeps_camera_media_and_deduplicates_explicit_source(
    tmp_path, monkeypatch
):
    explicit = tmp_path / "explicit"
    camera = tmp_path / "camera"
    storage = tmp_path / "storage"
    for path in (explicit, camera, storage):
        path.mkdir()
    (camera / "DCIM").mkdir()
    records = [
        source_record(explicit, label="duplicate", volume_uuid="EXPLICIT"),
        source_record(camera, label="Sony DSC", volume_uuid="CAMERA"),
        source_record(storage, label="USB disk", volume_uuid="STORAGE"),
    ]
    records[1]["identity"][0] += 1
    records[2]["identity"][0] += 2
    monkeypatch.setattr("muli_ingest.engine.linux_external_mounts", lambda protected: records)
    engine = Engine(
        tmp_path / "state",
        tmp_path / "stage",
        [explicit],
        rclone=str(RCLONE),
        mode="local",
        source_labels=["Configured"],
        source_uuids=["EXPLICIT"],
    )
    try:
        connected = [item for item in engine.sources() if item["connected"]]
        assert [item["label"] for item in connected] == ["Configured", "Sony DSC"]
    finally:
        engine.close()


def test_two_different_sources_copy_in_parallel_by_default(tmp_path):
    sources = [tmp_path / "camera-a", tmp_path / "camera-b"]
    for index, source in enumerate(sources):
        source.mkdir()
        (source / f"clip-{index}.mov").write_bytes(b"camera-data" * 1024)
    engine = Engine(
        tmp_path / "state",
        tmp_path / "stage",
        sources,
        rclone=str(RCLONE),
        mode="demo",
    )
    barrier = threading.Barrier(2)
    original = engine.copier.copy_and_hash

    def synchronized(fd, temp, settings, check, progress):
        barrier.wait(timeout=3)
        return original(fd, temp, settings, check, progress)

    engine.copier.copy_and_hash = synchronized
    try:
        batches = []
        for source in engine.sources():
            scan = engine.scan(source["source_id"], ["."])
            batches.append(
                engine.start(
                    source["source_id"], scan["scan_id"], 1, f"parallel-{source['source_id']}"
                )
            )
        for batch in batches:
            engine.futures[batch["batch_uid"]].result(timeout=5)
            assert engine.batch(batch["batch_uid"])["state"] == "COMPLETED"
    finally:
        engine.close()


def test_parallel_source_limit_one_keeps_second_batch_queued(tmp_path):
    sources = [tmp_path / "camera-a", tmp_path / "camera-b"]
    for index, source in enumerate(sources):
        source.mkdir()
        (source / f"clip-{index}.mov").write_bytes(b"camera-data")
    engine = Engine(
        tmp_path / "state",
        tmp_path / "stage",
        sources,
        rclone=str(RCLONE),
        mode="demo",
    )
    engine.update_settings(1, {"max_parallel_sources": 1})
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    original = engine.copier.copy_and_hash

    def blocked(fd, temp, settings, check, progress):
        nonlocal calls
        with calls_lock:
            calls += 1
            current = calls
        if current == 1:
            entered.set()
            assert release.wait(timeout=3)
        return original(fd, temp, settings, check, progress)

    engine.copier.copy_and_hash = blocked
    try:
        batches = []
        for source in engine.sources():
            scan = engine.scan(source["source_id"], ["."])
            batches.append(
                engine.start(source["source_id"], scan["scan_id"], 1, f"serial-{source['source_id']}")
            )
        assert entered.wait(timeout=2)
        time.sleep(0.1)
        assert engine.batch(batches[1]["batch_uid"])["state"] == "QUEUED"
        assert calls == 1
        release.set()
        for batch in batches:
            engine.futures[batch["batch_uid"]].result(timeout=5)
            assert engine.batch(batch["batch_uid"])["state"] == "COMPLETED"
    finally:
        release.set()
        engine.close()
