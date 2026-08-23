# Security

pheasant indexes local content for agents, so it must be conservative about paths, secrets, and execution.

A [security audit dated 2026-08-23](security-audit-2026-08-23.md) records open
findings against several claims on this page, including the ACL-coverage
statement below — treat that audit as the current state of remediation, not
this page alone.

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

- **Bind address.** Loopback by default on both paths: `pheasant up`
  generates `host: 127.0.0.1`, and compose publishes
  `127.0.0.1:8765:8765`. The container itself still binds `0.0.0.0`, because
  binding loopback *inside* a container makes it unreachable from the host —
  it is the published port that is restricted. Set `PHEASANT_BIND=0.0.0.0`
  to expose it, and only behind an authenticating ingress. Note that Docker's
  port publishing writes its own iptables rules, so a host firewall is not a
  substitute for this.
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

### Indexing any readable path — the deliberate tradeoff

`security.allow_user_selected_source_paths` defaults to `true`: a source may
name **any path the pheasant process can read**, not just one under
`allow_workspace_roots`. This is a deliberate product decision — pointing
pheasant at a folder without first editing an allowlist is the whole
quickstart experience — and it means the process's own filesystem access is
the boundary. Four controls compensate, and they are why the tradeoff is
tenable:

1. **Credentials never enter the index.** `security.default_exclude_secrets`
   (on by default) unions `SECRET_EXCLUDES` into every filesystem source's
   exclude list — SSH and GPG keys, `.env`, `~/.aws`, `~/.kube`,
   `~/.docker/config.json`, `~/.config/gh`, `.netrc`, `.npmrc`,
   `.git-credentials` and more. Critically, this happens *after* any
   caller-supplied `exclude`, because supplying that list replaces the field
   wholesale. Without this, indexing `$HOME` with
   `include: ["**/*.json", "**/*.yaml"]` sweeps up live tokens.
2. **The traversal is bounded** (`sync.limits`) and refuses rather than
   truncates, so a mistaken source is a clear stop, not an OOM.
3. **The API is not exposed to the network by default** — `pheasant up`
   generates `host: 127.0.0.1`, and compose publishes to loopback. Since the
   API is unauthenticated, this is what keeps "can read any path" from
   meaning "anyone on the network can read any path".
4. **The container does not run as root**, so in a Docker deployment the
   reachable filesystem is narrower than the host's.

Set `allow_user_selected_source_paths: false` (with explicit
`allow_workspace_roots`) for a multi-user or exposed deployment where
callers should not choose paths at all. The cost is that the UI file browser
can no longer leave the configured roots; the CLI is unaffected either way.

**What this does not protect against:** anything that can reach the API can
still ask it to index any path the process can read, and read the result
back. If you expose the port, put an authenticating proxy in front of it and
turn the flag off.
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

Two separate lists, because they answer different questions:

- **`SECRET_EXCLUDES`** — disclosure. SSH/GPG keys, PEM/`.key`/`.p12`,
  `.env*`, `~/.aws`, `~/.azure`, `~/.kube`, `~/.gnupg`,
  `~/.docker/config.json`, `~/.config/gh`, `~/.config/gcloud`, `.netrc`,
  `.npmrc`, `.pypirc`, `.git-credentials`, keychains and password stores.
  Governed by `security.default_exclude_secrets` and unioned into every
  filesystem source **after** any caller-supplied `exclude`, so they cannot
  be dropped by accident.
- **`NOISE_EXCLUDES`** — cost. `.git`, `node_modules`, `__pycache__`,
  virtualenvs, `dist`/`build`/`target`, and tool caches. These are ordinary
  defaults an operator may legitimately replace, and the walker *prunes*
  them, so an excluded subtree is never descended into.

If you deliberately need to index something on the secret list, set
`security.default_exclude_secrets: false` and take responsibility for the
source's own `exclude`.

## Upgrade note: container user

The image runs as uid 10001 rather than root. A `/state` volume created by an
older root-running image will still be owned by root and the new container
will fail to write to it. Fix it once with:

```bash
docker run --rm -v pheasant_pheasant-state:/state alpine chown -R 10001:10001 /state
```

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
