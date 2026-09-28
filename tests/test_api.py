from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from muli_ingest.access import LanAccess
from muli_ingest.api import create_app
from muli_ingest.engine import Engine

RCLONE = Path(__file__).resolve().parents[3] / "work" / "tools" / "rclone"


@pytest.fixture
def client(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "demo.txt").write_text("synthetic api fixture\n", encoding="utf-8")
    engine = Engine(tmp_path / "state", tmp_path / "staging", [source], rclone=str(RCLONE), mode="demo")
    app = create_app(engine)
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
    engine.close()


def test_status_is_readable_and_cleanup_is_disabled(client):
    response = client.get("/api/v1/status")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "demo"
    assert body["production_ready"] is False
    assert body["capabilities"]["cleanup_enabled"] is False
    assert body["capabilities"]["auto_cleanup_supported"] is False
    assert body["capabilities"]["purge_supported"] is False
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


def test_source_identity_fields_are_explicit(client):
    source = client.get("/api/v1/sources").json()["sources"][0]
    assert len(source["source_id"]) == 24
    assert len(source["session_id"]) == 32
    assert source["identity_confidence"] == "local_path"
    assert source["volume_uuid"] is None
    assert source["filesystem"] is None
    assert source["mount_id"] is None


def test_loopback_and_write_request_guards(client):
    assert client.get("/api/v1/status", headers={"host": "example.com"}).status_code == 403
    source = client.get("/api/v1/sources").json()["sources"][0]
    path = f"/api/v1/sources/{source['source_id']}/scans"

    missing_header = client.post(path, json={"selected_roots": ["."]})
    assert missing_header.status_code == 403
    assert missing_header.json() == {"detail": "ingest_request_header_required"}

    missing_json = client.post(path, headers={"X-Ingest-Request": "1"}, content="{}")
    assert missing_json.status_code == 415
    assert missing_json.json() == {"detail": "application_json_required"}

    cross_origin = client.post(
        path,
        headers={"X-Ingest-Request": "1", "Origin": "http://evil.example"},
        json={"selected_roots": ["."]},
    )
    assert cross_origin.status_code == 403


def test_scan_and_retention_preview_are_readable_but_execution_is_disabled(client):
    source = client.get("/api/v1/sources").json()["sources"][0]
    path = f"/api/v1/sources/{source['source_id']}/scans"
    response = client.post(path, headers={"X-Ingest-Request": "1"}, json={"selected_roots": ["."]})
    assert response.status_code == 200
    assert response.json()["file_count"] == 1

    response = client.get("/api/v1/retention/overview")
    assert response.status_code == 200
    assert response.json()["cleanup_enabled"] is False
    response = client.post(
        "/api/v1/cleanup/previews",
        headers={"X-Ingest-Request": "1"},
        json={},
    )
    assert response.status_code == 200
    assert response.json()["action"] == "trash"
    response = client.post(
        "/api/v1/cleanup/runs",
        headers={"X-Ingest-Request": "1"},
        json={"plan_id": response.json()["plan_id"], "confirmed": True},
    )
    assert response.status_code == 501
    assert response.json()["detail"].startswith("cleanup_runtime_disabled")


def test_lan_access_requires_signed_http_only_session(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    engine = Engine(tmp_path / "state", tmp_path / "staging", [source], rclone=str(RCLONE), mode="demo")
    access = LanAccess("correct-horse-battery-staple")
    app = create_app(engine, lan_access=access, allowed_hosts=("ingest.local",))
    try:
        with TestClient(app, base_url="http://192.168.50.25") as lan_client:
            assert lan_client.get("/api/v1/health").json() == {"status": "ok"}
            session = lan_client.get("/api/v1/session")
            assert session.json()["required"] is True
            assert session.json()["authenticated"] is False
            assert lan_client.get("/api/v1/status").status_code == 401

            missing_guard = lan_client.post(
                "/api/v1/session",
                json={"access_code": "correct-horse-battery-staple"},
            )
            assert missing_guard.status_code == 403
            bad = lan_client.post(
                "/api/v1/session",
                headers={"X-Ingest-Request": "1"},
                json={"access_code": "wrong-access-code"},
            )
            assert bad.status_code == 401
            assert "correct-horse" not in bad.text

            login = lan_client.post(
                "/api/v1/session",
                headers={"X-Ingest-Request": "1", "Origin": "http://192.168.50.25"},
                json={"access_code": "correct-horse-battery-staple"},
            )
            assert login.status_code == 200
            cookie = login.headers["set-cookie"].lower()
            assert "httponly" in cookie
            assert "samesite=strict" in cookie
            assert "correct-horse" not in cookie
            assert lan_client.get("/api/v1/status").status_code == 200
            replay_cookie = login.cookies.get(access.cookie_name)

            logout = lan_client.request(
                "DELETE",
                "/api/v1/session",
                headers={"X-Ingest-Request": "1", "Origin": "http://192.168.50.25"},
                json={},
            )
            assert logout.status_code == 200
            assert lan_client.get("/api/v1/status").status_code == 401
            with TestClient(app, base_url="http://192.168.50.25") as replay_client:
                assert replay_client.get(
                    "/api/v1/status",
                    headers={"Cookie": f"{access.cookie_name}={replay_cookie}"},
                ).status_code == 401
    finally:
        engine.close()


def test_lan_host_scope_and_login_rate_limit(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    engine = Engine(tmp_path / "state", tmp_path / "staging", [source], rclone=str(RCLONE), mode="demo")
    access = LanAccess("sixteen-byte-code", failure_limit=2)
    app = create_app(engine, lan_access=access, allowed_hosts=("ingest.local",))
    headers = {"X-Ingest-Request": "1"}
    try:
        with TestClient(app, base_url="http://public.example") as public_client:
            assert public_client.get("/api/v1/session").status_code == 403
        with TestClient(app, base_url="http://ingest.local") as named_client:
            assert named_client.get("/api/v1/session").status_code == 200
        with TestClient(app, base_url="http://192.168.50.25") as lan_client:
            assert lan_client.post(
                "/api/v1/session", headers=headers, json={"access_code": "bad-one"}
            ).status_code == 401
            assert lan_client.post(
                "/api/v1/session", headers=headers, json={"access_code": "bad-two"}
            ).status_code == 401
            limited = lan_client.post(
                "/api/v1/session", headers=headers, json={"access_code": "sixteen-byte-code"}
            )
            assert limited.status_code == 429
            assert int(limited.headers["retry-after"]) > 0
    finally:
        engine.close()
