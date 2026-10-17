import io
import json
import threading
import time
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from muli_ingest.remote_cli import main


def test_client_interrupt_is_structured_and_does_not_cancel(monkeypatch):
    def interrupted(_args):
        raise KeyboardInterrupt
    monkeypatch.setattr("muli_ingest.remote_cli._dispatch", interrupted)
    code, envelope = invoke("health")
    assert code == 130
    assert envelope["error"]["code"] == "interrupted"


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: ClassVar[list] = []
    states: ClassVar[list[str]] = ["QUEUED", "COPYING", "COMPLETED"]
    require_auth = False
    redirect = False
    slow = False
    slow_post = False
    bad_json = False
    session_body = None
    post_status = 202
    version_guard = False

    def log_message(self, *_args):
        return

    def _write(self, status, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.slow:
            time.sleep(0.2)
        self.requests.append(("GET", self.path, dict(self.headers)))
        if self.redirect:
            self.send_response(302)
            self.send_header("Location", "/api/v1/health")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.bad_json:
            raw = b"{bad-json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.require_auth and self.path not in {"/api/v1/health", "/api/v1/session"} and "muli_ingest_session=ok" not in self.headers.get("Cookie", ""):
            self._write(401, {"detail": "authentication_required"})
            return
        if self.path == "/api/v1/health":
            self._write(200, {"status": "ok"})
        elif self.path == "/api/v1/session":
            self._write(200, self.session_body if self.session_body is not None else {"required": self.require_auth, "authenticated": (not self.require_auth) or "muli_ingest_session=ok" in self.headers.get("Cookie", "")})
        elif self.path == "/api/v1/status":
            self._write(200, {"capabilities": {"settings_version_guard": self.version_guard}})
        elif self.path == "/api/v1/settings":
            self._write(200, {"version": 5, "values": {}})
        elif self.path.startswith("/api/v1/batches/"):
            state = self.states.pop(0) if len(self.states) > 1 else (self.states[0] if self.states else "COMPLETED")
            self._write(200, {"batch_id": "b1", "state": state})
        else:
            self._write(200, {"ok": True})

    def do_POST(self):
        if self.slow_post:
            time.sleep(0.2)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.requests.append(("POST", self.path, dict(self.headers), body))
        if self.headers.get("X-Ingest-Request") != "1" or self.headers.get_content_type() != "application/json":
            self._write(403, {"detail": "guard"})
            return
        if self.path == "/api/v1/session":
            self._write(200, {"authenticated": True}, {"Set-Cookie": "muli_ingest_session=ok; HttpOnly"})
        else:
            self._write(self.post_status, {"detail": "synthetic_failure"} if self.post_status >= 500 else {"state": "QUEUED"})


@pytest.fixture
def server():
    ApiHandler.requests = []
    ApiHandler.states = ["QUEUED", "COPYING", "COMPLETED"]
    ApiHandler.require_auth = False
    ApiHandler.redirect = False
    ApiHandler.slow = False
    ApiHandler.slow_post = False
    ApiHandler.bad_json = False
    ApiHandler.session_body = None
    ApiHandler.post_status = 202
    ApiHandler.version_guard = False
    value = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
    thread = threading.Thread(target=value.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{value.server_port}"
    finally:
        value.shutdown()
        thread.join(timeout=2)


def invoke(*args):
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(list(args))
    return code, json.loads(output.getvalue())


def test_dry_run_never_contacts_server(server, tmp_path):
    request = tmp_path / "request.json"
    request.write_text('{"source_id":"s","scan_id":"scan","scan_revision":1}', encoding="utf-8")
    code, envelope = invoke("--url", server, "start", "--idempotency-key", "key-1", "--request", str(request))
    assert code == 0
    assert envelope["data"]["executed"] is False
    assert ApiHandler.requests == []


def test_post_headers_and_idempotency(server, tmp_path):
    request = tmp_path / "request.json"
    request.write_text('{"source_id":"s","scan_id":"scan","scan_revision":1}', encoding="utf-8")
    code, _ = invoke("--url", server, "start", "--idempotency-key", "key-1", "--request", str(request), "--execute")
    assert code == 0
    method, path, headers, _body = ApiHandler.requests[-1]
    assert (method, path) == ("POST", "/api/v1/batches")
    assert headers["X-Ingest-Request"] == "1"
    assert headers["Idempotency-Key"] == "key-1"
    assert headers["Origin"] == server
    assert ApiHandler.requests[0][2]["Origin"] == server


def test_session_uses_named_env_once_without_echoing_secret(server, monkeypatch):
    ApiHandler.require_auth = True
    monkeypatch.setenv("TEST_INGEST_CODE", "synthetic-secret")
    code, envelope = invoke("--url", server, "--access-code-env", "TEST_INGEST_CODE", "status")
    assert code == 0
    assert envelope["ok"]
    assert [item[0] for item in ApiHandler.requests] == ["GET", "POST", "GET"]
    assert "synthetic-secret" not in json.dumps(envelope)


def test_session_without_env_is_read_only_auth_diagnosis(server):
    ApiHandler.require_auth = True
    code, envelope = invoke("--url", server, "session")
    assert code == 0
    assert envelope["data"]["required"] is True
    assert envelope["data"]["authenticated"] is False
    assert [item[0] for item in ApiHandler.requests] == ["GET"]


def test_redirect_and_timeout_are_network_errors(server):
    ApiHandler.redirect = True
    code, envelope = invoke("--url", server, "health")
    assert code == 3
    assert envelope["error"]["details"]["http_status"] == 302
    ApiHandler.redirect = False
    ApiHandler.slow = True
    code, envelope = invoke("--url", server, "--timeout", "0.01", "health")
    assert code == 3


def test_wait_returns_completion_and_failed_states(server):
    code, envelope = invoke("--url", server, "wait", "b1", "--interval", "0.001")
    assert code == 0
    assert envelope["data"]["state"] == "COMPLETED"
    ApiHandler.states = ["INTERRUPTED"]
    code, envelope = invoke("--url", server, "wait", "b1", "--interval", "0.001")
    assert code == 4
    assert envelope["error"]["code"] == "batch_failed"


def test_strict_start_json_and_wait_timeout(server, tmp_path):
    request = tmp_path / "request.json"
    request.write_text('{"source_id":"s","source_id":"duplicate","scan_id":"scan","scan_revision":1}', encoding="utf-8")
    code, envelope = invoke("--url", server, "start", "--idempotency-key", "key", "--request", str(request))
    assert code == 2
    assert envelope["error"]["code"] == "usage_error"
    ApiHandler.states = ["COPYING"]
    code, envelope = invoke("--url", server, "wait", "b1", "--deadline", "0.01", "--interval", "0.01")
    assert code == 5
    assert envelope["error"]["code"] == "wait_timeout"


def test_post_server_failure_is_unknown_and_is_not_retried(server, tmp_path):
    request = tmp_path / "request.json"
    request.write_text('{"source_id":"s","scan_id":"scan","scan_revision":1}', encoding="utf-8")
    ApiHandler.post_status = 500
    code, envelope = invoke("--url", server, "start", "--idempotency-key", "key-500", "--request", str(request), "--execute")
    assert code == 3
    details = envelope["error"]["details"]
    assert details["result_unknown"] is True
    assert details["automatic_retry"] is False
    assert details["method"] == "POST"
    assert details["path"] == "/api/v1/batches"
    assert details["idempotency_key"] == "key-500"
    assert [item[0] for item in ApiHandler.requests].count("POST") == 1


def test_malformed_session_and_response_are_network_errors_without_post(server):
    ApiHandler.session_body = {"required": "yes", "authenticated": False}
    code, envelope = invoke("--url", server, "status")
    assert code == 3
    assert envelope["error"]["details"]["reason"] == "invalid_session_schema"
    assert not any(item[0] == "POST" for item in ApiHandler.requests)
    ApiHandler.session_body = None
    ApiHandler.bad_json = True
    code, envelope = invoke("--url", server, "health")
    assert code == 3
    assert envelope["error"]["details"]["reason"] == "invalid_json_response"


def test_unknown_wait_state_and_invalid_interval_are_failures(server):
    ApiHandler.states = ["NOT_A_STATE"]
    code, envelope = invoke("--url", server, "wait", "b1", "--interval", "0.001")
    assert code == 4
    assert envelope["error"]["code"] == "operation_failed"
    code, envelope = invoke("--url", server, "wait", "b1", "--interval", "0")
    assert code == 2
    assert envelope["error"]["code"] == "usage_error"


def test_version_flag_and_remote_version(server):
    code, envelope = invoke("--version")
    assert code == 0
    assert envelope["command"] == "version"
    code, envelope = invoke("--url", server, "version", "--remote")
    assert code == 3
    # The synthetic status response intentionally lacks a version field.
    assert envelope["error"]["details"]["reason"] == "invalid_status_schema"


def test_settings_read_command(server):
    code, result = invoke('--url', server, 'settings')
    assert code == 0 and result['data']['version'] == 5
    assert all(r[0] == 'GET' for r in ApiHandler.requests)

@pytest.mark.parametrize('version',[True,False,0,-1,None,'5',1.5])
def test_invalid_expected_settings_version_no_network(server,tmp_path,version):
    p=tmp_path/'request.json'
    p.write_text(json.dumps({'source_id':'s','scan_id':'scan','scan_revision':1,'expected_settings_version':version}))
    code,_=invoke('--url',server,'start','--request',str(p),'--idempotency-key','one','--execute')
    assert code == 2 and not ApiHandler.requests

@pytest.mark.parametrize('supported',[False,True])
def test_guarded_start_requires_server_support(server,tmp_path,supported):
    ApiHandler.version_guard=supported
    body={'source_id':'s','scan_id':'scan','scan_revision':1,'expected_settings_version':5}
    p=tmp_path/'request.json';p.write_text(json.dumps(body))
    code,_=invoke('--url',server,'start','--request',str(p),'--idempotency-key','one','--execute')
    posts=[r for r in ApiHandler.requests if r[0]=='POST']
    assert code == (0 if supported else 3)
    assert len(posts) == int(supported)
    if supported:assert json.loads(posts[0][3]) == body
