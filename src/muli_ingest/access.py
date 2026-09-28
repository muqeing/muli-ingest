"""Small, dependency-free access control for explicitly enabled LAN service mode."""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import stat
import threading
import time
from collections import defaultdict, deque
from pathlib import Path


class AccessConfigurationError(ValueError):
    pass


class LanAccess:
    cookie_name = "muli_ingest_session"

    def __init__(
        self,
        access_code: str,
        *,
        secure_cookie: bool = False,
        session_ttl_seconds: int = 12 * 60 * 60,
        failure_limit: int = 5,
        failure_window_seconds: int = 5 * 60,
    ):
        if access_code != access_code.strip() or "\n" in access_code or "\r" in access_code:
            raise AccessConfigurationError("访问口令不能包含首尾空格或换行")
        encoded = access_code.encode("utf-8")
        if not encoded or len(encoded) > 256:
            raise AccessConfigurationError("登录密码不能为空且不能超过 256 字节")
        self._access_code = encoded
        self._signing_key = secrets.token_bytes(32)
        self.secure_cookie = secure_cookie
        self.session_ttl_seconds = session_ttl_seconds
        self.failure_limit = failure_limit
        self.failure_window_seconds = failure_window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._sessions: dict[str, int] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: str | Path, **kwargs) -> LanAccess:
        secret_path = Path(path)
        info = secret_path.lstat()
        if not stat.S_ISREG(info.st_mode) or secret_path.is_symlink():
            raise AccessConfigurationError("访问口令必须来自普通文件")
        if info.st_mode & 0o077:
            raise AccessConfigurationError("访问口令文件权限必须为 0600")
        value = secret_path.read_text(encoding="utf-8").removesuffix("\n")
        return cls(value, **kwargs)

    def authenticate(self, candidate: str, peer: str) -> tuple[bool, int | None]:
        now = time.monotonic()
        key = peer or "unknown"
        with self._lock:
            failures = self._failures[key]
            while failures and now - failures[0] >= self.failure_window_seconds:
                failures.popleft()
            if len(failures) >= self.failure_limit:
                retry_after = math.ceil(self.failure_window_seconds - (now - failures[0]))
                return False, max(1, retry_after)
            valid = hmac.compare_digest(candidate.encode("utf-8"), self._access_code)
            if valid:
                self._failures.pop(key, None)
            else:
                failures.append(now)
            return valid, None

    def issue_session(self, *, now: int | None = None) -> tuple[str, int]:
        current = int(time.time()) if now is None else now
        expires = current + self.session_ttl_seconds
        payload = f"v1.{expires}.{secrets.token_urlsafe(18)}"
        signature = hmac.new(self._signing_key, payload.encode("ascii"), hashlib.sha256).hexdigest()
        value = f"{payload}.{signature}"
        with self._lock:
            self._sessions[self._session_key(value)] = expires
        return value, expires

    def verify_session(self, value: str | None, *, now: int | None = None) -> tuple[bool, int | None]:
        if not value:
            return False, None
        try:
            version, raw_expires, nonce, supplied_signature = value.split(".", 3)
            expires = int(raw_expires)
        except (TypeError, ValueError):
            return False, None
        if version != "v1" or not nonce or expires <= (int(time.time()) if now is None else now):
            return False, None
        payload = f"{version}.{raw_expires}.{nonce}"
        expected = hmac.new(self._signing_key, payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied_signature, expected):
            return False, None
        current = int(time.time()) if now is None else now
        key = self._session_key(value)
        with self._lock:
            expired = [session for session, deadline in self._sessions.items() if deadline <= current]
            for session in expired:
                self._sessions.pop(session, None)
            if self._sessions.get(key) != expires:
                return False, None
        return True, expires

    def revoke_session(self, value: str | None) -> None:
        if not value:
            return
        with self._lock:
            self._sessions.pop(self._session_key(value), None)

    @staticmethod
    def _session_key(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
