import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from muli_ingest.engine import Engine
from muli_ingest.retention import Retention, now
from muli_ingest.safeio import SafetyError

LOCAL_RCLONE = Path(__file__).resolve().parents[3] / "work/tools/rclone"
RCLONE = LOCAL_RCLONE if LOCAL_RCLONE.is_file() else Path(shutil.which("rclone") or "")


@pytest.fixture
def service(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "unknown.xyz").write_bytes(b"preserve all bytes")
    e = Engine(
        tmp_path / "state", tmp_path / "stage", [source], rclone=str(RCLONE), mode="demo", allow_cleanup=True
    )
    yield e, Retention(e), source
    e.close()


def batch(e, key="first"):
    sid = e.sources()[0]["source_id"]
    s = e.scan(sid, ["."])
    return e.start(sid, s["scan_id"], s["revision"], key, background=False)


def expire(e, b):
    b["lifecycle"]["expires_at"] = (now() - timedelta(seconds=1)).isoformat()
    e._save(b)


def test_default_14_days_and_permanent_policy(service):
    e, r, _ = service
    b = batch(e)
    from datetime import datetime

    assert datetime.fromisoformat(b["lifecycle"]["expires_at"]) - datetime.fromisoformat(
        b["completed_at"]
    ) == timedelta(days=14)
    assert r.preview()["eligible"] == []
    life = r.update(b["batch_uid"], {"retention_days": None})
    assert life["expires_at"] is None
    with pytest.raises(SafetyError):
        r.update(b["batch_uid"], {"retention_days": 14})


def test_handoff_hold_review_trash_restore_preserves_source_and_report(service):
    e, r, source = service
    b = batch(e)
    uid = b["batch_uid"]
    expire(e, b)
    assert not r.preview()["eligible"]
    r.handoff(uid, True)
    r.update(uid, {"hold": True})
    assert not r.preview()["eligible"]
    r.update(uid, {"hold": False})
    plan = r.preview()
    assert len(plan["eligible"]) == 1
    result = r.execute(plan["plan_id"], True)
    assert result["results"][0]["state"] == "TRASHED", result
    assert (source / "unknown.xyz").read_bytes() == b"preserve all bytes"
    assert (e.archive / uid / "ingest_manifest.md").is_file()
    assert not (e.staging / b["batch_id"]).exists()
    life = r.restore(uid)
    assert life["storage_state"] == "ACTIVE" and life["hold"] is True
    assert (e.staging / b["batch_id"] / "SOURCE_DATA/unknown.xyz").read_bytes() == b"preserve all bytes"
    with pytest.raises(SafetyError):
        r.execute(plan["plan_id"], True)


def test_dependencies_prevent_history_cleanup(service):
    e, r, _ = service
    a = batch(e)
    b = batch(e, "second")
    assert b["summary"]["previously_ingested_count"] == 1
    expire(e, a)
    r.handoff(a["batch_uid"], True)
    assert any("引用" in x for x in r.reasons(e.batch(a["batch_uid"])))
    assert not r.preview()["eligible"]


def test_plan_rechecks_hold_and_unknown_data(service):
    e, r, _ = service
    b = batch(e)
    expire(e, b)
    r.handoff(b["batch_uid"], True)
    plan = r.preview()
    r.update(b["batch_uid"], {"hold": True})
    with pytest.raises(SafetyError):
        r.execute(plan["plan_id"], True)
    r.update(b["batch_uid"], {"hold": False})
    plan = r.preview()
    (e.staging / b["batch_id"] / "SOURCE_DATA/untracked").write_bytes(b"user addition")
    result = r.execute(plan["plan_id"], True)
    assert result["results"][0]["state"] == "CLEANUP_FAILED"
    assert (e.staging / b["batch_id"] / "SOURCE_DATA/untracked").read_bytes() == b"user addition"


def test_no_runtime_enable_and_unimplemented_setting_rejected(service):
    e, r, _ = service
    e.allow_cleanup = False
    with pytest.raises(SafetyError):
        r.execute(r.preview()["plan_id"], True)
    with pytest.raises(SafetyError):
        e.update_settings(1, {"auto_cleanup_hour": 5})
