# Security

pheasant indexes local content for agents, so it must be conservative about paths, secrets, and execution.

A [security audit dated 2026-08-23](security-audit-2026-08-23.md) found 5
critical, 6 high and 7 medium findings. All of them are closed as of this
page's current revision; the audit itself is left unedited as the record of
what was found and why, and this page describes the system **as remediated**.
Where a fix changed a claim the audit called out as wrong (the ACL-coverage
statement below, most notably), this page now states the corrected claim
directly rather than pointing elsewhere for it.

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
and it makes several controls load-bearing:

- **Bind address.** Loopback by default everywhere: `ServerSettings.host`'s
  own schema default is `127.0.0.1` (finding H4 — a bare `pip install`
  running `pheasant setup --accept-defaults` used to bind every interface,
  since the wizard reads its defaults straight off the schema), `pheasant up`
  writes `host: 127.0.0.1` explicitly for the same reason, and compose
  publishes `127.0.0.1:8765:8765`. The container image still answers
  `0.0.0.0` for its own generated config (`docker-entrypoint.sh`), because
  binding loopback *inside* a container makes it unreachable from the host —
  it is the published port that is restricted. Set `PHEASANT_BIND=0.0.0.0`
  to expose it, and only behind an authenticating ingress. Note that Docker's
  port publishing writes its own iptables rules, so a host firewall is not a
  substitute for this. `pheasant serve` logs a warning (not a refusal) to
  stderr when `server.host` is non-loopback and
  `server.api.cors_allow_all_origins` is off — the closest existing signal
  this config has for "an authenticating ingress fronts this."
- **CORS origins.** `server.api.cors_origins` is an allowlist, not `*`.
  Without it, any web page the user visits can script the whole API from
  their browser: read the index, rewrite the config, or repoint the
  embedding provider at an attacker's host and ship a server-held API key
  with the next request. The bundled UI proxies `/api/*` same-origin in
  both dev (Vite) and compose (nginx), so it needs no CORS entry at all.
  `server.api.cors_allow_all_origins: true` restores the wildcard for
  deployments that authenticate upstream — and, together with the two
  controls below, is the one flag that turns them all off at once, on the
  reasoning that an operator who set it has already put their own
  authenticating ingress in front.
- **Host validation (finding H2).** CORS is an *origin* check the browser
  enforces when reading a response; it does nothing to stop the browser
  reaching this server in the first place. A page the operator's browser
  visits can rebind its own hostname to `127.0.0.1` — at which point the
  browser treats this API as same-origin and CORS never applies — unless
  something checks the `Host` header itself. `TrustedHostMiddleware` does:
  seeded from `server.api.cors_origins`' hostnames plus
  `localhost`/`127.0.0.1`/`::1`/the container's own hostname, it rejects any
  request naming a host this deployment did not admit. The same
  `cors_allow_all_origins` escape hatch disables it, consistent with the
  MCP transport's own DNS-rebinding guard (`/mcp`), which derives its
  allow-list from the identical config.
- **Cross-origin state changes (finding H2).** `multipart/form-data` — what
  `POST /sources/upload` accepts — is a CORS-*simple* request: a browser
  sends it with no preflight, so CORS's origin check runs too late to stop
  the mutation, only to stop the attacking page from reading the response.
  A second middleware refuses any `POST`/`PUT`/`PATCH`/`DELETE` whose
  `Origin` header names a foreign origin, closing that gap without
  affecting same-origin requests or non-browser clients (curl, a script,
  an agent), which send no `Origin` header at all.

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
- **Remote fetching (findings C2, M5).** The `web_collection` and `api`
  connectors fetch `http`/`https` only. `file://` URLs are refused (and
  skipped with a warning rather than failing the sync) so a "web
  collection" cannot be used to read and index the host filesystem. Every
  outbound request this process makes — connector fetches, the assistant
  provider, the embeddings provider, IdP sync, the Synapse router webhook —
  goes through `pheasant.security.egress`: a literal loopback, link-local
  (including `169.254.169.254`, the AWS/GCP/Azure metadata address) or
  RFC1918/CGNAT/IPv6-ULA destination is refused, and a redirect hop is
  re-validated rather than trusted. This is off by default
  (`security.allow_private_egress: false`) and stays off for the
  sandboxed-connector guest fetch path regardless of the flag — a WASM
  guest's `host_fetch` is governed entirely by its own
  `connector.allowed_hosts` allowlist. A resolution *failure* (DNS down) is
  not treated as a denial, so this never makes production behavior depend
  on live DNS at request time; only a literal or resolved address that is
  actually private is refused. `follow_symlinks: true` (opt-in, off by
  default) additionally requires a followed symlink's target to stay under
  the source's own root — and, when `allow_user_selected_source_paths` is
  off, under the configured allow-list — so a symlink inside an
  already-validated corpus cannot point outside it. Index local content
  with a filesystem source, which goes through path policy either way.
- **Cloning.** Clone URLs must name a known transport (`http`, `https`,
  `ssh`, `git`) or the `user@host:path` form. Transport helpers such as
  `ext::` (which name a command for git to run) and anything starting with
  `-` (which git parses as an option) are refused before `git clone` sees
  them; the clone subprocess additionally runs with `protocol.allow=never`
  plus explicit per-protocol allowances and `GIT_TERMINAL_PROMPT=0`.
- **Backup restore.** Archive members are checked for traversal *and* for
  links whose target escapes the destination, then extracted with the
  stdlib `data` filter.
- **Empty allow-lists fail closed (finding M4).**
  `pheasant.security.path_policy.resolve_under` refuses a path when it is
  given *no* allowed roots, rather than treating that as "no restriction" —
  the failure mode a mistyped or not-yet-mounted
  `security.allow_workspace_roots` entry used to hit, silently turning a
  deliberately locked-down config into a wide-open one.

## Credential environment variables (findings C1, H6)

pheasant's credential convention is that a secret is never a config value,
only the *name* of an environment variable holding it —
`connector.api_key_env`, `connector.header_env`, `search.embeddings.api_key_env`,
`assistant.api_key_env`, `security.idp.api_key_env`, `synapse.signing_key_ref`,
`storage.dsn_env`. Two things make that convention actually hold:

- **The name itself is checked, not just its shape.** Every one of those
  fields is validated over HTTP/MCP against
  `security.allowed_credential_envs` plus each integration's own documented
  default and whatever is already configured — a caller cannot repoint a
  provider or connector at an arbitrary environment variable (`AWS_SECRET_ACCESS_KEY`,
  this region's own `PHEASANT_INDEX_WORKER_TOKEN`) and, paired with a
  caller-chosen endpoint, exfiltrate it on the next request. Add a name to
  `security.allowed_credential_envs` to let it be set that way; it is
  always settable in YAML directly.
- **`connector.header_env`** (a header name → env var name map) is the
  sanctioned way to put a token in an outbound header for `web_collection`/
  `api` connectors, which have no fixed credential field of their own the
  way the five first-party SaaS connectors do. `connector.headers` still
  accepts literal values for genuinely non-secret metadata — but as
  defense in depth, every route that reads config or the source registry
  back out (`GET /config`'s `effective` view, `GET /config/effective`,
  `GET /sources`, `GET /overview`, MCP `list_sources`) redacts
  `connector.headers`' values regardless of which one you used, and the
  Parquet export drops `sources.config_json` entirely rather than carrying
  it. `GET /config`'s `raw_yaml` field is the one deliberate exception — it
  is round-tripped verbatim by the UI's YAML editor back through
  `PUT /config`, so redacting it would risk a save silently persisting a
  placeholder as the real header value.

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

### Who the API trusts as "the principal" (finding C3)

`security.principal_source` decides where an HTTP request's principal
actually comes from — the field the caller cannot forge changed with this
finding, not the filtering logic itself:

- **`body`** (default) — the request's own `principal`/`principal_groups`
  field or query string, unauthenticated by construction: any caller can
  claim to be anyone. Fine for the library/CLI and a genuinely single-user
  region, which is why it stays the default — but `acl_enforced: true`
  **refuses to load** combined with `principal_source: body`, because that
  combination would look like enforcement without being any.
- **`header`** — trusts `security.principal_header` (default
  `X-Pheasant-Principal`), set by an authenticating ingress in front of
  this region; the body-supplied principal is ignored entirely.
- **`signed`** — verifies an Ed25519-signed assertion
  (`X-Pheasant-Principal-Assertion` / `-Signature`) against
  `security.principal_signing_public_key_ref`, for the Synapse router
  fan-out case: the router authenticates the original caller and asserts
  identity to each region it queries. A missing or invalid assertion
  resolves to *no* principal — narrower access, never wider, never a 500.

**MCP is unaffected by this setting on every mode.** A tool call's
`principal` argument means what it always has — the caller-supplied string
— because MCP sits behind a different boundary (a stdio pipe or an
operator-run process, not an open network port); routing it through a
header or a signed assertion too is real, larger work this finding did not
attempt.

### Coverage (finding H1)

Every route that returns indexed content is ACL-guarded when
`acl_enforced` is on: `search_context`/`/search`, `get_relevant_files`/
`POST /relevant-files`, the raw-content endpoints (`GET /files/summary`,
`GET /nodes/content`, both also accepting `principal_groups` for parity
with `/search`), `GET /sources/{id}/repo-map`, `GET /taxonomy`, and the
graph-explorer routes — `GET /graph`, `/graph/export/node-link-json`,
`/graph/export/cytoscape-json`, `/graph/neighbors`, `/graph/slice`,
`/nodes/explain`. A graph node is resolved to the `artifacts` row that
governs it (directly for an artifact node, via its owning artifact for a
chunk node) and conservatively denied when it cannot be — a symbol,
heading or entity node has no artifact row of its own to check, so under
enforcement those disappear from every one of these routes rather than
leaking unfiltered.

Two routes are gated off entirely rather than filtered, because neither
can be filtered at all without either a materially different computation
or leaking exactly what filtering exists to hide: `GET /graph/diagnostics`
aggregates hub/orphan/degree statistics across the *whole* graph in one
pass — recomputing that per-principal is a different, more expensive
computation, not a post-hoc drop — and `GET /graph/path` cannot redact the
*intermediate hops* of a connectivity chain without revealing them; a path
with holes in the middle is not an answer to "how are these related."
Both answer `403` while `acl_enforced` is true rather than silently
ignoring it.

Filtering search results while serving the same bytes from another route
is not enforcement. All of the above is a no-op when `acl_enforced` is
false.

### Agent memory isolation (finding C4)

`GET /memory` and MCP `memory_list` filter the same way: `org`-scope
records are shared, but `user`/`session`-scope records are visible only to
the principal that wrote them — the guarantee `security/acl.py` documents,
which the list surfaces did not enforce before this finding, regardless of
`acl_enforced`. Supplying no principal returns everything, matching
pre-fix/single-user behavior; this check is independent of
`acl_enforced` because it is a narrower, always-on guarantee once a
principal is actually supplied, not a broader enforcement toggle.

### Memory write authorship (finding H3)

`written_by` on a memory record is always the resolved principal (per C3
above), never a raw request field — a caller cannot write a record
attributed to someone else. Separately, `memory.allow_org_scope_writes`
(default `true`) gates whether `org`-scope or steering-kind
(`alias`/`preference`/`exclusion`) records may be written over
HTTP/MCP at all: those are what every agent in the region treats as
shared ground truth, or uses to steer ranking on *every* query, so a
multi-tenant or exposed deployment can reserve authoring them for the CLI
or a hand-authored file. `POST /memory`'s audit event now records the
resolved principal as the actor, not a hard-coded `"ui"`.

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

## Sandboxed extraction cache (finding C5)

The WASM accelerators (`search.wasm_relationship_search`,
`graph.wasm_cross_source_resolution`) and the sandboxed PDF extractor cache
an ahead-of-time-compiled module, since compiling on every run would erase
the point of accelerating. That cache lives under the region's own
`state_path`, mode `0700`, never a shared system temp directory — loading
already-compiled native machine code from a location any local user or
co-tenant process could write to would mean whoever wrote the file executes
code in this process. Before loading, the cache file's ownership and
permissions are checked (refused if not owned by this process's own uid, or
if group/other-writable) and its recorded digest is verified against both
the source `.wasm` bytes and the serialized artifact. A tampered or
foreign-owned cache is refused loudly, never silently deserialized — the
accelerator just recompiles, which is a performance cost, not a
correctness one.

## Distributed sync integrity (finding H5)

`sync.concurrency.remote_worker_enabled` (off by default; relevant to the
scaled, role-split deployment) lets a stateless worker parse/chunk content
remotely. The coordinator derives the committed artifact's `id` and `path`
itself from what it already knows about the item being processed, rather
than trusting them off the wire — a compromised or misbehaving worker
cannot forge either to clobber another source's artifact row or, via
`GET /nodes/content`, cause an arbitrary file on the API host to be read
back. `GET /nodes/content` additionally re-validates any artifact path
against the configured allow-list before opening it, independent of how
the row reached the database.

## Document-parsing limits (finding M1)

Every archive-backed document format (`.docx`, `.pptx`, `.xlsx`, `.epub`)
reads each member through a single bounded reader (32 MB per member,
`pheasant.ingestion.office`) and parses it with a DOCTYPE-refusing XML
parser: `xml.etree.ElementTree`/pyexpat does not resolve external entities
or fetch DTDs (confirmed by execution — there is no XXE file-disclosure
risk here), but it does expand *internal* general entities, so a
DOCTYPE-declared "billion laughs" bomb in a few KB of XML still reaches
gigabytes if nothing refuses the DOCTYPE itself. No format this codebase
reads legitimately declares one, so refusing it outright has no effect on
real documents and needed no new dependency — a `TreeBuilder` whose
`doctype()` callback raises keeps this pure standard library, matching the
rest of the module.

## Deployment: Kubernetes and Compose (findings M3, M6)

`deploy/kubernetes/networkpolicy.yaml` narrows ingress on 8765 to a
labelled ingress/proxy namespace plus same-namespace traffic — an
unauthenticated API is otherwise reachable from any pod in the cluster,
not just the ones actually meant to reach it. Adjust the namespace label
to your own ingress controller. The egress rule's `0.0.0.0/0:443`
allowance is an intentional, coarse SSRF control (most cloud metadata
services serve on port 80, which this already excludes) and now excludes
`169.254.169.254/32` explicitly, so it does not depend on that being true
forever. `docker-compose.scale.yml`'s `POSTGRES_PASSWORD` has no default
fallback (Compose's `${VAR:?message}` syntax, same as
`PHEASANT_INDEX_WORKER_TOKEN` beside it) — a bare `docker compose up` with
no `.env` refuses to start rather than standing up Postgres with a
well-known password.

## Metrics endpoint (finding M7)

`GET /metrics` is unauthenticated by default, matching every other route
on a standalone/no-infrastructure region — Prometheus scraping should not
need a token in the common case. `security.metrics_token_env` names an
environment variable to require instead: when set, the route answers
`401` without a matching `Authorization: Bearer <token>` header
(constant-time compared), and `503` if the named variable is itself
unset — refusing rather than silently accepting anything.
