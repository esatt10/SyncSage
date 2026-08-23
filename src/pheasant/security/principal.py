"""HTTP caller-principal resolution across the three trust modes.

(Security audit finding C3, 2026-08-23 — see ``docs/security-audit-2026-08-23.md``.)

Every HTTP route that accepts a ``principal``/``principal_groups`` in its
request body or query string used to trust them verbatim — self-asserted,
unauthenticated, by any caller reaching the (by design, unauthenticated)
HTTP API. That made ``security.acl_enforced`` advisory in every shipped
configuration: it changes *what a principal may see*, never *who the
caller actually is*. ``security.principal_source`` (default ``"body"``)
makes the trust assumption explicit and, in the two modes that actually
authenticate something, makes it real. ``PheasantConfig.model_validate``
refuses ``acl_enforced: true`` with ``principal_source: "body"`` at load
time — see its docstring — so that unsafe combination cannot exist
silently.

:func:`resolve_http_principal` is the one function every principal-accepting
HTTP route calls instead of reading ``req.principal``/``req.principal_groups``
directly:

- ``"body"`` — unchanged pre-C3 behavior: whatever the request body (or
  query string) supplied, unauthenticated. The default, and — per the
  audit's own solution note — "retained for the library/CLI and
  single-user regions": a standalone deployment needs nothing else, and
  this is also what an MCP tool call's ``principal`` argument means,
  unconditionally, on every ``principal_source`` setting (see below).
- ``"header"`` — the request body's ``principal``/``principal_groups`` are
  ignored entirely; the caller is whatever ``security.principal_header``
  names (default ``X-Pheasant-Principal``), set by an authenticating
  ingress in front of this region. Groups are never read from a header —
  they come from ``security.groups`` / the IdP sync, both already-trustworthy
  config-driven sources — so there is no second header to also get right.
- ``"signed"`` — verifies a signed assertion carried in
  ``X-Pheasant-Principal-Assertion``/``X-Pheasant-Principal-Signature``
  (see ``pheasant.synapse.signing.verify_signed_principal``), for the
  Synapse fan-out case: the router authenticates the original caller and
  asserts identity to each region it queries. An assertion that fails
  verification — missing, malformed, wrong signature, expired — resolves
  to *no principal* (anonymous), the same fail-safe posture ``_acl_guard``
  already takes for an artifact id it cannot resolve, rather than failing
  the whole request: a broken or absent assertion should narrow what a
  caller sees, never widen it, and never turn into a 500.

Scope: HTTP only, deliberately. The audited finding — and this fix — is
about the HTTP API specifically (``POST /search``, ``_acl_guard`` and its
callers, ``POST /assistant/chat``, the memory routes); MCP tool calls
(``search_context``, ``memory_write``, and the rest of
``mcp_server/tools.py``) are not in its scope and are unaffected by
``principal_source`` — an MCP caller's ``principal`` argument is trusted
exactly as before, on every mode. MCP already sits behind a different
boundary (a stdio pipe or an operator-run process, not an open network
port), and requiring it to also route through a header or a signed
assertion is real, larger work this pass does not attempt — narrowing it
silently to "MCP can no longer set a principal at all" once HTTP moves off
``"body"`` would have been a bigger behavior change than the audit asked
for, not a smaller one.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

PRINCIPAL_HEADER_DEFAULT = "X-Pheasant-Principal"
ASSERTION_HEADER = "X-Pheasant-Principal-Assertion"
SIGNATURE_HEADER = "X-Pheasant-Principal-Signature"


class _HeaderLike(Protocol):
    def get(self, key: str, default: str | None = None) -> str | None: ...


def resolve_http_principal(
    *,
    headers: _HeaderLike,
    body_principal: str | None,
    body_groups: list[str] | None,
    config: Any,
) -> tuple[str | None, list[str] | None]:
    """The trustworthy ``(principal, groups)`` for one HTTP request.

    ``headers`` is any mapping-like object with a case-suitable ``.get``
    (a Starlette ``Headers`` object satisfies this; so does a plain
    ``dict`` in tests).
    """
    source = getattr(config.security, "principal_source", "body")
    if source == "body":
        return body_principal, body_groups
    if source == "header":
        header_name = getattr(config.security, "principal_header", PRINCIPAL_HEADER_DEFAULT)
        principal = headers.get(header_name)
        return (principal or None), None
    if source == "signed":
        assertion = headers.get(ASSERTION_HEADER)
        signature = headers.get(SIGNATURE_HEADER)
        key_ref = getattr(config.security, "principal_signing_public_key_ref", None)
        if not assertion or not signature or not key_ref:
            return None, None
        from pheasant.synapse.signing import PrincipalSignatureError, verify_signed_principal

        try:
            claims = verify_signed_principal(assertion, signature, key_ref)
        except PrincipalSignatureError as exc:
            logger.warning("principal assertion rejected: %s", exc)
            return None, None
        principal = claims.get("principal")
        groups = claims.get("groups")
        return (
            str(principal) if principal else None,
            [str(g) for g in groups] if isinstance(groups, list) else None,
        )
    # Unreachable once PheasantConfig.model_validate's own check has run,
    # but a config built by hand (tests, library callers bypassing
    # model_validate) should not silently trust a body principal for an
    # unrecognized mode.
    logger.warning("unknown security.principal_source %r; treating as no principal", source)
    return None, None
