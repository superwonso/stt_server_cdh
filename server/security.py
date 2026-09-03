from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import OrderedDict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

PASSWORD_HASHER = PasswordHasher()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def password_matches(encoded: str | None, password: str) -> bool:
    if not encoded:
        return False
    try:
        return PASSWORD_HASHER.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


class RateLimiter:
    """Small bounded in-memory limiter; no client-controlled forwarded headers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._events: OrderedDict[tuple, deque[float]] = OrderedDict()

    def allow(self, key: tuple, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self._lock:
            entries = self._events.setdefault(key, deque())
            self._events.move_to_end(key)
            while entries and entries[0] <= now - window:
                entries.popleft()
            permitted = len(entries) < limit
            if permitted:
                entries.append(now)
            while len(self._events) > 4096:
                self._events.popitem(last=False)
            return permitted
