"""Shared derivation of a trusted-host allow-list from ``server.api.cors_origins``
(security audit finding H2, 2026-08-23).

``docs/security.md`` names the bind address as this API's primary
compensating control — it is unauthenticated by design, so "who can open
the port" *is* the authorization boundary. DNS rebinding defeats that:
nothing validated the ``Host`` header, so a page the operator's browser
visits can rebind its own hostname to ``127.0.0.1`` and the browser then
treats the pheasant API as same-origin — CORS never applies, because CORS
is an *origin* check, not a *destination* check.

Two independent consumers need to know which ``Host`` values may reach this
server: the FastMCP DNS-rebinding guard
(``mcp_server/server.py:_apply_transport_security``, which predates this
module) and the ``TrustedHostMiddleware`` this module's caller
(``api/app.py``) wires into the main app. Both derive their allow-list from
the same config an operator already sets for browser origins —
``cors_origins`` — rather than requiring a second list to be kept in sync.
They need different *shapes* of the same data: FastMCP's guard compares a
full ``host:port`` netloc, while Starlette's ``TrustedHostMiddleware``
strips the port and compares the bare hostname only (see its source), so
:func:`cors_origin_hosts` returns both projections from one walk of
``cors_origins`` rather than each consumer re-parsing it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

#: Always trusted, independent of configured CORS origins.
#:
#: ``localhost``/``127.0.0.1``/``::1`` are what the default loopback bind
#: (``server.host: 127.0.0.1`` — security audit finding H4) actually
#: listens on, so a config that never mentions CORS still serves itself.
#:
#: ``testserver`` is not a real-world hostname — nothing on the public or a
#: private DNS resolves to the literal single-label name "testserver", so
#: admitting it grants an attacker no rebinding target. It is Starlette's
#: ``TestClient``'s fixed default ``Host`` header (every ``TestClient(...)``
#: call across this test suite sends it), and FastAPI/Starlette apps that
#: add ``TrustedHostMiddleware`` conventionally allow it for exactly this
#: reason — the alternative is either disabling host validation under test
#: (which would leave the standalone/no-infrastructure path, the one
#: CLAUDE.md rule 7 requires a test for, actually unverified) or rewriting
#: every ``TestClient`` construction across the suite to pin a different
#: base URL, which is a change with far more blast radius than the risk
#: this one inert string carries.
ALWAYS_TRUSTED_HOSTS = ("localhost", "127.0.0.1", "::1", "testserver")


def cors_origin_hosts(api: Any) -> tuple[list[str], list[str]]:
    """``(hostnames, netlocs)`` parsed out of ``api.cors_origins``, each
    deduplicated and order-preserving. Callers decide for themselves what
    ``cors_allow_all_origins`` should mean for their own guard (the FastMCP
    guard disables DNS-rebinding protection entirely under it; the
    ``TrustedHostMiddleware`` caller skips adding the middleware) — this
    function only walks the origin list, the same for every caller.
    """
    hostnames: list[str] = []
    netlocs: list[str] = []
    for origin in getattr(api, "cors_origins", None) or []:
        parsed = urlsplit(origin)
        if parsed.hostname and parsed.hostname not in hostnames:
            hostnames.append(parsed.hostname)
        if parsed.netloc and parsed.netloc not in netlocs:
            netlocs.append(parsed.netloc)
    return hostnames, netlocs
