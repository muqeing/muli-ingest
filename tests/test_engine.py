import os
from pathlib import Path
import pytest
from muli_ingest.engine import Engine
from muli_ingest.safeio import SafetyError

RCLONE = Path("/Users/zhangxuran/Documents/Codex/2026-09-27/adobe-bridge-chatgpt-conversation-6ab8d169-cc50-2/work/tools/rclone")


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
    )
    try:
        sources = engine.sources()
        assert len(sources) == 1
        assert sources[0]["path"] == str(source)
        assert sources[0]["identity_confidence"] == "low"
        assert len(engine.scan(sources[0]["source_id"], ["."])["files"]) == 1
    finally:
        engine.close()
