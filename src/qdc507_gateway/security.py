from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional


class SecurityConfigurationError(ValueError):
    """Raised when a persisted security configuration is not acceptable."""


@dataclass(frozen=True)
class AuthLimitDecision:
    blocked: bool
    retry_after_seconds: int = 0
    newly_blocked: bool = False


class AuthFailureLimiter:
    """Small in-memory fail2ban-style limiter for Bearer token failures."""

    def __init__(
        self,
        max_failures: int = 10,
        window_seconds: int = 300,
        block_seconds: int = 900,
        *,
        clock=time.monotonic,
        max_identities: int = 10000,
    ):
        if min(max_failures, window_seconds, block_seconds, max_identities) <= 0:
            raise SecurityConfigurationError("auth limiter values must be positive")
        self.max_failures = int(max_failures)
        self.window_seconds = int(window_seconds)
        self.block_seconds = int(block_seconds)
        self.max_identities = int(max_identities)
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, identity: str) -> AuthLimitDecision:
        now = self._clock()
        with self._lock:
            self._expire(identity, now)
            blocked_until = self._blocked_until.get(identity, 0.0)
            if blocked_until > now:
                return AuthLimitDecision(True, max(1, int(blocked_until - now + 0.999)))
            return AuthLimitDecision(False)

    def record_failure(self, identity: str) -> AuthLimitDecision:
        now = self._clock()
        with self._lock:
            self._expire(identity, now)
            blocked_until = self._blocked_until.get(identity, 0.0)
            if blocked_until > now:
                return AuthLimitDecision(True, max(1, int(blocked_until - now + 0.999)))
            self._make_room(identity)
            failures = self._failures.setdefault(identity, deque())
            cutoff = now - self.window_seconds
            while failures and failures[0] <= cutoff:
                failures.popleft()
            failures.append(now)
            self._last_seen[identity] = now
            if len(failures) < self.max_failures:
                return AuthLimitDecision(False)
            blocked_until = now + self.block_seconds
            self._blocked_until[identity] = blocked_until
            failures.clear()
            return AuthLimitDecision(True, self.block_seconds, newly_blocked=True)

    def record_success(self, identity: str) -> None:
        with self._lock:
            self._failures.pop(identity, None)
            self._blocked_until.pop(identity, None)
            self._last_seen.pop(identity, None)

    def status(self) -> dict[str, int]:
        now = self._clock()
        with self._lock:
            for identity in tuple(self._last_seen):
                self._expire(identity, now)
            return {
                "tracked_identities": len(self._last_seen),
                "blocked_identities": sum(
                    blocked_until > now
                    for blocked_until in self._blocked_until.values()
                ),
            }

    def _expire(self, identity: str, now: float) -> None:
        blocked_until = self._blocked_until.get(identity)
        if blocked_until is not None and blocked_until <= now:
            self._blocked_until.pop(identity, None)
        failures = self._failures.get(identity)
        if failures is not None:
            cutoff = now - self.window_seconds
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                self._failures.pop(identity, None)
        if identity not in self._blocked_until and identity not in self._failures:
            self._last_seen.pop(identity, None)

    def _make_room(self, identity: str) -> None:
        if identity in self._last_seen or len(self._last_seen) < self.max_identities:
            return
        oldest = min(self._last_seen, key=self._last_seen.get)
        self._failures.pop(oldest, None)
        self._blocked_until.pop(oldest, None)
        self._last_seen.pop(oldest, None)


def hash_token(token: str, salt: Optional[bytes] = None) -> str:
    if not isinstance(token, str) or not token:
        raise SecurityConfigurationError("token must be a non-empty string")
    salt = salt or secrets.token_bytes(16)
    scrypt = getattr(hashlib, "scrypt", None)
    if scrypt is None:
        raise SecurityConfigurationError("Python hashlib.scrypt is required for API tokens")
    digest = scrypt(token.encode(), salt=salt, n=2**14, r=8, p=1)
    scheme = "scrypt"
    return "%s$%s$%s" % (scheme,
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def verify_token(token: str, encoded: str) -> bool:
    if not isinstance(token, str) or not isinstance(encoded, str):
        return False
    try:
        scheme, salt_value, digest_value = encoded.split("$", 2)
        salt_padding = "=" * (-len(salt_value) % 4)
        digest_padding = "=" * (-len(digest_value) % 4)
        salt = base64.b64decode(salt_value + salt_padding, altchars=b"-_", validate=True)
        expected = base64.b64decode(digest_value + digest_padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError, binascii.Error):
        return False
    if scheme != "scrypt" or not hasattr(hashlib, "scrypt"):
        return False
    # Reject malformed persisted values before invoking a memory-hard KDF.
    if len(salt) != 16 or len(expected) != 64:
        return False
    try:
        actual = hashlib.scrypt(token.encode(), salt=salt, n=2**14, r=8, p=1)
    except (ValueError, TypeError, OverflowError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def dangerous_at_command(command: str) -> bool:
    normalized = command.strip().upper()
    return any(prefix in normalized for prefix in (
        "AT+QCFG=", "AT+CFUN", "AT+QADBKEY", "AT&W", "AT+W", "AT+RESET"
    ))


def forbidden_generic_at_command(command: str) -> bool:
    return re.search(r"\bAT\s*\+\s*QADBKEY\b", command, re.IGNORECASE) is not None
