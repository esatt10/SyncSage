"""Artifact ACL normalization + principal checks (Product Step 32.1/32.2).

Connectors capture raw source-ACL metadata in ``item.metadata["acl"]``
(reserved since Phase 31); this module normalizes each connector's shape
into one canonical, rule-based document stored on the artifact row::

    {"allow": ["user:<id>", "group:<id>", ...], "public": bool}

and answers the enforcement question at query time. Enforcement is
**opt-in** (``security.acl_enforced: false`` by default): with it off, or
with no principal supplied, behavior is byte-identical to pre-32. An
artifact with **no** ACL (filesystem/memory/git sources) follows the
region's ``security.default_visibility`` (``public`` by default —
a single-user region stays fully searchable).

The trust model: the region enforces *visibility*; the caller (the Synapse
router, or the region's own deployment perimeter) authenticates the
principal. Principals are strings (``user:...``/``group:...``); group
membership is expanded from ``security.groups`` config (an external IdP
sync loop is Step 32.4).
"""

from __future__ import annotations

import json
from typing import Any

PUBLIC = {"allow": [], "public": True}


def normalize_acl(connector_type: str, raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Canonical ACL doc for one connector's captured metadata, or None.

    Deterministic rules per connector; None means "source expressed no
    ACL" and the region default applies.
    """
    if not raw:
        return None
    allow: list[str] = []
    public = False
    if connector_type == "notion":
        for key in ("created_by", "last_edited_by"):
            if raw.get(key):
                allow.append(f"user:{raw[key]}")
    elif connector_type == "gdrive":
        allow.extend(f"user:{owner}" for owner in raw.get("owners") or [])
    elif connector_type == "slack":
        public = not raw.get("is_private", False)
    elif connector_type == "confluence":
        if raw.get("space"):
            allow.append(f"group:space:{raw['space']}")
        if raw.get("created_by"):
            allow.append(f"user:{raw['created_by']}")
    elif connector_type == "imap":
        for key in ("from", "to", "cc"):
            for address in str(raw.get(key) or "").split(","):
                address = address.strip()
                if "@" in address:
                    # Bare the address out of "Name <addr>" forms.
                    address = address.split("<")[-1].rstrip(">").strip()
                    allow.append(f"user:{address}")
    else:
        # Unknown connector: trust explicitly-shaped canonical docs only.
        allow = [str(p) for p in raw.get("allow") or []]
        public = bool(raw.get("public", False))
    if not allow and not public:
        return None
    return {"allow": sorted(set(allow)), "public": public}


def expand_principal(
    principal: str | None,
    groups: list[str] | None,
    config_groups: dict[str, list[str]] | None,
) -> set[str] | None:
    """The principal's full identity set, or None for an anonymous caller."""
    if not principal:
        return None
    identities = {principal}
    identities.update(groups or [])
    bare = principal.split(":", 1)[-1]
    mapped = (config_groups or {}).get(principal) or (config_groups or {}).get(bare) or []
    for group in mapped:
        identities.add(group if group.startswith("group:") else f"group:{group}")
    return identities


def is_allowed(
    acl_json: str | None,
    identities: set[str] | None,
    *,
    default_public: bool,
) -> bool:
    """May a caller with ``identities`` (None = anonymous) see this artifact?"""
    if acl_json:
        try:
            acl = json.loads(acl_json)
        except (TypeError, ValueError):
            return False  # unreadable ACL fails closed
    else:
        acl = PUBLIC if default_public else None
    if acl is None:
        return identities is not None and bool(identities)  # private default:
        # any authenticated principal may see un-ACL'd artifacts; anonymous may not.
    if acl.get("public"):
        return True
    if identities is None:
        return False
    return bool(identities.intersection(acl.get("allow") or []))
