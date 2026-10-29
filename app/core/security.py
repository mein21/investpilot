"""Local password hashing, signed sessions, CSRF and secret masking.

The app intentionally avoids an external identity provider. The T-Invest token
never enters cookies or HTML; only the local session cookie is signed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

TOKEN_PATTERN = re.compile(r"\bt\.[A-Za-z0-9_\-]{20,}\b")
BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+")
SENSITIVE_KEYS = (
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "session",
)


class SecurityError(ValueError):
    pass


class SlidingWindowRateLimiter:
    """In-process limiter for local login attempts, keyed by client address."""

    def __init__(
        self,
        max_attempts: int,
        window_seconds: int,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._now = now
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, current: float) -> list[float]:
        cutoff = current - self._window_seconds
        attempts = [item for item in self._attempts.get(key, []) if item > cutoff]
        if attempts:
            self._attempts[key] = attempts
        else:
            self._attempts.pop(key, None)
        return attempts

    def retry_after(self, key: str) -> int:
        with self._lock:
            current = self._now()
            attempts = self._prune(key, current)
            if len(attempts) < self._max_attempts:
                return 0
            oldest = min(attempts[-self._max_attempts :])
            return max(1, int(oldest + self._window_seconds - current) + 1)

    def record_failure(self, key: str) -> None:
        with self._lock:
            current = self._now()
            attempts = self._prune(key, current)
            attempts.append(current)
            self._attempts[key] = attempts[-self._max_attempts :]

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise SecurityError("password must have at least 10 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        if algorithm != "scrypt":
            return False
        salt = _b64decode(salt_b64)
        expected = _b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def sign_session(payload: dict[str, Any], secret: str) -> str:
    if len(secret) < 32:
        raise SecurityError("session secret is too short")
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = _b64encode(body)
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def load_session(token: str, secret: str) -> dict[str, Any] | None:
    try:
        encoded, signature_b64 = token.split(".", 1)
        expected = hmac.new(
            secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64decode(signature_b64)):
            return None
        payload = json.loads(_b64decode(encoded))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    expires_at = payload.get("exp")
    if not isinstance(expires_at, int) or expires_at < int(time.time()):
        return None
    if not isinstance(payload.get("csrf"), str):
        return None
    return payload


@dataclass(frozen=True)
class SessionData:
    user_id: int
    username: str
    csrf: str
    expires_at: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "uid": self.user_id,
            "username": self.username,
            "csrf": self.csrf,
            "exp": self.expires_at,
        }


def new_session(user_id: int, username: str, ttl_hours: int) -> SessionData:
    return SessionData(
        user_id=user_id,
        username=username,
        csrf=new_csrf_token(),
        expires_at=int(time.time()) + ttl_hours * 3600,
    )


def mask_secret(value: str | None) -> str:
    if not value:
        return "***"
    if len(value) <= 8:
        return "***"
    return f"{value[:3]}***{value[-2:]}"


def mask_text(text: str, extra_secrets: list[str] | None = None) -> str:
    masked = TOKEN_PATTERN.sub("t.***", text)
    masked = BEARER_PATTERN.sub("Bearer ***", masked)
    for secret in extra_secrets or []:
        if secret and secret in masked:
            masked = masked.replace(secret, "***")
    return masked


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEYS)


def sanitize(value: Any, extra_secrets: list[str] | None = None, _depth: int = 0) -> Any:
    """Recursively mask secrets in audit payloads. Never mutates the input."""

    if _depth > 12:
        return "<max-depth>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                result[key_text] = "***"
            else:
                result[key_text] = sanitize(item, extra_secrets, _depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item, extra_secrets, _depth + 1) for item in value]
    if isinstance(value, str):
        return mask_text(value, extra_secrets)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return mask_text(str(value), extra_secrets)
