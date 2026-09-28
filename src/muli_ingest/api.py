"""Loopback-only HTTP API for the local ingest engine."""

from __future__ import annotations

import ipaddress
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import __version__
from .access import LanAccess
from .engine import Engine
from .retention import Retention
from .safeio import SafetyError


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_roots: list[str] = Field(default_factory=lambda: ["."])


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    scan_id: str
    scan_revision: int = 1


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int
    values: dict[str, Any] = Field(default_factory=dict)
    confirm_policy_change: bool = False


class CleanupRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str
    confirmed: bool


class ConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_code: str = Field(min_length=1, max_length=256)


def _host_parts(value: str) -> tuple[str, int | None]:
    """Parse a Host or Origin authority without resolving DNS names."""
    parsed = urlsplit(f"//{value}")
    if not parsed.hostname:
        raise ValueError("missing host")
    return parsed.hostname.lower().rstrip("."), parsed.port


def _is_loopback_name(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value == "localhost"


def _is_private_lan_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(
        address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
    )


def _effective_port(scheme: str, port: int | None) -> int | None:
    return port if port is not None else {"http": 80, "https": 443}.get(scheme.lower())


def _same_origin(request: Request, origin: str) -> bool:
    try:
        origin_parts = urlsplit(origin)
        if origin_parts.scheme not in {"http", "https"} or not origin_parts.hostname:
            return False
        host, host_port = _host_parts(request.headers.get("host", ""))
        origin_host = origin_parts.hostname.lower().rstrip(".")
        return origin_host == host and _effective_port(
            origin_parts.scheme, origin_parts.port
        ) == _effective_port(request.url.scheme, host_port)
    except ValueError:
        return False


def _error_detail(exc: SafetyError) -> str:
    message = str(exc)
    return exc.code if not message or message == exc.code else f"{exc.code}: {message}"


def _batch_view(batch: dict[str, Any]) -> dict[str, Any]:
    """Keep the engine's useful evidence fields while guaranteeing contract keys."""
    result = dict(batch)
    result.setdefault("source_label", result.get("source_id"))
    result.setdefault("error", None)
    result.setdefault("summary", {})
    result.setdefault("progress", {})
    result.setdefault(
        "lifecycle",
        {
            "storage_state": "ACTIVE",
            "expires_at": None,
            "hold": False,
            "handoff_confirmed": False,
            "blocked_reasons": [],
        },
    )
    return result


def _harden(response):
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def _resolve_batch(engine: Engine, identifier: str) -> dict[str, Any]:
    try:
        return engine.batch(identifier)
    except SafetyError as exc:
        if exc.code != "batch_not_found":
            raise
    for batch in engine.batches():
        if batch.get("batch_id") == identifier or batch.get("batch_uid") == identifier:
            return batch
    raise SafetyError("batch_not_found")


def _unsupported() -> None:
    raise HTTPException(status_code=501, detail="cleanup_retention_not_supported")


def create_app(
    engine: Engine,
    *,
    static_dir: str | Path | None = None,
    lan_access: LanAccess | None = None,
    allowed_hosts: tuple[str, ...] = (),
) -> FastAPI:
    """Create the API around one already-owned Engine instance."""

    app = FastAPI(title="木梨 Ingest", version=__version__)
    app.state.engine = engine
    retention = Retention(engine)
    allowed = {value.lower().rstrip(".") for value in allowed_hosts if value}

    def session_state(request: Request) -> tuple[bool, int | None]:
        if lan_access is None:
            return True, None
        return lan_access.verify_session(request.cookies.get(lan_access.cookie_name))

    @app.middleware("http")
    async def access_guard(request: Request, call_next):
        try:
            host, _ = _host_parts(request.headers.get("host", ""))
        except ValueError:
            return _harden(JSONResponse(status_code=400, content={"detail": "invalid_host"}))
        if not _is_loopback_name(host) and not (
            lan_access is not None and (_is_private_lan_address(host) or host in allowed)
        ):
            return _harden(JSONResponse(status_code=403, content={"detail": "loopback_host_required"}))
        origin = request.headers.get("origin")
        if origin and not _same_origin(request, origin):
            return _harden(JSONResponse(status_code=403, content={"detail": "same_origin_required"}))
        public_api_paths = {"/api/v1/health", "/api/v1/session"}
        if request.url.path.startswith("/api/") and request.url.path not in public_api_paths:
            authenticated, _ = session_state(request)
            if not authenticated:
                return _harden(
                    JSONResponse(status_code=401, content={"detail": "authentication_required"})
                )
        if request.method in {"POST", "PATCH", "PUT", "DELETE"} and request.url.path.startswith("/api/"):
            if request.headers.get("x-ingest-request") != "1":
                return _harden(JSONResponse(status_code=403, content={"detail": "ingest_request_header_required"}))
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return _harden(JSONResponse(status_code=415, content={"detail": "application_json_required"}))
        return _harden(await call_next(request))

    @app.exception_handler(SafetyError)
    async def safety_error_handler(_: Request, exc: SafetyError):
        status = (
            404
            if exc.code in {"batch_not_found", "source_unavailable"}
            else 501
            if exc.code in {"cleanup_runtime_disabled", "purge_not_supported"}
            else 409
            if exc.code
            in {"stale_scan", "source_busy", "version_conflict", "idempotency_conflict", "batch_busy"}
            else 400
        )
        return JSONResponse(status_code=status, content={"detail": _error_detail(exc)})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError):
        fields = ", ".join(str(item.get("loc", ["request"])[-1]) for item in exc.errors())
        return JSONResponse(status_code=422, content={"detail": f"invalid_request: {fields}"})

    @app.exception_handler(ValidationError)
    async def settings_validation_error_handler(_: Request, exc: ValidationError):
        fields = ", ".join(str(item.get("loc", ["settings"])[-1]) for item in exc.errors())
        return JSONResponse(status_code=422, content={"detail": f"invalid_settings: {fields}"})

    api = "/api/v1"

    @app.get(f"{api}/health")
    def health():
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    @app.get(f"{api}/session")
    def get_session(request: Request):
        authenticated, expires_at = session_state(request)
        return JSONResponse(
            {
                "required": lan_access is not None,
                "authenticated": authenticated,
                "lan_enabled": lan_access is not None,
                "secure_transport": request.url.scheme == "https",
                "expires_at": expires_at,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post(f"{api}/session")
    def create_session(request: Request, payload: SessionRequest):
        if lan_access is None:
            raise HTTPException(status_code=400, detail="lan_access_disabled")
        peer = request.client.host if request.client else "unknown"
        valid, retry_after = lan_access.authenticate(payload.access_code, peer)
        if retry_after is not None:
            return JSONResponse(
                status_code=429,
                content={"detail": "too_many_access_attempts"},
                headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
            )
        if not valid:
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid_access_code"},
                headers={"Cache-Control": "no-store"},
            )
        session, expires_at = lan_access.issue_session()
        response = JSONResponse(
            {
                "required": True,
                "authenticated": True,
                "lan_enabled": True,
                "secure_transport": request.url.scheme == "https",
                "expires_at": expires_at,
            },
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            lan_access.cookie_name,
            session,
            max_age=lan_access.session_ttl_seconds,
            httponly=True,
            secure=lan_access.secure_cookie,
            samesite="strict",
            path="/",
        )
        return response

    @app.delete(f"{api}/session")
    def delete_session(request: Request):
        response = JSONResponse(
            {
                "required": lan_access is not None,
                "authenticated": lan_access is None,
                "lan_enabled": lan_access is not None,
                "secure_transport": False,
                "expires_at": None,
            },
            headers={"Cache-Control": "no-store"},
        )
        if lan_access is not None:
            lan_access.revoke_session(request.cookies.get(lan_access.cookie_name))
            response.delete_cookie(
                lan_access.cookie_name,
                path="/",
                secure=lan_access.secure_cookie,
                httponly=True,
                samesite="strict",
            )
        return response

    @app.get(f"{api}/status")
    def status():
        settings = engine.settings()
        rclone = str(engine.copier.executable)
        return {
            "name": "木梨 Ingest",
            "version": __version__,
            "mode": engine.mode,
            "production_ready": False,
            "staging_root": str(engine.staging),
            "tools": {
                "rclone": rclone,
                "exiftool": shutil.which("exiftool"),
                "ffprobe": shutil.which("ffprobe"),
            },
            "capabilities": {
                "cleanup_enabled": bool(engine.allow_cleanup and settings["values"]["cleanup_enabled"]),
                "auto_cleanup_supported": False,
                "purge_supported": False,
            },
            "auto_cleanup_supported": False,
            "purge_supported": False,
            "settings_version": settings["version"],
            "access": {
                "lan_enabled": lan_access is not None,
                "secure_cookie": bool(lan_access and lan_access.secure_cookie),
            },
        }

    @app.get(f"{api}/sources")
    def sources():
        fields = (
            "source_id",
            "session_id",
            "label",
            "path",
            "classification",
            "connected",
            "identity_confidence",
            "volume_uuid",
            "filesystem",
            "mount_id",
        )
        return {"sources": [{key: source.get(key) for key in fields} for source in engine.sources()]}

    @app.post(f"{api}/sources/{{source_id}}/scans")
    def scan(source_id: str, payload: ScanRequest):
        if not payload.selected_roots:
            raise HTTPException(status_code=422, detail="selected_roots_required")
        return engine.scan(source_id, payload.selected_roots)

    @app.post(f"{api}/batches", status_code=202)
    def start_batch(request: Request, payload: BatchRequest):
        key = request.headers.get("idempotency-key")
        if not key or len(key) > 200:
            raise SafetyError("invalid_idempotency_key")
        batch = engine.start(payload.source_id, payload.scan_id, payload.scan_revision, key, background=True)
        return _batch_view(batch)

    @app.get(f"{api}/batches")
    def batches():
        return {"batches": [_batch_view(batch) for batch in engine.batches()]}

    @app.get(f"{api}/batches/{{batch_id}}")
    def get_batch(batch_id: str):
        return _batch_view(_resolve_batch(engine, batch_id))

    @app.get(f"{api}/batches/{{batch_id}}/files")
    def batch_files(batch_id: str):
        batch = _resolve_batch(engine, batch_id)
        fields = ("relative_path", "size_bytes", "copy_status", "error", "metadata", "existing_copy")
        return {
            "files": [
                {key: file.get(key) for key in fields} for file in engine.store.files(batch["batch_uid"])
            ]
        }

    @app.post(f"{api}/batches/{{batch_id}}/interrupt")
    def interrupt(batch_id: str, _: dict[str, Any] | None = None):
        batch = _resolve_batch(engine, batch_id)
        return engine.interrupt(batch["batch_uid"])

    @app.post(f"{api}/batches/{{batch_id}}/resume")
    def resume(batch_id: str, _: dict[str, Any] | None = None):
        batch = _resolve_batch(engine, batch_id)
        return _batch_view(engine.resume(batch["batch_uid"], background=True))

    @app.get(f"{api}/batches/{{batch_id}}/manifest")
    def manifest(batch_id: str, format: str = "md"):
        if format not in {"md", "json"}:
            raise HTTPException(status_code=400, detail="manifest_format_must_be_md_or_json")
        batch = _resolve_batch(engine, batch_id)
        storage_state = batch.get("lifecycle", {}).get("storage_state", "ACTIVE")
        if storage_state == "ACTIVE":
            root = engine.staging / batch["batch_id"]
        elif storage_state == "TRASHED":
            root = engine.staging / ".ingest-trash" / batch["batch_uid"]
        else:
            root = engine.archive / batch["batch_uid"]
        path = root / f"ingest_manifest.{format}"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="manifest_not_found")
        media_type = "text/markdown; charset=utf-8" if format == "md" else "application/json"
        return FileResponse(path, media_type=media_type, filename=path.name)

    @app.get(f"{api}/settings")
    def settings():
        return engine.settings()

    @app.patch(f"{api}/settings")
    def update_settings(payload: SettingsRequest):
        return engine.update_settings(payload.expected_version, payload.values, payload.confirm_policy_change)

    @app.patch(f"{api}/batches/{{batch_id}}/retention")
    def update_retention(batch_id: str, payload: dict[str, Any] | None = None):
        batch = _resolve_batch(engine, batch_id)
        return retention.update(batch["batch_uid"], payload or {})

    @app.post(f"{api}/batches/{{batch_id}}/handoffs")
    def handoff(batch_id: str, payload: ConfirmationRequest):
        batch = _resolve_batch(engine, batch_id)
        return retention.handoff(batch["batch_uid"], payload.confirmed)

    @app.post(f"{api}/batches/{{batch_id}}/restore")
    def restore(batch_id: str, _: dict[str, Any] | None = None):
        batch = _resolve_batch(engine, batch_id)
        return retention.restore(batch["batch_uid"])

    @app.post(f"{api}/cleanup/previews")
    def cleanup_preview(_: dict[str, Any] | None = None):
        return retention.preview()

    @app.get(f"{api}/retention/overview")
    def retention_overview():
        value = retention.overview()
        value["batches"] = [_batch_view(batch) for batch in value["batches"]]
        return value

    @app.post(f"{api}/cleanup/runs")
    def cleanup_run(payload: CleanupRunRequest):
        result = retention.execute(payload.plan_id, payload.confirmed)
        return result

    root = Path(static_dir) if static_dir else Path(__file__).with_name("web")
    if root.is_dir():
        app.mount("/static", StaticFiles(directory=root), name="static")
        index = root / "index.html"
        if index.is_file():

            @app.get("/", include_in_schema=False)
            def index_page():
                return FileResponse(index, media_type="text/html; charset=utf-8")

    return app


__all__ = ["create_app"]
