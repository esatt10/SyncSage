# Security

SyncSage indexes local content for agents, so it must be conservative about paths, secrets, and execution.

## Required controls

- Only index paths under configured allowlisted roots.
- Reject path traversal and unsafe symlinks that escape allowlisted roots.
- Exclude secrets and generated dependency/build folders by default.
- Do not execute code from indexed repositories.
- Keep MCP tools limited to retrieval, sync, registration, export, and status operations.
- Prefer read-only source mounts in Docker and Kubernetes.
- Bind local API/UI carefully and protect enterprise ingress with cluster controls.

## Trust model for the HTTP API

**The HTTP API has no authentication of its own.** Every route — including
the ones that write config, register sources and trigger syncs — is open to
anything that can reach the port. That is a deliberate local-first choice,
and it makes two controls load-bearing:

- **Bind address.** `server.host` defaults to `0.0.0.0` so the container is
  reachable from the host. On a shared or untrusted network, publish it to
  `127.0.0.1` only (`ports: ["127.0.0.1:8765:8765"]` in compose) or put an
  authenticating ingress in front.
- **CORS origins.** `server.api.cors_origins` is an allowlist, not `*`.
  Without it, any web page the user visits can script the whole API from
  their browser: read the index, rewrite the config, or repoint the
  embedding provider at an attacker's host and ship a server-held API key
  with the next request. The bundled UI proxies `/api/*` same-origin in
  both dev (Vite) and compose (nginx), so it needs no CORS entry at all.
  `server.api.cors_allow_all_origins: true` restores the wildcard for
  deployments that authenticate upstream.

Anything reachable over that API is reachable by whoever can reach the port.
Treat "who can open :8765" as the real authorization boundary.

## Path and write policy

- **Source paths.** `security.allow_user_selected_source_paths` (default
  `true`) lets a user index any path that exists — the quickstart UX depends
  on it. Set it to `false` and list explicit `security.allow_workspace_roots`
  for a deployment where callers should not choose arbitrary paths.
- **Config writes.** Source promotion (`POST /sources/{id}/promote`, MCP
  `promote_runtime_source_to_config`) may only write this server's own
  config file or a path under a configured root. It deliberately does *not*
  consult `allow_user_selected_source_paths`: choosing what to index and
  choosing where the server writes YAML are different permissions.
- **Remote fetching.** The `web_collection` and `api` connectors fetch
  `http`/`https` only. `file://` URLs are refused (and skipped with a
  warning rather than failing the sync) so a "web collection" cannot be
  used to read and index the host filesystem. Index local content with a
  filesystem source, which goes through path policy.
- **Cloning.** Clone URLs must name a known transport (`http`, `https`,
  `ssh`, `git`) or the `user@host:path` form. Transport helpers such as
  `ext::` (which name a command for git to run) and anything starting with
  `-` (which git parses as an option) are refused before `git clone` sees
  them; the clone subprocess additionally runs with `protocol.allow=never`
  plus explicit per-protocol allowances and `GIT_TERMINAL_PROMPT=0`.
- **Backup restore.** Archive members are checked for traversal *and* for
  links whose target escapes the destination, then extracted with the
  stdlib `data` filter.

## Prompt injection posture

Indexed documents are untrusted data. Retrieval responses should preserve provenance and should not cause agents to run instructions found inside indexed content unless explicitly requested by the user and independently validated.

## Default excluded content

Exclude `.env*`, private keys, PEM/key files, `.git`, dependency folders, virtual environments, caches, and build outputs unless a user intentionally overrides the policy.

## Artifact ACLs and principal-aware retrieval (Phase 32)

SaaS connectors capture source permissions into a canonical per-artifact ACL
(`{"allow": ["user:…", "group:…"], "public": bool}`). Enforcement is opt-in:

```yaml
security:
  acl_enforced: true          # default false = pre-32 behavior, byte-identical
  default_visibility: public  # un-ACL'd artifacts; "private" requires a principal
  groups:                     # deterministic config-mapped principal -> groups
    carol:
      - eng
```

With enforcement on, `search_context` (library, MCP, HTTP `/search`) accepts
`principal` + `principal_groups` and filters candidates against artifact ACLs
before results are merged. The trust model: the region enforces *visibility*;
the caller (the Synapse router, or your deployment perimeter) authenticates.

Enforcement covers **every** surface that returns indexed content, not just
`/search`: `get_relevant_files` / `POST /relevant-files` run the same filter,
and the raw-content endpoints (`GET /files/summary`, `GET /nodes/content`)
take a `principal` query parameter and answer `403` for a caller the
artifact's ACL does not admit. Filtering search results while serving the
same bytes from another route is not enforcement. All of it is a no-op when
`acl_enforced` is false.

### External IdP group sync (Step 32.4)

Group membership can also be synced from a SCIM 2.0 directory instead of
hand-maintained config:

```yaml
security:
  idp:
    enabled: true
    provider: scim
    base_url: https://idp.example.com/scim/v2
    api_key_env: IDP_TOKEN        # bearer token env var; never stored
    sync_interval_minutes: 60     # scheduler-beat refresh cadence
    staleness_max_minutes: 1440   # the SLA
```

The mapping persists in the region's SQLite state and refreshes on the
scheduler beat or on demand (`POST /security/idp/sync`;
`GET /security/idp/status` reports the last heartbeat and SLA verdict).
**Staleness SLA:** if the last successful sync is older than
`staleness_max_minutes`, IdP-derived grants are dropped — fail closed —
until the next successful sync. Config-mapped groups and explicit caller
groups are unaffected.
