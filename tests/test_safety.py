import os

import pytest

from muli_ingest.safeio import Root, SafetyError, digest_file, publish_file
from muli_ingest.settings import Settings


def test_no_overwrite_under_existing_target(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.write_bytes(b"new")
    target = tmp_path / "target"
    target.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        publish_file(incoming, target)
    assert target.read_bytes() == b"old"
    assert incoming.read_bytes() == b"new"


def test_beneath_no_links_or_traversal(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (tmp_path / "secret").write_bytes(b"outside")
    (source / "link").symlink_to(tmp_path / "secret")
    with Root(source) as root:
        for path in ["../secret", "/etc/passwd", "link", "a//b"]:
            with pytest.raises((SafetyError, OSError)), root.file(path):
                pass


def test_new_content_same_attributes_different_digest(tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"aaaa")
    st = f.stat()
    before = digest_file(f)
    f.write_bytes(b"bbbb")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert digest_file(f) != before


def test_settings_default_and_reject_unsafe_values():
    s = Settings()
    assert s.retention_days == 14
    assert s.require_handoff_confirmation is True
    assert Settings(retention_days=None).retention_days is None
    for values in [{"retention_days": 0}, {"max_retries": 11}, {"hash_threads": 0}, {"auto_copy": True}]:
        with pytest.raises(ValueError):
            Settings(**values)
