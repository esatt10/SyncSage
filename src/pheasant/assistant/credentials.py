"""Process-local vault for user-supplied chat API keys.

The UI offers two ways to reach a chat model: an environment variable the
operator sets on the container, or a key the user pastes for the duration
of their session. This module backs the second one, and its whole job is
to make that path safe:

* the key lives in this process's memory only — never in ``/state``, never
  in the config file, never in a log line, never in a response body;
* callers hold an opaque token (``secrets.token_urlsafe``), not the key;
* entries expire on a TTL and are swept on every access, so a forgotten
  browser tab does not leave a usable credential behind indefinitely;
* the process holds no persistence hook at all, so a container restart is
  a full credential flush by construction.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SessionCredential:
    provider: str
    api_key: str
    model: str | None
    base_url: str | None
    expires_at: datetime

    def redacted(self) -> dict:
        """Metadata safe to return over HTTP — never includes the key."""
        return {
            "provider": self.provider,
            "model": self.model,
            "expires_at": self.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


class SessionKeyStore:
    """In-memory, TTL'd, thread-safe token → credential map."""

    def __init__(self, ttl_minutes: int = 720) -> None:
        self._ttl = timedelta(minutes=max(1, int(ttl_minutes)))
        self._entries: dict[str, SessionCredential] = {}
        self._lock = threading.Lock()

    def put(
        self,
        provider: str,
        api_key: str,
        *,
        model: str | None = None,
        base_url: str | None = None,
    ) -> tuple[str, SessionCredential]:
        token = secrets.token_urlsafe(32)
        credential = SessionCredential(
            provider=provider,
            api_key=api_key,
            model=model,
            base_url=base_url,
            expires_at=_now() + self._ttl,
        )
        with self._lock:
            self._purge_locked()
            self._entries[token] = credential
        return token, credential

    def get(self, token: str | None) -> SessionCredential | None:
        if not token:
            return None
        with self._lock:
            self._purge_locked()
            return self._entries.get(token)

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return self._entries.pop(token, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _purge_locked(self) -> None:
        now = _now()
        for token in [t for t, c in self._entries.items() if c.expires_at <= now]:
            del self._entries[token]
