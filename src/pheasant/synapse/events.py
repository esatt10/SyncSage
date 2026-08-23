"""Append-only sync event stream + router webhook (Synapse step 21.5).

Two responsibilities, both fail-soft:

1. :class:`EventStream` appends one JSON object per line to
   ``<state>/events/YYYY-MM-DD.ndjson`` for ``sync.started`` /
   ``sync.completed`` / ``sync.failed`` / ``source.changed``. The log is
   purely local and always written (it is useful standalone) — a write
   failure is logged, never raised, so the indexing path is never blocked by
   the event sink.

2. :class:`RouterWebhook` POSTs the ``sync.completed`` event to
   ``<router_url>/v1/synapse/events`` when ``synapse.router_url`` is set. The
   body shape matches the sibling's 20.3 ``POST /v1/synapse/events`` endpoint:
   an event envelope with an optional inline ``contract`` (the router upserts
   it) or, absent a contract, an ``endpoint`` the router pulls
   ``GET <endpoint>/contract`` from. Network calls go through stdlib
   ``urllib`` (no new dependency, same surface as the connectors/embedder) with
   a short timeout; any failure is logged and swallowed — a region must keep
   working when the router is down.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request

from pheasant.security import egress

logger = logging.getLogger(__name__)

EVENT_TYPES = frozenset({"sync.started", "sync.completed", "sync.failed", "source.changed"})


class EventStream:
    """Durable, append-only NDJSON event log under ``<state>/events/``."""

    def __init__(self, state_path: str | Path):
        self.directory = Path(state_path) / "events"

    def _path_for(self, day: date) -> Path:
        return self.directory / f"{day.isoformat()}.ndjson"

    def append(self, event: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
        """Append one event; returns the stored record (with ``ts``).

        ``now`` is an ISO-8601 timestamp (injectable for deterministic tests).
        Failures to write are logged and swallowed — the event log must never
        break a sync.
        """

        from pheasant.ingestion.pipeline import utc_now

        record = dict(event)
        record.setdefault("ts", now or utc_now())
        line = json.dumps(record, sort_keys=True, default=str)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            day = _day_from_ts(record["ts"])
            path = self._path_for(day)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:  # pragma: no cover - defensive, disk failure
            logger.warning("Failed to append synapse event to NDJSON log: %s", exc)
        return record

    def read_all(self) -> list[dict[str, Any]]:
        """Read every event across all daily files (oldest day first)."""

        events: list[dict[str, Any]] = []
        if not self.directory.exists():
            return events
        for path in sorted(self.directory.glob("*.ndjson")):
            for raw in path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if raw:
                    events.append(json.loads(raw))
        return events


class RouterWebhook:
    """POST ``sync.completed`` to the router, fail-soft.

    No-op (returns ``False``) when ``router_url`` is unset.
    """

    def __init__(
        self, router_url: str | None, *, timeout: float = 5.0, allow_private_egress: bool = False
    ):
        self.router_url = (router_url or "").rstrip("/") or None
        self.timeout = timeout
        self.allow_private_egress = allow_private_egress

    @property
    def enabled(self) -> bool:
        return self.router_url is not None

    def post_event(self, event: dict[str, Any]) -> bool:
        """POST the event to ``<router>/v1/synapse/events``.

        Returns ``True`` on a 2xx response, ``False`` when disabled or on any
        failure (logged, never raised) — including the destination failing
        the egress check (security audit finding C2), which surfaces here as
        an ordinary :class:`~pheasant.security.egress.EgressBlocked`, caught
        below same as any other reason the webhook could not be delivered.
        """

        if not self.enabled:
            return False
        url = f"{self.router_url}/v1/synapse/events"
        body = json.dumps(event, default=str).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "pheasant/0.1"},
            method="POST",
        )
        try:
            with egress.open_url(
                request, timeout=self.timeout, allow_private=self.allow_private_egress
            ) as response:
                status = int(getattr(response, "status", 0) or response.getcode() or 0)
        except (URLError, OSError, ValueError, egress.EgressBlocked) as exc:
            logger.warning("Synapse router webhook to %s failed: %s", url, exc)
            return False
        if 200 <= status < 300:
            return True
        logger.warning("Synapse router webhook to %s returned HTTP %s", url, status)
        return False


def _day_from_ts(ts: str) -> date:
    """Best-effort YYYY-MM-DD from an ISO-8601 timestamp."""

    from datetime import datetime

    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
    except ValueError:
        return date.fromisoformat(str(ts)[:10])
