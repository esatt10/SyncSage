"""Redacting ``connector.headers`` from config read paths (security audit
finding H6, 2026-08-23).

``connector.headers`` is the one place this codebase's credential
convention — a secret is an env-var *name*, never a value — was optional
rather than enforced. ``connector.header_env`` (also H6, see
``config/schema.py``) is now the sanctioned way to put a token in a header
without it ever landing in config or state, but ``headers`` still accepts
literal values for legitimately non-secret metadata, and an operator who
puts a token there anyway (out of habit, or before ``header_env`` existed)
should not have it echoed back verbatim by every route that reads the
config or the source registry back out. This redacts defensively
regardless of which the operator used.

Deliberately **not** applied to ``GET /config``'s ``raw_yaml`` field: that
is the literal bytes of the config file, round-tripped by the UI's YAML
editor straight back through ``PUT /config``'s ``yaml_text`` — redacting it
would mean a save that doesn't happen to touch that exact line silently
persists the placeholder as the real header value, corrupting a working
connector. ``raw_yaml`` is reachable only by whoever can already reach this
HTTP API, the same unauthenticated-by-design perimeter every other route
here already trusts; ``effective`` (the parsed, introspection-only view)
and the source registry are the read paths this closes.
"""

from __future__ import annotations

import json
from typing import Any

#: Not a real value, and long enough that no genuine header value collides.
REDACTED = "***redacted***"


def redact_config(data: Any) -> Any:
    """A deep copy of ``data`` with every ``connector.headers`` mapping's
    *values* masked. Keys are kept — an operator can see which header
    names are set, just not their contents — mirroring
    ``persistence.secrets.redact_dsn``'s "shape visible, secret hidden"
    posture for the DSN case.

    Walks the whole structure rather than a fixed dotted path, the same
    reasoning ``api/app.py``'s ``_find_credential_env_values`` already
    uses for ``api_key_env``/``header_env`` names: the same ``headers``
    key shape can appear as one source's connector block, one item in a
    top-level ``sources`` list, or nested inside a ``PATCH
    /config/section/{section}`` payload, and one recursive walk covers all
    of those without needing to know which is which.
    """
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if key == "headers" and isinstance(value, dict):
                result[key] = dict.fromkeys(value, REDACTED)
            else:
                result[key] = redact_config(value)
        return result
    if isinstance(data, list):
        return [redact_config(item) for item in data]
    return data


def redact_config_json(raw: str | None) -> str | None:
    """``redact_config`` for a ``sources.config_json``-shaped value: a JSON
    *string* (what the state store and the UI's ``JSON.parse(source
    .config_json)`` both expect), not a parsed structure. Malformed or
    empty input passes through unchanged rather than raising — a redaction
    helper refusing to serve a response over a JSON quirk would be a worse
    failure than a row this defensive pass could not parse.
    """
    if not raw:
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    return json.dumps(redact_config(parsed))
