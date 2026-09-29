import pytest

from muli_ingest.safeio import SafetyError
from muli_ingest.sources import scan_source, source_record


def test_full_scan_keeps_photo_video_sidecars_unknown_and_empty_dirs(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "PRIVATE/M4ROOT").mkdir(parents=True)
    (tmp_path / "empty").mkdir()
    for name in ["DCIM/x.ARW", "PRIVATE/M4ROOT/x.MP4", "PRIVATE/M4ROOT/x.XML", "unknown.weird", ".DS_Store"]:
        (tmp_path / name).write_bytes(name.encode())
    s = source_record(tmp_path)
    scan = scan_source(s, ["."], True)
    assert scan["complete"]
    assert {x["relative_path"] for x in scan["files"]} == {
        "DCIM/x.ARW",
        "PRIVATE/M4ROOT/x.MP4",
        "PRIVATE/M4ROOT/x.XML",
        "unknown.weird",
    }
    assert "empty" in scan["directories"]
    assert len(scan["excluded"]) == 1


def test_partial_preserves_root_path_and_no_source_writes(tmp_path):
    d = tmp_path / "a/b"
    d.mkdir(parents=True)
    (d / "x").write_bytes(b"x")
    (tmp_path / "other").write_bytes(b"no")
    scan = scan_source(source_record(tmp_path), ["a/b", "a/b"], True)
    assert [f["relative_path"] for f in scan["files"]] == ["a/b/x"]
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == [
        "a",
        "a/b",
        "a/b/x",
        "other",
    ]


def test_link_inside_scope_fails_scan(tmp_path):
    (tmp_path / "escape").symlink_to("/tmp")
    scan = scan_source(source_record(tmp_path), ["."], True)
    assert not scan["complete"]
    assert scan["errors"][0]["type"] == "unsupported_entry"


def test_configured_uuid_rejects_stale_exact_mount_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "muli_ingest.sources.mounted_volume_identity", lambda _path: (True, "SYSTEM-DISK")
    )
    with pytest.raises(SafetyError, match="设备身份不一致") as error:
        source_record(tmp_path, label="Action", volume_uuid="ACTION-CARD")
    assert error.value.code == "source_identity_mismatch"
