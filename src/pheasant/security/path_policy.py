from __future__ import annotations

from pathlib import Path
from typing import Any


class PathPolicyError(ValueError):
    pass


def resolve_under(path: str | Path, allowed_roots: list[str | Path]) -> Path:
    """``path``, resolved, if and only if it falls under one of
    ``allowed_roots`` — resolved too, so a symlink escape or a relative
    ``..`` cannot slip through the comparison.

    An empty ``allowed_roots`` raises rather than admitting every path
    (security audit finding M4): "no roots configured" means nothing is
    allowed, never "everything is". A caller that means "no restriction"
    has to say so some other way — e.g.
    ``security.allow_user_selected_source_paths: true``, which widens the
    allow-list to ``/`` explicitly (see :func:`configured_roots`) — rather
    than reaching this function with an empty list, whether that list came
    from a config with genuinely nothing configured or from a caller that
    silently dropped every root it was given (the bug this guards against:
    a root that does not currently exist on disk is not the same thing as
    a root that was never configured, and a caller that conflated the two
    used to turn a locked-down allow-list into no restriction at all the
    moment one of its entries was unmounted or mistyped).
    """
    candidate = Path(path).expanduser().resolve()
    roots = [Path(root).expanduser().resolve() for root in allowed_roots]
    if not roots:
        raise PathPolicyError(
            f"Path {candidate} is outside allowed roots: no roots are configured. "
            f"Set security.allow_workspace_roots (or "
            f"security.allow_user_selected_source_paths: true)."
        )
    for root in roots:
        if candidate == root or root in candidate.parents:
            return candidate
    raise PathPolicyError(
        f"Path {candidate} is outside allowed roots: {', '.join(map(str, roots))}. "
        f"Mount it under one of those roots, or add its container path to "
        f"security.allow_workspace_roots (or set "
        f"security.allow_user_selected_source_paths: true)."
    )


def configured_roots(config: Any) -> list[Path]:
    """Every root a caller should treat as allowed, resolved from live
    config — ``pheasant.workspace_root``, ``pheasant.exports_path``,
    ``security.allow_workspace_roots``, plus ``/`` when
    ``security.allow_user_selected_source_paths`` is on.

    Existence is deliberately **not** checked here (security audit finding
    M4): a root that is not currently mounted is still a root the operator
    configured, and dropping it here is exactly how a locked-down
    allow-list silently became "allow everything" once :func:`resolve_under`
    saw an empty list — the operator who wrote
    ``allow_user_selected_source_paths: false`` with an explicit
    ``allow_workspace_roots`` did the *safer* thing, and a mistyped or
    not-yet-mounted entry should narrow what is reachable, never widen it.
    A caller that wants to know whether a specific root is actually
    mounted (the file browser, flagging ``mounted: false``) checks
    ``.exists()`` itself, root by root, on the list this returns.

    One function so the three places that used to each derive this list
    (``api/app.py``'s ``_allowed_roots``/``_configured_roots`` — now one
    function, re-exported from here — and ``mcp_server/tools.py``'s inline
    copy) cannot drift out of agreement on what "configured" means.
    """
    roots = [
        config.pheasant.workspace_root,
        config.pheasant.exports_path,
        *config.security.allow_workspace_roots,
    ]
    if config.security.allow_user_selected_source_paths:
        roots.append(Path("/"))
    seen: list[Path] = []
    for root in roots:
        resolved = Path(root).expanduser().resolve()
        if resolved not in seen:
            seen.append(resolved)
    return seen


def resolve_config_write_target(
    requested: str | Path | None,
    *,
    server_config_path: str | Path,
    allowed_roots: list[str | Path] | None = None,
) -> Path:
    """Where a "promote this source into YAML" write is allowed to land.

    ``config_path`` on the promote surfaces (HTTP ``POST
    /sources/{id}/promote``, MCP ``promote_runtime_source_to_config``) used
    to be written verbatim, which turned a source-management call into an
    arbitrary file write anywhere the process could reach. Promotion only
    ever *means* "write my config", so the target is constrained to the
    config file this server was started with, or — for the multi-config
    workflows the CLI supports — a path under an allowed root.

    ``allowed_roots`` is opt-in; with none supplied the server's own config
    path is the single permitted target.
    """
    server_path = Path(server_config_path).expanduser().resolve()
    if requested is None or str(requested).strip() == "":
        return server_path
    candidate = Path(requested).expanduser().resolve()
    if candidate == server_path:
        return candidate
    if allowed_roots:
        try:
            return resolve_under(candidate, list(allowed_roots))
        except PathPolicyError:
            pass
    raise PathPolicyError(
        f"Refusing to write config to {candidate}: promotion may only write this "
        f"server's config file ({server_path})"
        + (
            f" or a path under {', '.join(str(Path(r)) for r in allowed_roots)}."
            if allowed_roots
            else "."
        )
    )
