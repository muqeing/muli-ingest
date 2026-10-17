"""Small, bounded JSON CLI for the local Muli Ingest HTTP API.

The client deliberately keeps transport and operation policy here rather than
sharing the server's CLI.  It is intended for scripts and AI callers: every
invocation emits one JSON envelope and writes never happen without
``--execute``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import __version__

SCHEMA_VERSION = 1
CLI_VERSION = __version__
SERVICE = "muli-ingest"
DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT = 10.0
DEFAULT_BODY_CAP = 4 * 1024 * 1024
_API = "/api/v1"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _visible_header(value: str) -> bool:
    return all(0x20 <= ord(char) != 0x7F and not char.isspace() for char in value)


class CliError(Exception):
    """An error with a stable CLI exit code and JSON-safe details."""

    exit_code = 4
    code = "operation_failed"

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details


class UsageError(CliError):
    exit_code = 2
    code = "usage_error"


class TransportError(CliError):
    exit_code = 3
    code = "network_error"


class WaitTimeout(CliError):
    exit_code = 5
    code = "wait_timeout"


class _ArgumentParser(argparse.ArgumentParser):
    """Turn argparse diagnostics into the same JSON envelope as other errors."""

    def error(self, message: str) -> None:
        raise UsageError("invalid command line", message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, new):  # type: ignore[no-untyped-def]
        return None


class _DuplicateKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _json_loads(value: str | bytes) -> Any:
    try:
        return json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise UsageError("invalid JSON", str(exc)) from exc


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise UsageError("request is not valid JSON", str(exc)) from exc


def _read_json_file(filename: str, cap: int) -> Any:
    if filename == "-":
        raw = sys.stdin.buffer.read(cap + 1)
    else:
        try:
            path = Path(filename)
            if path.stat().st_size > cap:
                raise UsageError("request exceeds body cap", {"body_cap": cap})
            with path.open("rb") as stream:
                raw = stream.read(cap + 1)
        except OSError as exc:
            raise UsageError("cannot read request file", str(exc)) from exc
    if len(raw) > cap:
        raise UsageError("request exceeds body cap", {"body_cap": cap})
    return _json_loads(raw)


def _validate_url(value: str) -> str:
    if not isinstance(value, str) or not _visible_header(value):
        raise UsageError("--url must be a valid http(s) origin")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UsageError("--url must be a valid http(s) origin", str(exc)) from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise UsageError("--url must use http or https and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise UsageError("--url must not contain userinfo")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"} or ";" in parsed.path:
        raise UsageError("--url must not contain path, query, or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise UsageError("--url port is out of range")
    return value.rstrip("/")


def _validate_identifier(value: str, name: str = "id") -> str:
    if (
        not value
        or value in {".", ".."}
        or any(char in value for char in "/\\\x00")
        or not _visible_header(value)
    ):
        raise UsageError(f"invalid {name}")
    return value


def _path_identifier(value: str, name: str = "id") -> str:
    return quote(_validate_identifier(value, name), safe="")


def _error_detail(raw: bytes, status: int) -> dict[str, Any]:
    """Extract a small server detail without ever dumping arbitrary response bodies."""
    detail: Any = None
    try:
        parsed = _json_loads(raw)
        if isinstance(parsed, dict):
            detail = parsed.get("detail")
    except UsageError:
        pass
    if not isinstance(detail, (str, int, float, bool, type(None))):
        detail = None
    return {"http_status": status, "detail": detail}


class Client:
    def __init__(self, base_url: str, timeout: float, body_cap: int):
        self.base_url = _validate_url(base_url)
        if not 0 < timeout <= 300:
            raise UsageError("--timeout must be between 0 and 300 seconds")
        if not 1024 <= body_cap <= 64 * 1024 * 1024:
            raise UsageError("--body-cap must be between 1024 and 67108864 bytes")
        self.timeout = timeout
        self.body_cap = body_cap
        self.cookie: str | None = None
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _url(self, path: str) -> str:
        if not path.startswith(_API + "/"):
            raise UsageError("invalid API path")
        return self.base_url + path

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        data = None if body is None else _json_bytes(body)
        headers = {
            "Accept": "application/json",
            "Origin": self.base_url,
            "User-Agent": f"muli-ingest-client/{CLI_VERSION}",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        if data is not None:
            headers["Content-Type"] = "application/json"
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            headers["X-Ingest-Request"] = "1"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(self._url(path), data=data, headers=headers, method=method)
        request_timeout = self.timeout if timeout is None else timeout
        operation_details = {
            "method": method,
            "path": path,
        }
        if idempotency_key is not None:
            operation_details["idempotency_key"] = idempotency_key
        try:
            response = self._opener.open(request, timeout=request_timeout)
        except HTTPError as exc:
            try:
                raw = exc.read(self.body_cap + 1)
                detail = _error_detail(raw, exc.code)
            except (OSError, TimeoutError, HTTPException) as read_exc:
                detail = {
                    **operation_details,
                    "http_status": exc.code,
                    "reason": str(read_exc) or read_exc.__class__.__name__,
                    "result_unknown": True,
                    "automatic_retry": False,
                }
            finally:
                exc.close()
            if method == "POST" and "result_unknown" not in detail:
                detail = {**operation_details, **detail, "result_unknown": exc.code >= 500, "automatic_retry": False}
            raise TransportError("HTTP request failed", detail) from exc
        except (TimeoutError, OSError, URLError) as exc:
            detail = {**operation_details, "reason": str(exc) or exc.__class__.__name__}
            if method == "POST":
                detail.update(result_unknown=True, automatic_retry=False)
            raise TransportError("HTTP request failed", detail) from exc
        try:
            try:
                raw = response.read(self.body_cap + 1)
            except (OSError, TimeoutError, HTTPException) as exc:
                detail = {**operation_details, "reason": str(exc) or exc.__class__.__name__}
                if method == "POST":
                    detail.update(result_unknown=True, automatic_retry=False)
                raise TransportError("response read failed", detail) from exc
            if len(raw) > self.body_cap:
                detail = {**operation_details, "body_cap": self.body_cap}
                if method == "POST":
                    detail.update(result_unknown=True, automatic_retry=False)
                raise TransportError("response exceeds body cap", detail)
            set_cookie = response.headers.get("Set-Cookie", "")
            if set_cookie:
                value = set_cookie.split(";", 1)[0].strip()
                if value.startswith("muli_ingest_session="):
                    self.cookie = value
            content_type = response.headers.get_content_type()
        finally:
            response.close()
        if not raw:
            detail = {**operation_details, "reason": "empty_response"}
            if method == "POST":
                detail.update(result_unknown=True, automatic_retry=False)
            raise TransportError("invalid empty response", detail)
        if content_type in {"application/json", "application/problem+json"} or raw[:1] in {b"{", b"["}:
            try:
                return _json_loads(raw)
            except UsageError as exc:
                detail = {**operation_details, "reason": "invalid_json_response"}
                if method == "POST":
                    detail.update(result_unknown=True, automatic_retry=False)
                raise TransportError("invalid JSON response", detail) from exc
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            detail = {**operation_details, "reason": "invalid_utf8_response"}
            if method == "POST":
                detail.update(result_unknown=True, automatic_retry=False)
            raise TransportError("response is not UTF-8 JSON or text", detail) from exc

    @staticmethod
    def _validate_session_state(state: Any) -> dict[str, bool]:
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("required"), bool)
            or not isinstance(state.get("authenticated"), bool)
            or (not state["required"] and not state["authenticated"])
        ):
            raise TransportError("malformed session response", {"reason": "invalid_session_schema"})
        return {"required": state["required"], "authenticated": state["authenticated"]}

    def _login_if_needed(self, state: Any, access_code_env: str | None, *, fail_without_credentials: bool) -> None:
        state = self._validate_session_state(state)
        if not state["required"] or state["authenticated"]:
            return
        if not access_code_env and not fail_without_credentials:
            return
        if not access_code_env:
            raise TransportError("authentication required", {"http_status": 401, "detail": "authentication_required"})
        if not _ENV_NAME.fullmatch(access_code_env):
            raise UsageError("--access-code-env must be an environment variable name")
        access_code = os.environ.get(access_code_env)
        if not access_code:
            raise UsageError("referenced access code environment variable is unset")
        # The value is scoped to this request and is never included in an envelope,
        # exception, log, or cookie output.
        self.request("POST", f"{_API}/session", {"access_code": access_code})

    def ensure_session(self, access_code_env: str | None) -> None:
        self._login_if_needed(self.request("GET", f"{_API}/session"), access_code_env, fail_without_credentials=True)

    def session(self, access_code_env: str | None) -> Any:
        state = self.request("GET", f"{_API}/session")
        self._login_if_needed(state, access_code_env, fail_without_credentials=False)
        if access_code_env and isinstance(state, dict) and state.get("required") and not state.get("authenticated"):
            return self.request("GET", f"{_API}/session")
        return state


def _envelope(command: str, *, ok: bool, data: Any = None, error: CliError | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "service": SERVICE,
        "command": command,
        "cli_version": CLI_VERSION,
    }
    if ok:
        result["data"] = data
    else:
        result["error"] = {
            "code": error.code if error else "operation_failed",
            "message": error.message if error else "operation failed",
            "details": error.details if error else None,
        }
    return result


def _command_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="muli-ingest-client", add_help=False)
    parser.add_argument("--url", default=os.environ.get("MULI_INGEST_URL", DEFAULT_URL))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--body-cap", type=int, default=DEFAULT_BODY_CAP)
    parser.add_argument("--access-code-env")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    commands = parser.add_subparsers(dest="command", parser_class=_ArgumentParser)
    commands.add_parser("help", add_help=False)
    commands.add_parser("schema", add_help=False)
    version = commands.add_parser("version", add_help=False)
    version.add_argument("--remote", action="store_true")
    for name in ("health", "status", "session", "sources", "batches", "settings"):
        commands.add_parser(name, add_help=False)
    for name in ("batch", "files"):
        sub = commands.add_parser(name, add_help=False)
        sub.add_argument("batch_id")
    manifest = commands.add_parser("manifest", add_help=False)
    manifest.add_argument("batch_id")
    manifest.add_argument("--format", choices=("md", "json"), default="md")
    scan = commands.add_parser("scan", add_help=False)
    scan.add_argument("--source-id", required=True)
    scan.add_argument("--root", action="append", dest="roots")
    scan.add_argument("--execute", action="store_true")
    start = commands.add_parser("start", add_help=False)
    start.add_argument("--idempotency-key", required=True)
    start.add_argument("--request", required=True)
    start.add_argument("--execute", action="store_true")
    for name in ("interrupt", "resume"):
        sub = commands.add_parser(name, add_help=False)
        sub.add_argument("batch_id")
        sub.add_argument("--execute", action="store_true")
    wait = commands.add_parser("wait", add_help=False)
    wait.add_argument("batch_id")
    wait.add_argument("--deadline", type=float, default=300.0)
    wait.add_argument("--interval", type=float, default=1.0)
    for sub in commands.choices.values():
        sub.add_argument("--url", default=argparse.SUPPRESS)
        sub.add_argument("--timeout", type=float, default=argparse.SUPPRESS)
        sub.add_argument("--body-cap", type=int, default=argparse.SUPPRESS)
        sub.add_argument("--access-code-env", default=argparse.SUPPRESS)
    return parser


def _schema() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "api_contract_version": 1,
        "envelope": {"schema_version": 1, "ok": True, "service": SERVICE, "command": "...", "cli_version": CLI_VERSION, "data": "..."},
        "exit_codes": {"success": 0, "usage": 2, "network_or_http": 3, "operation_failed": 4, "wait_timeout": 5},
        "start_request": {"fields": ["source_id", "scan_id", "scan_revision"], "optional_fields": ["expected_settings_version"], "extra_fields": "rejected", "scan_revision_minimum": 1},
        "commands": {
            "help": "local command help",
            "schema": "print this schema",
            "version": "print client and protocol versions",
            "health": "GET /api/v1/health",
            "status": "GET /api/v1/status",
            "session": "GET /api/v1/session (and login when --access-code-env is required)",
            "sources": "GET /api/v1/sources",
            "settings": "GET /api/v1/settings",
            "batches": "GET /api/v1/batches",
            "batch": "GET /api/v1/batches/{batch_id}",
            "files": "GET /api/v1/batches/{batch_id}/files",
            "manifest": "GET /api/v1/batches/{batch_id}/manifest",
            "scan": "POST /api/v1/sources/{source_id}/scans; dry-run unless --execute",
            "start": "POST /api/v1/batches; dry-run unless --execute",
            "interrupt": "POST /api/v1/batches/{batch_id}/interrupt; dry-run unless --execute",
            "resume": "POST /api/v1/batches/{batch_id}/resume; dry-run unless --execute",
            "wait": "poll GET /api/v1/batches/{batch_id} until a terminal state or deadline",
        },
    }


def _dry_run(method: str, path: str, body: Any) -> dict[str, Any]:
    return {"executed": False, "method": method, "path": path, "body": body}


def _validate_batch_request(value: Any) -> dict[str, Any]:
    required = {"source_id", "scan_id", "scan_revision"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"expected_settings_version"}:
        raise UsageError("start request must contain exactly BatchRequest fields", {"fields": ["source_id", "scan_id", "scan_revision"]})
    if not isinstance(value["source_id"], str) or not value["source_id"]:
        raise UsageError("source_id must be a non-empty string")
    if not isinstance(value["scan_id"], str) or not value["scan_id"]:
        raise UsageError("scan_id must be a non-empty string")
    if isinstance(value["scan_revision"], bool) or not isinstance(value["scan_revision"], int):
        raise UsageError("scan_revision must be an integer")
    if value["scan_revision"] < 1:
        raise UsageError("scan_revision must be at least 1")
    if "expected_settings_version" in value:
        version = value["expected_settings_version"]
        if type(version) is not int or version < 1:
            raise UsageError("expected_settings_version must be a positive integer")
    return value


def _validate_idempotency(value: str) -> str:
    if not value or len(value) > 200 or not _visible_header(value):
        raise UsageError("--idempotency-key must be 1 to 200 visible characters")
    return value


def _wait(client: Client, batch_id: str, deadline: float, interval: float) -> dict[str, Any]:
    if not math.isfinite(deadline) or not 0 < deadline <= 3600:
        raise UsageError("--deadline must be between 0 and 3600 seconds")
    if not math.isfinite(interval) or not 0 < interval <= 60:
        raise UsageError("--interval must be greater than 0 and at most 60 seconds")
    end = time.monotonic() + deadline
    last: Any = None
    known = {"QUEUED", "PREPARING", "COPYING", "VERIFYING", "FINALIZING", "RECOVERING", "RUNNING", "PENDING"}
    terminal = {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise WaitTimeout("wait deadline exceeded", {"state": last.get("state") if isinstance(last, dict) else None, "batch": last})
        try:
            last = client.request(
                "GET",
                f"{_API}/batches/{_path_identifier(batch_id, 'batch_id')}",
                timeout=min(client.timeout, remaining),
            )
        except TransportError as exc:
            if time.monotonic() >= end:
                raise WaitTimeout("wait deadline exceeded", {"batch": last, "cause": exc.details}) from exc
            raise
        if not isinstance(last, dict) or not isinstance(last.get("state"), str):
            raise CliError("batch response has no valid state", {"batch_id": batch_id})
        state = last["state"]
        if state in terminal:
            result = {"state": state, "terminal": True, "batch": last}
            if state != "COMPLETED":
                failure = CliError("batch did not complete", {"state": state, "batch": last})
                failure.exit_code = 4
                failure.code = "batch_failed"
                raise failure
            return result
        if state not in known:
            raise CliError("unknown batch state", {"state": state, "batch": last})
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise WaitTimeout("wait deadline exceeded", {"state": state, "batch": last})
        time.sleep(min(interval, remaining))


def _dispatch(args: argparse.Namespace) -> tuple[Any, int]:
    command = args.command or "help"
    if args.version:
        command = "version"
        return {"cli_version": CLI_VERSION, "schema_version": SCHEMA_VERSION, "service": SERVICE}, 0
    if args.help or command == "help":
        return {"usage": "muli-ingest-client [global options] <command>", "commands": _schema()["commands"]}, 0
    if command == "schema":
        return _schema(), 0
    if command == "version":
        if not args.remote:
            return {"cli_version": CLI_VERSION, "schema_version": SCHEMA_VERSION, "service": SERVICE}, 0
        client = Client(args.url, args.timeout, args.body_cap)
        client.ensure_session(args.access_code_env)
        status = client.request("GET", f"{_API}/status")
        if not isinstance(status, dict) or not isinstance(status.get("version"), str) or not status["version"]:
            raise TransportError("malformed remote version response", {"reason": "invalid_status_schema"})
        return {
            "cli_version": CLI_VERSION,
            "schema_version": SCHEMA_VERSION,
            "service": SERVICE,
            "remote_version": status["version"],
        }, 0
    client = Client(args.url, args.timeout, args.body_cap)
    if command == "health":
        return client.request("GET", f"{_API}/health"), 0
    if command == "session":
        return client.session(args.access_code_env), 0
    reads = {
        "status": f"{_API}/status",
        "sources": f"{_API}/sources",
        "settings": f"{_API}/settings",
        "batches": f"{_API}/batches",
    }
    if command in reads:
        client.ensure_session(args.access_code_env)
        return client.request("GET", reads[command]), 0
    if command in {"batch", "files"}:
        path = f"{_API}/batches/{_path_identifier(args.batch_id, 'batch_id')}"
        if command == "files":
            path += "/files"
        client.ensure_session(args.access_code_env)
        return client.request("GET", path), 0
    if command == "manifest":
        batch = _path_identifier(args.batch_id, "batch_id")
        path = f"{_API}/batches/{batch}/manifest?{urlencode({'format': args.format})}"
        client.ensure_session(args.access_code_env)
        return client.request("GET", path), 0
    if command == "scan":
        path = f"{_API}/sources/{_path_identifier(args.source_id, 'source_id')}/scans"
        body = {"selected_roots": args.roots if args.roots is not None else ["."]}
        if not body["selected_roots"] or any(not isinstance(root, str) or not root for root in body["selected_roots"]):
            raise UsageError("--root values must be non-empty strings")
        if not args.execute:
            return _dry_run("POST", path, body), 0
        client.ensure_session(args.access_code_env)
        return client.request("POST", path, body), 0
    if command == "start":
        body = _validate_batch_request(_read_json_file(args.request, client.body_cap))
        key = _validate_idempotency(args.idempotency_key)
        path = f"{_API}/batches"
        if not args.execute:
            return _dry_run("POST", path, body) | {"idempotency_key": key}, 0
        client.ensure_session(args.access_code_env)
        if "expected_settings_version" in body:
            status = client.request("GET", f"{_API}/status")
            if status.get("capabilities", {}).get("settings_version_guard") is not True:
                raise TransportError("server_upgrade_required", {"reason": "settings_version_guard_unavailable"})
        return client.request("POST", path, body, idempotency_key=key), 0
    if command in {"interrupt", "resume"}:
        batch = _path_identifier(args.batch_id, "batch_id")
        path = f"{_API}/batches/{batch}/{command}"
        body: dict[str, Any] = {}
        if not args.execute:
            return _dry_run("POST", path, body), 0
        client.ensure_session(args.access_code_env)
        return client.request("POST", path, body), 0
    if command == "wait":
        _validate_identifier(args.batch_id, "batch_id")
        client.ensure_session(args.access_code_env)
        return _wait(client, args.batch_id, args.deadline, args.interval), 0
    raise UsageError(f"unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    command = "help"
    try:
        parser = _command_parser()
        args = parser.parse_args(argv)
        command = "version" if args.version else (args.command or "help")
        data, _ = _dispatch(args)
        print(json.dumps(_envelope(command, ok=True, data=data), ensure_ascii=False, separators=(",", ":")))
        return 0
    except SystemExit as exc:
        # argparse's own exit is kept JSON-shaped for AI callers.
        error = UsageError("invalid command line")
        print(json.dumps(_envelope(command, ok=False, error=error), ensure_ascii=False, separators=(",", ":")))
        return 2 if exc.code else 0
    except CliError as exc:
        print(json.dumps(_envelope(command, ok=False, error=exc), ensure_ascii=False, separators=(",", ":")))
        return exc.exit_code
    except KeyboardInterrupt:
        error = CliError("client interrupted; the server operation was not cancelled")
        error.code = "interrupted"
        print(json.dumps(_envelope(command, ok=False, error=error), ensure_ascii=False, separators=(",", ":")))
        return 130
    except Exception as exc:  # noqa: BLE001 - keep the public CLI envelope stable
        error = CliError("unexpected client error", {"type": exc.__class__.__name__})
        print(json.dumps(_envelope(command, ok=False, error=error), ensure_ascii=False, separators=(",", ":")))
        return error.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
