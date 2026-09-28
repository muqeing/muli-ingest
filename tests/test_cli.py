import pytest

from muli_ingest.cli import _bind_host


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_bind_is_allowed(host):
    assert _bind_host(host, False, {}) == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.50.25", "example.com"])
def test_non_loopback_bind_is_rejected_by_default(host):
    with pytest.raises(ValueError):
        _bind_host(host, False, {})


def test_container_all_interface_bind_requires_both_gates():
    with pytest.raises(ValueError):
        _bind_host("0.0.0.0", True, {})
    with pytest.raises(ValueError):
        _bind_host("0.0.0.0", False, {"MULI_INGEST_CONTAINER_LOOPBACK": "1"})
    assert (
        _bind_host("0.0.0.0", True, {"MULI_INGEST_CONTAINER_LOOPBACK": "1"})
        == "0.0.0.0"
    )


def test_container_gate_never_allows_lan_address():
    with pytest.raises(ValueError):
        _bind_host("192.168.50.25", True, {"MULI_INGEST_CONTAINER_LOOPBACK": "1"})


def test_lan_access_explicitly_allows_private_or_all_interface_bind():
    assert _bind_host("0.0.0.0", False, {}, lan_access=True) == "0.0.0.0"
    assert _bind_host("192.168.50.25", False, {}, lan_access=True) == "192.168.50.25"
    with pytest.raises(ValueError):
        _bind_host("8.8.8.8", False, {}, lan_access=True)
