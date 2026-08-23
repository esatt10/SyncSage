"""External-IdP group sync with a staleness SLA (Product Step 32.4).

The 32.2 ``security.groups`` config map stays the deterministic core;
this module adds a *synced* principal->groups mapping pulled from an
external identity provider (SCIM 2.0 ``/Groups``) so group grants track
the directory without config edits. The mapping is persisted in the
region's SQLite state (``idp_groups`` + ``idp_sync_meta``) and refreshed
on the scheduler beat or on demand (``POST /security/idp/sync``).

The staleness SLA is the load-bearing rule: when the last successful
sync is older than ``security.idp.staleness_max_minutes`` (or has never
happened), IdP-derived grants are **dropped — fail closed**. Nobody
keeps group access on the strength of a directory the region can no
longer see; config-mapped groups and explicit caller groups still apply.

Disabled by default (``security.idp.enabled: false``): a standalone /
config-mapped region behaves byte-identically to 32.2. The only network
call lives in the module-level :func:`_http_get_json` (the connector
pattern — stdlib urllib, monkeypatch-friendly, never invoked by tests).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from pheasant.security import egress

logger = logging.getLogger(__name__)

SCIM_PAGE_SIZE = 100


def _http_get_json(url: str, token: str | None) -> dict[str, Any]:
    """The single network hop; tests monkeypatch this."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def fetch_scim_groups(base_url: str, token: str | None) -> dict[str, list[str]]:
    """Principal -> sorted group names, from a SCIM 2.0 ``/Groups`` listing.

    Paginates the standard ListResponse (``totalResults`` / ``startIndex`` /
    ``Resources``); each group contributes its ``displayName`` to every
    ``members[].value`` principal. Deterministic: sorted groups per
    principal, duplicates collapsed.
    """
    mapping: dict[str, set[str]] = {}
    start_index = 1
    while True:
        query = urllib.parse.urlencode({"startIndex": start_index, "count": SCIM_PAGE_SIZE})
        page = _http_get_json(f"{base_url.rstrip('/')}/Groups?{query}", token)
        resources = page.get("Resources") or []
        for group in resources:
            name = str(group.get("displayName") or "").strip()
            if not name:
                continue
            for member in group.get("members") or []:
                principal = str(member.get("value") or "").strip()
                if principal:
                    mapping.setdefault(principal, set()).add(name)
        total = int(page.get("totalResults") or 0)
        start_index += len(resources)
        if not resources or start_index > total:
            break
    return {principal: sorted(groups) for principal, groups in sorted(mapping.items())}


def run_idp_sync(
    config: Any,
    state: Any,
    *,
    now: datetime | None = None,
    fetch: Any = None,
) -> dict[str, Any]:
    """One sync pass: fetch the directory, persist the mapping, bump the clock.

    ``synced_at`` updates on every *successful* pass even when the mapping is
    unchanged — the heartbeat IS the staleness-SLA clock. Raises ValueError
    when the block is disabled or misconfigured (actionable messages).
    """
    settings = getattr(config.security, "idp", None)
    if settings is None or not settings.enabled:
        raise ValueError("IdP sync is disabled — set security.idp.enabled: true")
    if settings.provider != "scim":
        raise ValueError(f"unknown IdP provider {settings.provider!r} (supported: scim)")
    if not settings.base_url:
        raise ValueError("security.idp.base_url is required for SCIM sync")
    # Validated once, here — not inside _http_get_json, which
    # tests/test_idp_sync.py replaces wholesale with a 2-argument fake — so
    # one check covers every paginated request fetch_scim_groups makes
    # against the same host (security audit finding C2).
    try:
        egress.check_fetchable(
            settings.base_url, allow_private=config.security.allow_private_egress
        )
    except egress.EgressBlocked as exc:
        raise ValueError(str(exc)) from exc
    token = os.environ.get(settings.api_key_env)
    fetcher = fetch or fetch_scim_groups
    mapping = fetcher(settings.base_url, token)
    synced_at = (now or datetime.now(UTC)).isoformat()
    changed = state.replace_idp_groups(mapping, synced_at)
    return {
        "synced_at": synced_at,
        "principals": len(mapping),
        "groups": len({g for groups in mapping.values() for g in groups}),
        "changed": changed,
    }


def idp_status(config: Any, state: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """The operator view: last heartbeat, SLA verdict, mapping size."""
    settings = getattr(config.security, "idp", None)
    enabled = bool(settings is not None and settings.enabled)
    synced_at = state.idp_synced_at()
    return {
        "enabled": enabled,
        "provider": getattr(settings, "provider", None) if enabled else None,
        "synced_at": synced_at,
        "stale": _is_stale(synced_at, settings, now=now) if enabled else None,
        "staleness_max_minutes": getattr(settings, "staleness_max_minutes", None)
        if enabled
        else None,
        "principals": state.idp_principal_count(),
    }


def run_idp_maintenance(
    config: Any,
    state: Any,
    *,
    now: datetime | None = None,
    fetch: Any = None,
) -> dict[str, Any] | None:
    """Scheduler-beat refresh: sync when the interval has elapsed.

    Never raises — a fetch failure is reported (and logged) so the beat
    survives; prolonged failure simply ages the mapping toward the SLA,
    which then fails closed at query time. Returns None when disabled or
    not yet due.
    """
    settings = getattr(config.security, "idp", None)
    if settings is None or not settings.enabled:
        return None
    moment = now or datetime.now(UTC)
    last = _parse_instant(state.idp_synced_at())
    if last is not None and moment - last < timedelta(minutes=settings.sync_interval_minutes):
        return None
    try:
        return run_idp_sync(config, state, now=moment, fetch=fetch)
    except Exception as exc:
        logger.warning("IdP group sync failed (mapping ages toward the SLA): %s", exc)
        return {"error": str(exc), "synced_at": state.idp_synced_at()}


def fresh_idp_groups(
    state: Any, principal: str, settings: Any, *, now: datetime | None = None
) -> set[str]:
    """The principal's IdP-granted ``group:`` identities — empty when stale.

    The staleness SLA fails closed: a mapping older than
    ``staleness_max_minutes`` (or never synced) grants nothing.
    """
    if settings is None or not getattr(settings, "enabled", False):
        return set()
    if _is_stale(state.idp_synced_at(), settings, now=now):
        return set()
    bare = principal.split(":", 1)[-1]
    groups = state.idp_groups_for({principal, bare})
    return {g if g.startswith("group:") else f"group:{g}" for g in groups}


def _is_stale(synced_at: str | None, settings: Any, *, now: datetime | None = None) -> bool:
    last = _parse_instant(synced_at)
    if last is None:
        return True
    moment = now or datetime.now(UTC)
    return moment - last > timedelta(minutes=settings.staleness_max_minutes)


def _parse_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
