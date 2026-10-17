"""Keep the independently deployed settings-version guard across this release."""
from unittest.mock import patch
import pytest
from test_engine import engine
from muli_ingest.api import BatchRequest
from muli_ingest.safeio import SafetyError


def scan(engine):
    e, source = engine
    (source/'synthetic.txt').write_bytes(b'synthetic test only')
    sid = e.sources()[0]['source_id']
    snapshot = e.scan(sid, ['.'])
    return e, sid, snapshot


def test_stale_settings_version_cannot_create_a_batch(engine):
    e, sid, result = scan(engine)
    version = e.settings()['version']
    before = e.batches()
    with patch.object(e, '_dispatch') as dispatch:
        with pytest.raises(SafetyError, match='设置已变化'):
            e.start(sid, result['scan_id'], 1, 'stale-settings', expected_settings_version=version+1)
        dispatch.assert_not_called()
    assert e.batches() == before


def test_settings_version_is_part_of_exact_idempotency_scope(engine):
    e, sid, result = scan(engine)
    version = e.settings()['version']
    with patch.object(e, '_dispatch') as dispatch:
        batch = e.start(sid, result['scan_id'], 1, 'scoped-key', expected_settings_version=version)
        again = e.start(sid, result['scan_id'], 1, 'scoped-key', expected_settings_version=version)
        assert again['batch_uid'] == batch['batch_uid']
        assert dispatch.call_count == 1
        with pytest.raises(SafetyError):
            e.start(sid, result['scan_id'], 1, 'scoped-key', expected_settings_version=version+1)


def test_legacy_request_without_version_stays_valid_and_invalid_versions_reject():
    request = dict(source_id='synthetic', scan_id='synthetic-scan', scan_revision=1)
    assert BatchRequest(**request).expected_settings_version is None
    assert BatchRequest(**request, expected_settings_version=1).expected_settings_version == 1
    with pytest.raises(ValueError):
        BatchRequest(**request, expected_settings_version=0)
