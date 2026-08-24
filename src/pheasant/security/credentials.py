"""Credential env-var allowlist for HTTP/MCP-settable ``api_key_env`` fields.

(Security audit finding C1, 2026-08-23 — see ``docs/security-audit-2026-08-23.md``.)

pheasant's credential convention is that a secret is never a config value,
only the *name* of an environment variable holding it —
``connector.api_key_env``, ``search.embeddings.api_key_env``,
``assistant.api_key_env``, ``security.idp.api_key_env``. That convention is
sound, but before this module the *name itself* was unconstrained on every
surface that accepts it over HTTP or MCP: an unauthenticated
``PUT /search/embeddings``, ``PATCH /config/section/assistant``, or
``POST /sources`` could set ``api_key_env`` to any string at all — including
one naming a secret with nothing to do with LLM providers or connectors, such
as ``AWS_SECRET_ACCESS_KEY`` or the region's own
``PHEASANT_INDEX_WORKER_TOKEN`` — and, paired with an attacker-chosen
``base_url``/``connector.api_endpoint``, ship that environment variable's
value to an attacker-controlled destination on the next request.

:func:`known_credential_envs` computes the small, closed set of env var names
this is allowed to resolve to: every integration's own documented default
(an LLM provider's catalog entry, a connector's ``DEFAULT_TOKEN_ENV``, the
IdP's default), plus whatever the *operator's own current config* already
names for one of these fields (so nothing already legitimately configured
breaks), plus ``security.allowed_credential_envs`` — an explicit operator
opt-in for anything beyond that. :func:`resolve_credential_env` is the check
itself: a requested name outside that set is refused, not silently
substituted, so a caller gets a clear error rather than a request that
quietly did something other than what it asked for.

Deliberately a set membership check, not a naming-convention heuristic
(e.g. "ends in _API_KEY") — a convention is something an attacker can also
follow (nothing stops a secret named ``ATTACKER_API_KEY`` from existing in
the environment), where a closed set of names this deployment actually
uses cannot be satisfied by guessing a shape.
"""

from __future__ import annotations

from typing import Any


class CredentialEnvNotAllowed(ValueError):
    """A caller asked to point an integration's credential at an env var
    this deployment has not approved for that purpose."""


#: Connector `type` values this module knows a `DEFAULT_TOKEN_ENV` for —
#: the only ones a caller can meaningfully check a bare `connector.api_key_env`
#: against *without* an explicit allowlist entry (unrecognized names still
#: need `security.allowed_credential_envs`, or to already be configured).
#: `web_collection`/`api` are deliberately not here even though they do
#: have a credential concept since H6 (`connector.header_env`): unlike the
#: five SaaS connectors below, they have no fixed default env var name to
#: fall back to — every `header_env` value is checked unconditionally,
#: by type, wherever it is validated (`api/app.py`'s `_source_from_payload`),
#: rather than through this type-gated set. Every other type — built-in
#: ones with no credential concept at all (`document_folder`, `s3`,
#: `memory`, ...) and, deliberately, every third-party plugin type — is
#: left unchecked: a plugin is an operator-installed extension whose own
#: credential-env convention this module cannot know in advance
#: (`pheasant.testing.ConnectorConformance` is the SDK's stability
#: contract, and it says nothing about a fixed env var name), so refusing
#: an unrecognized name there would break the plugin SDK's basic
#: "arbitrary custom token env var" flexibility rather than close a hole —
#: the operator already trusts the plugin's code to run in-process, which
#: is a materially bigger trust step than trusting its env var name.
CHECKABLE_CONNECTOR_TYPES = frozenset({"notion", "gdrive", "slack", "confluence", "imap"})


def resolve_credential_env(requested: str | None, *, allowed: set[str]) -> str | None:
    """Validate a caller-supplied ``api_key_env`` name against ``allowed``.

    Returns ``requested`` unchanged when it is allowed — ``None`` (or the
    empty string) always is, since it means "use this integration's own
    default" rather than naming anything. Raises
    :class:`CredentialEnvNotAllowed` otherwise.
    """
    if not requested:
        return requested
    if requested not in allowed:
        raise CredentialEnvNotAllowed(
            f"{requested!r} is not an approved credential environment variable name. "
            f"Expected one of: {', '.join(sorted(allowed)) or '(none configured)'}. "
            "Add it to security.allowed_credential_envs in pheasant.yaml if this "
            "destination is intentional."
        )
    return requested


def known_credential_envs(config: Any) -> set[str]:
    """The full set of env var names this deployment already treats as
    credential references — the allowlist :func:`resolve_credential_env`
    checks against.

    Built fresh from live config each call (cheap: a handful of attribute
    reads and constant imports) rather than cached, so a config reload never
    serves a stale set.
    """

    from pheasant.assistant.catalog import PROVIDERS
    from pheasant.connectors.confluence import DEFAULT_TOKEN_ENV as CONFLUENCE_TOKEN_ENV
    from pheasant.connectors.gdrive import DEFAULT_TOKEN_ENV as GDRIVE_TOKEN_ENV
    from pheasant.connectors.imap import DEFAULT_TOKEN_ENV as IMAP_TOKEN_ENV
    from pheasant.connectors.notion import DEFAULT_TOKEN_ENV as NOTION_TOKEN_ENV
    from pheasant.connectors.slack import DEFAULT_TOKEN_ENV as SLACK_TOKEN_ENV

    allowed: set[str] = {spec.api_key_env for spec in PROVIDERS.values()}
    allowed.update(
        {
            CONFLUENCE_TOKEN_ENV,
            GDRIVE_TOKEN_ENV,
            IMAP_TOKEN_ENV,
            NOTION_TOKEN_ENV,
            SLACK_TOKEN_ENV,
        }
    )

    security = getattr(config, "security", None)
    idp = getattr(security, "idp", None)
    if idp is not None and getattr(idp, "api_key_env", None):
        allowed.add(idp.api_key_env)
    if security is not None:
        allowed.update(getattr(security, "allowed_credential_envs", None) or [])

    search = getattr(config, "search", None)
    embeddings = getattr(search, "embeddings", None)
    if embeddings is not None and getattr(embeddings, "api_key_env", None):
        allowed.add(embeddings.api_key_env)

    assistant = getattr(config, "assistant", None)
    if assistant is not None and getattr(assistant, "api_key_env", None):
        allowed.add(assistant.api_key_env)

    for source in getattr(config, "sources", None) or []:
        connector = getattr(source, "connector", None)
        env_name = getattr(connector, "api_key_env", None) if connector is not None else None
        if env_name:
            allowed.add(env_name)
        # Security audit finding H6: connector.header_env (a header name ->
        # env var name map, for web_collection/api's own credential
        # header) is the same class of reference as api_key_env, so an
        # already-configured value is grandfathered the same way.
        header_env = getattr(connector, "header_env", None) if connector is not None else None
        if header_env:
            allowed.update(v for v in header_env.values() if v)

    return allowed
