import pytest

from muli_ingest.access import AccessConfigurationError, LanAccess


def test_access_code_validation_and_session_tamper_detection(tmp_path):
    with pytest.raises(AccessConfigurationError):
        LanAccess("")
    with pytest.raises(AccessConfigurationError):
        LanAccess(" leading-and-long-enough")

    assert LanAccess("short").secure_cookie is False

    secret = tmp_path / "access-code"
    secret.write_text("0123456789abcdef\n", encoding="utf-8")
    secret.chmod(0o600)
    access = LanAccess.from_file(secret, session_ttl_seconds=60)
    value, expires = access.issue_session(now=100)
    assert expires == 160
    assert access.verify_session(value, now=159) == (True, 160)
    assert access.verify_session(value, now=160) == (False, None)
    assert access.verify_session(value + "x", now=159) == (False, None)
    value, _ = access.issue_session(now=200)
    assert access.verify_session(value, now=201)[0] is True
    access.revoke_session(value)
    assert access.verify_session(value, now=201) == (False, None)


def test_access_code_file_rejects_shared_permissions(tmp_path):
    secret = tmp_path / "access-code"
    secret.write_text("0123456789abcdef", encoding="utf-8")
    secret.chmod(0o644)
    with pytest.raises(AccessConfigurationError, match="0600"):
        LanAccess.from_file(secret)


def test_secure_cookie_setting_is_explicit():
    assert LanAccess("0123456789abcdef").secure_cookie is False
    assert LanAccess("0123456789abcdef", secure_cookie=True).secure_cookie is True
