# Security audit — 2026-08-23

Line references below are true as of commit `e030550` (release 0.10.4).
Code moves; if a citation no longer lines up, trust the working tree and
treat the citation as approximate.

**Threat model.** This audit evaluates pheasant under the operator's stated
deployment posture: the hosting location provides network isolation (the
perimeter is intact), and the hosting identity is granted legitimate access
to the assets and services it needs. Severity below is relative to that
model — it asks only the question that matters under it:

> Given a correctly isolated network and a legitimately-privileged host
> identity, what can still go wrong?

**This audit does not re-litigate `docs/security.md`'s unauthenticated-API
posture.** That document states plainly that the HTTP API has no
authentication of its own, and that "who can open :8765" *is* the
authorization boundary (`docs/security.md:17`). That is a deliberate,
documented product decision. What follows takes it as given and asks what
still goes wrong on the other side of a correctly held perimeter.

The answer is that the perimeter protects **ingress**, and the most severe
findings here are not ingress issues:

- Several routes let a caller choose **which environment variable** becomes
  an outbound `Authorization` header and **where it is sent**. Network
  isolation does not stop a request the server itself makes; this converts
  the trusted host identity into the thing being stolen.
- The Phase-32 ACL system — the one feature designed to distinguish callers
  *inside* the perimeter — accepts the caller's identity as an unverified
  string, so it cannot enforce anything.
- Memory scope isolation, which `security/acl.py` documents as a guarantee,
  is bypassed entirely by two list surfaces.
- The control the whole model rests on (loopback bind) is defeatable from a
  browser, because no route validates the `Host` header.
- Two code paths load **precompiled native machine code** from a
  predictable, shared `/tmp` directory. That is a local trust boundary, and
  a network perimeter has nothing to say about it.

A prior scan (`tests/test_security_hardening.py`, 2026-07-31) closed seven
findings; those guards are still in place and are reused below rather than
rebuilt. Everything here is new.

Two claims were confirmed by execution rather than reading, and are called
out where they appear: `ET.fromstring` expands internal entities (M1), and
`sources.config_json` carries `connector.headers` into the Parquet export
(H6).

---

## Finding summary

| # | Severity | Finding | Primary location |
|---|---|---|---|
| C1 | Critical | Caller-chosen `api_key_env` + endpoint = arbitrary env-var exfiltration | `app.py:2866,3077,1311`; `vector_store.py:315`; `chat.py:703` |
| C2 | Critical | Unrestricted SSRF on every outbound URL, response reflected | `providers.py:138`; `vector_store.py:318`; `idp.py:37`; `events.py:101` |
| C3 | Critical | ACL principal is self-asserted — enforcement is unenforceable | `app.py:2225,2320`; `acl.py:91-124` |
| C4 | Critical | Memory scope isolation bypassed by both list surfaces | `app.py:1957`; `tools.py:527` |
| C5 | Critical | Precompiled WASM deserialized from shared `/tmp` → native code exec | `accel/loader.py:57-94`; `extractor_sandbox.py:120-165` |
| H1 | High | ACL coverage claim false for graph/structural routes | `app.py:2499-2650,1472,2411` |
| H2 | High | No `Host` validation → DNS rebinding; multipart CSRF on upload | `app.py:918-960,1739` |
| H3 | High | Forged memory author; anyone can steer every agent's ranking | `app.py:1868-1895` |
| H4 | High | `setup --accept-defaults` binds `0.0.0.0` unauthenticated | `schema.py:266` vs `quickstart.py:147` |
| H5 | High | Worker's `parsed.id`/`parsed.path` unvalidated → arbitrary file read | `engine.py:974-985,1666`; `app.py:2394` |
| H6 | High | `connector.headers` plaintext secret reaches `/config`, `/sources`, exports | `schema.py:1020`; `source_registry.py:30`; `analytics.py:156` |
| M1 | Medium | Unbounded DOCX member read + XML entity expansion | `extractor.py:397`; `office.py:76` |
| M2 | Medium | gRPC worker cannot serve TLS; token in cleartext | `grpc_worker.py:407` |
| M3 | Medium | NetworkPolicy admits every namespace | `deploy/kubernetes/networkpolicy.yaml:13` |
| M4 | Medium | Hardened path-policy config fails open on empty roots | `path_policy.py:13`; `app.py:405-422` |
| M5 | Medium | `follow_symlinks: true` escapes the resolved root | `walk.py:231-244` |
| M6 | Medium | Default Postgres password in the scale compose file | `docker-compose.scale.yml:24,33` |
| M7 | Medium | Unauthenticated `/metrics` and `repo-map` | `app.py:1174,1472` |

---

## Findings

Severity is relative to the stated threat model (perimeter intact, host
identity legitimately privileged).

---

### C1 — CRITICAL: Caller-chosen `api_key_env` + caller-chosen endpoint = arbitrary environment-variable exfiltration

**Problem.** Three surfaces let an unauthenticated caller specify *both* the
name of an environment variable to read *and* the URL it is sent to. The
server reads `os.environ[<caller's choice>]` and puts it in an
`Authorization` header aimed at `<caller's choice>`. Any secret in the
process environment leaves the box: cloud credentials,
`PHEASANT_INDEX_WORKER_TOKEN`, the Ed25519 `signing_key_ref` seed,
`IDP_TOKEN`, connector tokens.

This is the finding that matters most under the stated model. The perimeter
filters *inbound* connections; this is an *outbound* request the server
makes on its own behalf, using the very identity the hosting solution
granted it.

**Impacted files and code.**

1. `src/pheasant/api/app.py:2866` — `PUT /search/embeddings` accepts
   `base_url` and `api_key_env` from the body (`EmbeddingsRequest`,
   `app.py:257-271`) and applies them live via `_reload_vector_stack()`:
   ```python
   requested = {..., "base_url": req.base_url, "api_key_env": req.api_key_env, ...}
   for key, value in changes.items():
       setattr(settings, key, value)
   ```
   The payload lands here — `src/pheasant/search/vector_store.py:315-321`:
   ```python
   api_key = os.environ.get(self.api_key_env or "", "")
   if api_key:
       headers["Authorization"] = f"Bearer {api_key}"
   request = Request(f"{self.base_url}/embeddings", ...)
   ```
   Trigger: `POST /search/embeddings/reindex` (`app.py:3022`), or simply any
   subsequent `POST /search` in a vector/hybrid mode.

2. `src/pheasant/api/app.py:3077` — `PATCH /config/section/assistant`.
   `assistant` is live-applicable (`LIVE_APPLICABLE_SECTIONS`,
   `app.py:390-402`) so the swap takes effect with no restart:
   ```python
   setattr(config, section, getattr(candidate, section))
   ```
   `src/pheasant/assistant/chat.py:703-712` then resolves
   `env_name = settings.api_key_env or spec.api_key_env; api_key = env.get(env_name)`
   and `src/pheasant/assistant/providers.py:204` sends
   `headers = {"authorization": f"Bearer {key}"}` to `{base_url}/chat/completions`.
   Trigger: `POST /assistant/chat` (`app.py:3313`).

3. `src/pheasant/api/app.py:1311` — `POST /sources`. `connector.api_key_env`
   and `connector.api_endpoint` are per-source config, and every
   first-party connector reads the env var by that name and posts to that
   endpoint — `src/pheasant/connectors/gdrive.py:67-77`, `confluence.py:78-88`,
   `notion.py:116-126`, `slack.py:59`, `imap.py:48-65` (the IMAP one sends
   `user:password` to an arbitrary host/port). Trigger: `POST /sync/{id}`.

`PUT /config` (`app.py:2720`) reaches the same state by rewriting the file.

**Solution approach.**
- Add an **env-var allowlist** to the schema — e.g.
  `security.allowed_credential_envs: list[str]` — and resolve every
  `api_key_env` through one shared helper (new
  `src/pheasant/security/credentials.py::resolve_credential_env`) that
  refuses any name not on it. Seed the default from the names the code
  already knows: the `PROVIDERS[*].api_key_env` values
  (`assistant/catalog.py:19-38` — this is the live catalogue;
  `assistant/providers.py` re-exports it as `PROVIDERS = CATALOG_PROVIDERS`),
  the connector `DEFAULT_TOKEN_ENV` constants, and `IdPSettings.api_key_env`.
  This is the load-bearing fix: it decouples "which secret" from the
  request.
- Treat `api_key_env` and `api_endpoint`/`base_url` as **operator-only**
  fields: strip them from the request models on `PUT /search/embeddings`,
  `PATCH /config/section/*` and `POST|PUT /sources`, so they are settable
  in YAML and by the CLI but not over HTTP/MCP. `_config_write_roots`
  (`app.py:449-469`) is the existing precedent for "this permission is not
  that permission" and its docstring already argues the principle.
- Pair with C2 so the endpoint itself is constrained even when an operator
  does set it.

---

### C2 — CRITICAL: Unrestricted SSRF on every outbound URL, with response reflection

**Problem.** `require_fetchable_url` (`src/pheasant/sync/connectors.py:673-683`)
checks the **scheme only** — it was added to stop `file://` (a prior
finding) and does nothing about the target host. Every other outbound path
does not even call it. Nothing anywhere rejects loopback, link-local
(`169.254.169.254`), or RFC1918 destinations.

Under the stated model this is the direct route to the hosting identity's
credentials: an isolated network still lets the container reach its own
cloud metadata endpoint and its neighbours.

Response content is reflected back to the caller, so this is not blind:
`src/pheasant/assistant/providers.py:131-133`
```python
except urllib.error.HTTPError as exc:
    detail = exc.read().decode("utf-8", "replace")[:600]
    raise ProviderError(f"{exc.code} from provider: {detail}") from exc
```
`urllib` also follows redirects and downgrades POST→GET on 301/302/303, so
an attacker-controlled `base_url` that redirects reaches GET-only endpoints
such as IMDSv1 and returns the body.

**Impacted files and code.**
- `src/pheasant/assistant/providers.py:138-164` (`complete` → `_http_json`,
  `urllib.request.urlopen`, no host check) — reached via `POST
  /assistant/chat`.
- `src/pheasant/search/vector_store.py:318-325` — reached via
  `POST /search/embeddings/reindex` and vector search.
- `src/pheasant/security/idp.py:37-43` (`_http_get_json`) — reached via
  `POST /security/idp/sync` (`app.py:2108`), sends the IdP bearer token.
- `src/pheasant/connectors/*.py` — all five, via `POST /sync/{id}`.
- `src/pheasant/sync/connectors.py:686-713` (`_urlopen`) — `web_collection`/
  `api` connectors; scheme-checked, host-unchecked.
- `src/pheasant/synapse/events.py:101-119` — `synapse.router_url` has **no
  validation at all**, and `synapse` is live-applicable
  (`LIVE_APPLICABLE_SECTIONS`), so `PATCH /config/section/synapse` retargets
  the webhook — and the contract/corpus metadata it carries — unauthenticated
  and without a restart.
- Contrast with the one place that gets it right:
  `src/pheasant/sandbox/wasm_runtime.py:113-125`, which requires an explicit
  `connector.allowed_hosts` match before any guest fetch. Even there the
  check is pre-request only, so an allowlisted host that redirects or
  resolves internally is unfiltered.

**Redirects specifically.** `require_fetchable_url` runs before the
request; `urllib` then follows redirects with its own handler, which
permits `ftp://` targets. So the `file://` guard closed by the prior finding
is reachable around via a 302 to a scheme `FETCHABLE_SCHEMES` was written to
exclude.

**Solution approach.**
- Extend `require_fetchable_url` into a shared
  `security/egress.py::require_egress_allowed(url)` that keeps the existing
  scheme allowlist and adds: DNS-resolve the host and reject
  loopback/link-local/multicast/RFC1918/CGNAT/IPv6-ULA unless the operator
  opts in (`security.allow_private_egress: false` by default); reject
  redirects to a newly-disallowed target by installing a custom `urllib`
  opener rather than relying on the default redirect handler.
- Call it from **every** site listed above — `providers._http_json`,
  `vector_store._embed_batch`, `idp._http_get_json`,
  `sync/connectors._urlopen`, and each first-party connector's
  `_base_url()`.
- Stop reflecting the upstream body: replace the 600-char echo in
  `providers.py:131-133` with the status code plus a log-only body.
- `docs/security.md` "Remote fetching" currently documents only the
  `file://` refusal; extend that section.

---

### C3 — CRITICAL: ACL enforcement is unenforceable — the principal is a self-asserted string

**Problem.** `security.acl_enforced` exists to distinguish callers *inside*
the perimeter (the Synapse router fanning out to a region, multi-agent
tenants). But the principal and its groups arrive **in the request body**,
and the region unions them straight into the identity set. Any caller is any
principal.

`src/pheasant/security/acl.py:16-18` states the intent — "the region
enforces *visibility*; the caller … authenticates the principal" — but
nothing in the repo authenticates it, and the docs name the deployment
perimeter as the authenticator. A perimeter cannot authenticate a field in a
JSON body it does not inspect. So the feature is advisory in every shipped
configuration.

Worse, with `default_visibility: "private"` — the setting an operator picks
to *tighten* things — `acl.py:122-124` grants all un-ACL'd artifacts to any
non-empty principal string:
```python
if acl is None:
    return identities is not None and bool(identities)
```
`principal: "user:x"` unlocks the corpus.

**Impacted files and code.**
- `src/pheasant/api/app.py:2225` `POST /search` → `principal=req.principal,
  principal_groups=req.principal_groups`; same at `app.py:2288`
  `/relevant-files` and `app.py:3313` `/assistant/chat`.
- `src/pheasant/api/app.py:2320-2349` `_acl_guard(artifact_id, principal, …)`;
  `principal` is a query parameter on `GET /nodes/content` (`app.py:2378`)
  and `GET /files/summary` (`app.py:2351`).
- `src/pheasant/security/acl.py:91-105` `expand_principal` — merges
  caller-supplied `groups` with no verification.
- `src/pheasant/search/hybrid.py:220-253` — the filter itself is correct;
  its input is not.

**Solution approach.** The operator has confirmed the remediation direction:
make ACL enforcement real via `security.principal_source`, not document the
feature as advisory. Concretely:
- Make the principal **provable** rather than declared. Add
  `security.principal_source` with three modes:
  - `body` — today's behavior, retained for the library/CLI and
    single-user regions, but **rejected at startup when
    `acl_enforced: true`** so the unsafe combination cannot be configured
    silently;
  - `header` — trust a named header (e.g. `X-Pheasant-Principal`) that an
    authenticating ingress sets, and **ignore the body field entirely**;
  - `signed` — verify a signed assertion from the Synapse router using the
    Ed25519 machinery already present in `src/pheasant/synapse/signing.py`
    (`signing_bytes`/`sign_body` are there; only a verify helper is
    missing). This is the mode that makes the router fan-out story real.
- Drop `principal_groups` from every request model when the mode is not
  `body`; groups come from `security.groups` and the IdP sync
  (`security/idp.py`), which are already the trustworthy sources.
- Fix the `default_visibility: "private"` fall-through at `acl.py:122-124`
  so a bare authenticated principal does not stand in for an explicit
  grant.

---

### C4 — CRITICAL: Memory scope isolation is bypassed by both list surfaces

**Problem.** `src/pheasant/security/acl.py:47-63` documents a hard
guarantee for agent memory: `org` scope is shared, but `user` and `session`
records "were written by and for one principal, so they are readable only
by their writer" — added specifically because ACL enforcement otherwise
"filtered every corpus document by principal while leaving one agent's
private notes readable by every other agent in the same region."

The two routes that list memory records do exactly that. Neither accepts a
principal, and neither consults the ACL:

`src/pheasant/api/app.py:1957-1982` — `GET /memory`:
```python
records = MemoryStore(source.path).list_records(scope, current_only=current_only)
```
`src/pheasant/mcp_server/tools.py:527-544` — `memory_list`, identical.

Every `user:`- and `session:`-scoped record from every principal is
returned in full, regardless of `acl_enforced`. This is not a documented
tradeoff — it contradicts the guarantee in the same repo. It is the finding
least dependent on any threat-model assumption.

**Solution approach.**
- Give both routes a principal parameter and filter through the existing
  `normalize_acl("memory", …)` + `is_allowed(...)` pair from
  `security/acl.py` — the same primitives `_acl_guard` uses — so there is
  one rule, not two.
- Default to returning `org`-scope records only when no principal is
  supplied, rather than everything.
- Land this behind C3 so the principal being filtered on is trustworthy.

---

### C5 — CRITICAL: Precompiled WASM cache is deserialized from a predictable shared `/tmp` path → native code execution

**Problem.** Two modules cache an ahead-of-time-compiled WASM module under
`tempfile.gettempdir()/pheasant-wasm-cache/` and load it with
`wasmtime.Module.deserialize_file`. That call loads **already-compiled
native machine code**; wasmtime documents it as unsafe on untrusted input,
and it bypasses validation entirely — none of the fuel metering, memory
ceiling or import-denial ceremony in `sandbox/wasm_runtime.py` applies to a
deserialized artifact. Whoever writes that file executes code in the
pheasant process.

The path is fully predictable: the digest is taken over the **vendored**
`accel.wasm` bytes, so the filename is identical on every install of a
given version. And the directory is created with
`mkdir(parents=True, exist_ok=True)` — default mode, no ownership or mode
check, `exist_ok` silently accepting a directory an attacker pre-created.
On a shared host `/tmp` is world-writable, so an unprivileged local user
(or a co-tenant process) can create the directory and drop the artifact
before pheasant first runs. The `.tmp` staging write in the same directory
is symlink-followable.

`except wasmtime.WasmtimeError` catches architecture mismatches, not
malice.

**Impacted files and code.**

`src/pheasant/sandbox/accel/loader.py:57-94`:
```python
def _cache_dir() -> Path:
    return Path(tempfile.gettempdir()) / "pheasant-wasm-cache"

def _cache_path(wasm_bytes: bytes) -> Path:
    digest = hashlib.sha256(wasm_bytes).hexdigest()[:16]
    return _cache_dir() / f"accel-{digest}.cwasm"
...
def _load_module(engine, wasm_bytes):
    cache_path = _cache_path(wasm_bytes)
    if cache_path.exists():
        try:
            return wasmtime.Module.deserialize_file(engine, str(cache_path))
```
and the write side at `loader.py:70-74` (`cache_path.parent.mkdir(...)`,
`tmp_path.write_bytes(module.serialize())`).

`src/pheasant/ingestion/extractor_sandbox.py:120-165` — the same pattern
with an `extract-<digest>.cwasm` prefix; `deserialize_file` at line 152,
`mkdir` at 160.

Reachable whenever the `[wasm]` extra is installed — which is the
**published image's default** (`Dockerfile`,
`PHEASANT_EXTRAS=mcp,agent,vector,wasm,...`), and `docker-entrypoint.sh`
explicitly turns both accelerators on when wasmtime imports.

**Scope note.** In the Kubernetes manifests `readOnlyRootFilesystem: true`
with no `/tmp` emptyDir means the cache write fails soft and the load never
happens — those deployments are not exposed. The exposure is the
bare-metal/`pip install` path and any container where `/tmp` is shared or
writable by more than one identity.

**Solution approach.**
- Move the cache out of the shared temp dir into a pheasant-owned location
  derived from `config.pheasant.state_path` —
  `src/pheasant/persistence/paths.py` already centralizes state-path
  derivation and should own this.
- Create it with mode `0o700`, and before loading, `os.stat` the file and
  refuse it unless it is a regular file owned by the current uid with no
  group/other write bit. Refuse rather than fall back silently, so a
  tampered artifact is loud.
- Verify integrity: alongside the `.cwasm`, store the sha256 of the source
  `accel.wasm` **and** of the serialized artifact, and check both before
  `deserialize_file`. A hash over the input alone does not attest the
  output.
- Write via `tempfile.mkstemp` in the target directory (O_EXCL, no
  predictable name) instead of a fixed `.tmp` sibling.
- Both call sites are near-identical; factor one loader and have
  `extractor_sandbox.py` use it, so this cannot drift again.

---

### H1 — HIGH: ACL coverage claim is false for the graph and structural routes

**Problem.** `docs/security.md` states: "Enforcement covers **every**
surface that returns indexed content." Several routes returning indexed
content have no ACL guard at all — they leak file paths, headings, symbol
names, entity names and corpus structure whether or not `acl_enforced` is
set.

**Impacted files and code** (all `src/pheasant/api/app.py`):
`GET /graph` (2499), `GET /graph/export/node-link-json` (2520) and
`/cytoscape-json` (2524) — **unbounded whole-graph dumps**, `GET
/graph/neighbors` (2528), `/graph/slice` (2544), `/graph/diagnostics`
(2572), `/graph/path` (2624), `GET /nodes/explain` (2650), `GET /taxonomy`
(2411), `GET /sources/{id}/repo-map` (1472). Also `GET /files/summary`
(2351) and `GET /nodes/content` (2378) accept `principal` but **not**
`principal_groups`, so a group-derived grant cannot be presented there even
though `/search` accepts one.

**Solution approach.** Route the node sets through one ACL-aware projection
helper (extend the filter in `src/pheasant/search/hybrid.py:220-253` into a
reusable `filter_nodes_for_principal`) and apply it in the graph exporters.
Accept `principal_groups` on the two content routes for parity. Where a
route genuinely cannot be filtered, gate it off under `acl_enforced` rather
than leaving the docs wrong.

---

### H2 — HIGH: No `Host` validation — DNS rebinding defeats the loopback-bind control

**Problem.** `docs/security.md` names bind address as the primary
compensating control. A browser defeats it: no route validates the `Host`
header, and there is no `TrustedHostMiddleware` — `create_app` installs
only `CORSMiddleware` and the `bound_concurrency` limiter
(`src/pheasant/api/app.py:918-960`). A page the user visits can rebind its
own hostname to `127.0.0.1`, at which point the browser treats it as
same-origin: CORS never applies, and the full API — read the index,
rewrite the config, register a source over `/` — is scriptable.

The MCP mount already guards against exactly this
(`src/pheasant/mcp_server/server.py:583-621` widens FastMCP's
DNS-rebinding `allowed_hosts`), so the protection exists for `/mcp` and is
absent for the other ~68 routes. Note also that turning on
`cors_allow_all_origins` **disables** the MCP guard too
(`server.py:604-608`).

Separately, `POST /sources/upload` (`app.py:1739`) takes
`multipart/form-data`, a CORS-simple content type — a cross-origin form
POST needs no preflight, so a malicious page can write files into the
corpus and trigger a sync today, with no rebinding required.

**Solution approach.** Add `TrustedHostMiddleware` seeded from the same
`server.api.cors_origins` netlocs the MCP guard already derives
(`server.py:598-620` — reuse that host-derivation, do not write a second
one), plus `localhost`/`127.0.0.1`/the container hostname. For the CSRF
vector, require a non-simple header (e.g. `X-Pheasant-Request: 1`) on
state-changing routes, or an `Origin` check for the mutating verbs.

---

### H3 — HIGH: Memory writes accept a forged author and let anyone steer every agent's ranking

**Problem.** `src/pheasant/api/app.py:1868-1895` passes
`written_by=req.principal` straight from the request body. Combined with C3
there is no author integrity: any caller can write a memory record
attributed to any principal.

Memory records are what agents treat as ground truth, and
`alias`/`preference`/`exclusion` steering records at `org` scope change
ranking for *every* query in the region (see the Memory section of
`CLAUDE.md`). So an unauthenticated writer can persistently redirect what
every agent in the region retrieves — a durable prompt-injection primitive
that survives restarts because records are source content on disk.

`docs/security.md`'s "Prompt injection posture" covers *indexed*
documents; it does not cover records the region itself asserts as fact.

**Solution approach.** Derive `written_by` from the authenticated principal
(C3), never from the body. Gate `org`-scope and steering-kind writes behind
an explicit `memory.allow_org_scope_writes` / operator-only permission,
defaulting to session/user scope for API callers. Record the resolved
principal in the audit event (`app.py:973-995` currently hard-codes actor
`"ui"`/`"http"`).

---

### H4 — HIGH: `pheasant setup --accept-defaults` binds `0.0.0.0` unauthenticated

**Problem.** Two config generators disagree.
`src/pheasant/quickstart.py:142-147` deliberately writes
`server.host = "127.0.0.1"` with a comment explaining why. `pheasant setup
--accept-defaults` reads defaults off the live schema, and
`src/pheasant/config/schema.py:266` is `host: str = "0.0.0.0"` — so the
non-container install path generates a config that binds every interface
with no authentication. The container is fine (compose publishes to
loopback), but the `pip install` path documented in `CLAUDE.md` §3 is not.

**Solution approach.** Flip the schema default to `127.0.0.1` and have the
container set `0.0.0.0` explicitly — `docker-entrypoint.sh` already passes
`--answers` for the WASM flags (`docker-entrypoint.sh`, `WASM_ANSWERS`), so
add `server.host` there and keep one code path for "what does a default
config look like". Emit a startup warning when `host` is non-loopback and
no ingress auth is configured.

---

### H5 — HIGH: A remote worker's `parsed.id` and `parsed.path` are committed unvalidated → arbitrary file read

**Problem.** `CLAUDE.md` states the guarantee plainly: "Remote preparation
is an *optimization*: no arrangement of worker failures may change what a
sync produces." The validation at `src/pheasant/sync/engine.py:974-985`
checks three fields of a worker's answer — `source_id`, `relative_path`,
`sha256` — and its docstring shows the author reasoning about exactly this
risk ("an answer for the wrong file would be committed under this item's
stable ID"). It does not check `id` or `path`, and both are committed
verbatim at `engine.py:1666-1672`:

```python
artifact_row = {
    "id": parsed.id,
    "source_id": parsed.source_id,
    ...
    "path": str(parsed.path),
```

Both arrive as attacker-controlled strings off the wire —
`src/pheasant/sync/remote_worker.py:60-77` does `str(payload["id"])`,
`str(payload["path"])` with no constraint.

Two consequences:
- **Row clobber.** A forged `id` overwrites another source's artifact row
  and its graph node — a cross-source integrity break that the idempotency
  spine would not catch, because the sync reports success.
- **Arbitrary file read.** `path` is later opened straight off disk by an
  unauthenticated route — `src/pheasant/api/app.py:2394-2400`:
  ```python
  artifact_rows = state.rows("SELECT path FROM artifacts WHERE id=? LIMIT 1", (node_id,))
  if artifact_rows:
      path = Path(artifact_rows[0]["path"])
      if path.exists() and path.is_file():
          content = read_text(path)
  ```
  So a compromised worker — or anyone who recovers the worker token off
  the cleartext transport in M2 — reads any file the API process can
  reach, through `GET /nodes/content`.

Not reachable in the default topology
(`sync.concurrency.remote_worker_enabled` is off, `--role all` is the
default), which is why this is High rather than Critical. It is squarely in
scope for the scaled deployment this repo publishes.

**Solution approach.**
- Extend the existing check at `engine.py:974-985` to cover both fields:
  recompute the expected `id` from the local `source.name` /
  `item.relative_path` / branch using the same grammar as
  `ingestion/pipeline.py:440,515` and reject any mismatch; require
  `parsed.path` to equal the path the coordinator itself resolved for the
  item, never the worker's copy.
- Better still, **do not accept them from the wire at all** — a worker's
  job is parse/chunk, so drop `id` and `path` from `parsed_from_wire`
  (`remote_worker.py:60-77`) and have the coordinator fill both in locally.
  That removes the class rather than validating it.
- Independently, defend the read side: `GET /nodes/content` should run
  `artifacts.path` through `resolve_under(_allowed_roots(config))` before
  opening it, so a bad row cannot become a file read regardless of how it
  got there.

---

### H6 — HIGH: `connector.headers` is a plaintext-secret sink that reaches `/config`, `/sources` and the Parquet export

**Problem.** The credential convention in this repo is excellent and
near-total: every secret is an **env-var name** in YAML, never a value —
`connector.api_key_env`, `search.embeddings.api_key_env`,
`security.idp.api_key_env`, `sync.concurrency.remote_worker_token_env`,
`synapse.signing_key_ref`, `storage.dsn_env` (with
`src/pheasant/persistence/secrets.py:1-10` documenting that there is
deliberately no `storage.dsn` field).

`src/pheasant/config/schema.py:1020` is the one hole:
```python
headers: dict[str, str] = field(default_factory=dict)
```
It is sent on every fetch (`sync/connectors.py:316,700`), so
`Authorization: Bearer …` is the obvious thing to put there, and there is
no env-var indirection offered for it. From there it reaches three sinks:

1. `GET /config` (`app.py:2707-2718`) returns `raw_yaml` — the config file
   verbatim — and the full effective config. There is no redaction pass
   anywhere in the codebase for config output; the only `redact*` helpers
   cover DSNs (`persistence/secrets.py:45-60`) and worker URLs.
2. `GET /sources` and MCP `list_sources` — `SourceRegistry.register_source`
   stores `source.model_dump(mode="json")` into `sources.config_json`
   (`src/pheasant/registry/source_registry.py:30-39`), and the list
   surfaces return it.
3. **The Parquet export.** `sources` is in `SQL_TABLES`/`DEFAULT_TABLES`
   (`src/pheasant/analytics.py:156,184`) and the projection is the
   declared column list, which includes `config_json`
   (`persistence/schema.py:50`). `/exports` is, in this repo's own words,
   "the one volume meant to be read by something that is not pheasant." I
   confirmed this by execution: `config_json` on the exported `sources.parquet`
   row for a source configured with `connector.headers` carries the header
   value verbatim.

The export map's docstring (`analytics.py:141-155`) already reasons
carefully about this exact hazard for identity data — `idp_groups`,
`idp_sync_meta` and `source_audit_events` are excluded because "an export
is a file people pass around; identity and audit data is not that." The
same reasoning was simply not applied to `config_json`.

Worth noting `task_payload` (`sync/remote_worker.py:86-96`) explicitly does
**not** forward `connector.headers` to workers, with a comment saying why —
so the sensitivity of this field is already understood in one place.

**Solution approach.**
- Add `connector.header_env` (a name → env-var-name map) alongside
  `headers`, resolved at fetch time through the same C1 credential helper,
  and document `headers` as non-secret metadata only.
- Redact on the way out, not just at the source: add a `redact_config()`
  pass used by `GET /config`, `GET /config/effective`, `GET /sources` and
  MCP `list_sources`, masking `headers` values and anything matching the
  existing secret-ish key patterns.
- Drop `config_json` from the `sources` projection in `analytics.py` —
  extend the existing per-table column selection rather than adding a new
  mechanism — and extend the docstring's exclusion rationale to cover it.

---

### M1 — MEDIUM: Document-parsing DoS — unbounded DOCX member read, and XML entity expansion

**Problem, part one — the DOCX size-bound asymmetry.**
`src/pheasant/ingestion/office.py:59-73` (`_read_member`) checks
`info.file_size` against `MAX_MEMBER_BYTES = 32 MB` (`office.py:49`) before
reading, and `msdoc.py:55` bounds OLE streams. The DOCX reader has
**neither** check — `src/pheasant/ingestion/extractor.py:397-403`:
```python
with zipfile.ZipFile(io.BytesIO(content)) as archive:
    names = set(archive.namelist())
    if "word/document.xml" not in names:
        return ""
    xml_bytes = archive.read("word/document.xml")   # no size bound
```
A `.docx` whose `word/document.xml` inflates to gigabytes OOMs the process.

**Problem, part two — entity expansion.** Every OOXML/EPUB parse uses
stdlib `xml.etree.ElementTree` with no entity guard — `office.py:40,76-84`
(`_parse_member` → `ET.fromstring`) and `extractor.py:408`. `defusedxml`
is not a dependency anywhere in the repo.

ElementTree/pyexpat does **not** resolve external entities or fetch DTDs,
so XXE file disclosure is *not* present — worth stating so the fix is
scoped correctly. But I confirmed by execution against this environment's
interpreter that it **does** expand internal general entities, so a
`<!DOCTYPE … [<!ENTITY …>]>` bomb in a 5 KB `document.xml` still reaches
gigabytes. The 32 MB member cap bounds the *input*, never the expansion.

Both are reachable unauthenticated: `POST /sources/upload` registers
uploads with `include: ["**/*"]` (`app.py:1800`), and `web_collection`
plus the SaaS connectors pull untrusted documents by design. Killing the
indexer takes any in-flight sync with it.

**Solution approach.**
- Route the DOCX read through the existing `office._read_member` rather
  than a second hand-rolled `archive.read` — the bound already exists,
  this path just does not use it.
- For entities: add `defusedxml` and swap the `ET.fromstring` call sites
  for `defusedxml.ElementTree.fromstring` (it refuses DTDs outright), or —
  to keep the stdlib-only property `office.py:8-9` documents — install an
  `XMLParser` whose `EntityDeclHandler` rejects internal entity
  declarations.
- Pin both with crafted fixtures next to the existing extractor tests.

---

### M2 — MEDIUM: gRPC preparation worker cannot terminate TLS; shared token in cleartext

**Problem.** `src/pheasant/sync/grpc_worker.py:407` binds with
`server.add_insecure_port(...)` and there is no secure-port path, while the
client *can* be secure (`grpc_worker.py:171-172`). So the long-lived
shared bearer token (`_authenticate`, `grpc_worker.py:314-336` —
correctly constant-time) and every file body being indexed cross the pod
network in plaintext, replayable by anything on it.
`deploy/kubernetes/scaled/README.md:81` already flags that there is no
NetworkPolicy for the worker port.

**Solution approach.** Add `sync.concurrency.grpc_tls` (cert/key paths)
and call `add_secure_port` with `grpc.ssl_server_credentials` when set;
refuse to start insecure when the token is configured and the bind host is
not loopback.

---

### M3 — MEDIUM: The shipped NetworkPolicy does not deliver the isolation the model assumes

**Problem.** The trust model makes network isolation load-bearing, but
`deploy/kubernetes/networkpolicy.yaml` admits ingress on 8765 from
`namespaceSelector: {}` — **every namespace in the cluster**. With no
authentication on the API, every pod in the cluster can rewrite the config
and read the corpus. (Pod hardening itself is good:
`deploy/kubernetes/deployment.yaml` and the scaled manifests all set
`runAsNonRoot`, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`,
`drop: ALL`.)

The egress rule is better — `0.0.0.0/0` only on 443 plus in-cluster —
which happens to block IMDS on port 80 where it is applied; that is worth
stating explicitly rather than leaving as a coincidence.

**Solution approach.** Narrow ingress to a labelled ingress/proxy
namespace and ship the permissive rule commented out. Document the egress
rule as an intentional SSRF control in `docs/security.md`, and add an
explicit `169.254.169.254/32` deny so the mitigation does not depend on the
port coincidence.

---

### M4 — MEDIUM: The hardened path-policy configuration fails open

**Problem.** `resolve_under` (`src/pheasant/security/path_policy.py:10-23`)
is itself correct — it calls `.resolve()` on candidate *and* roots before
comparing, so symlink escapes fail, and containment is component-wise
(`root in candidate.parents`), so `/workspace-evil` is not treated as
under `/workspace`. But it has a fail-open branch:
```python
if not roots:
    return candidate
```
And `_allowed_roots` (`src/pheasant/api/app.py:405-422`) **drops roots
that do not exist**:
```python
if resolved not in seen and resolved.exists():
    seen.append(resolved)
```

So the operator who does the right thing — sets
`allow_user_selected_source_paths: false` with explicit
`allow_workspace_roots` — and then mistypes a root or forgets to mount it
gets an empty list, and the policy silently allows everything. The
hardened config degrades to *weaker* than the default. Note the three call
sites disagree about this: `_configured_roots` (`app.py:425-446`) and the
MCP path (`mcp_server/tools.py:111-118`) do not filter on existence.

**Solution approach.** Make `resolve_under` raise on an empty root list
rather than returning the candidate — "no roots" means "nothing is
allowed", not "everything is". Then fix the callers to pass configured
roots regardless of existence (a missing root simply matches nothing), and
log a startup warning naming any configured root that is not mounted.
Reconcile the three call sites on one helper.

---

### M5 — MEDIUM: `follow_symlinks: true` escapes the resolved root

**Problem.** `src/pheasant/ingestion/walk.py:231-244` computes `realpath`
when `follow_symlinks` is on, but only for **loop detection** — it is
never compared against the source root:
```python
if follow_symlinks:
    real = os.path.realpath(entry.path)
    if real in seen_real_dirs:
        continue
    seen_real_dirs.add(real)
```
So with the flag on, a symlink inside an allow-listed corpus reads and
indexes content outside every allowed root — defeating the `resolve_under`
check that was applied to the source path. Safe today only because the
default is `False` (`config/schema.py:548`), where
`entry.is_file(follow_symlinks=False)` skips symlinks entirely.

**Solution approach.** Reuse `resolve_under` inside the walk: when
following a symlink, require `realpath` to stay under the source root (and
under the configured allow-list when `allow_user_selected_source_paths` is
off), skipping with a warning otherwise. Add the escape case to the walk
tests.

---

### M6 — MEDIUM: `docker-compose.scale.yml` ships a default Postgres password

**Problem.** `docker-compose.scale.yml:24,33`:
```yaml
PHEASANT_DATABASE_URL: postgresql://pheasant:${POSTGRES_PASSWORD:-pheasant}@postgres:5432/pheasant
...
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-pheasant}
```
`docker compose -f docker-compose.scale.yml up` with no `.env` silently
stands up Postgres with the password `pheasant`. The line immediately
above it gets this right —
`PHEASANT_INDEX_WORKER_TOKEN: ${PHEASANT_INDEX_WORKER_TOKEN:?set a shared
worker token}` fails closed.

**Solution approach.** Apply the same `:?` treatment to
`POSTGRES_PASSWORD`. It is a one-character-class change and makes the two
adjacent secrets behave consistently.

---

### M7 — MEDIUM: Unauthenticated `/metrics` and structural read routes

**Problem.** `GET /metrics` (`src/pheasant/api/app.py:1174`) exposes
Prometheus data with no auth, and `GET /sources/{id}/repo-map`
(`app.py:1472`) returns every indexed file path and size — a corpus map
for anything that reaches the port.

**Solution approach.** Put `/metrics` behind the same operator gate as the
worker routes (`_authorize_worker`, `app.py:1048-1064`, is the existing
pattern), or bind it to a separate port. Fold `repo-map` into the H1 ACL
projection.

---

### L — LOW / defence-in-depth

- **Upload memory DoS.** `app.py:1769` does `data = await upload.read()` —
  each file is buffered fully in memory *before* `store_upload`'s
  `max_bytes` check (`src/pheasant/api/uploads.py:146`). Stream to disk and
  check as you go.
- **`pheasant export query` is unrestricted DuckDB.** `analytics.py:862-907`
  executes free-form SQL against a bare `duckdb.connect(":memory:")`
  connection with **no security options set at all** — no `memory_limit`,
  no `enable_external_access=false`, no `disabled_filesystems`, no
  `lock_configuration`. (The export *write* path at `analytics.py:660-661`
  does set `memory_limit`/`temp_directory` on its own connection, but the
  query path does not share that configuration — it inherits nothing.) So
  `read_csv('/etc/passwd')`, `COPY … TO '…'` and `INSTALL httpfs` all work.
  **CLI-only today** — I confirmed nothing in `api/` or `mcp_server/`
  imports `analytics` — so it is a local user who already has a shell, not
  a vulnerability. It becomes RCE the moment anyone wires it to HTTP or an
  MCP tool, and the docstring calling it "a documented query surface"
  invites exactly that. Harden the connection now and leave a comment
  saying why. (The `export --table` path is clean: `_quote`
  `analytics.py:486-491`, `_sql_literal` `480-483`, `resolve_tables`
  validates against `EXPORTABLE`.)
- **zstd decompression bombs.** `persistence/graph_store.py:143` and
  `backup.py:160-162` decompress with no `max_output_size`. Local `/state`
  only.
- **`kb_id` path building.** `persistence/graph_store.py:51-54` does
  `self.root / kb_id` unsanitized. Config-derived today; a hazard only if
  `kb_id` ever becomes request-controlled — worth a guard before that
  happens.
- **`connector.wasm_module_path`** (`sandbox/connector.py:63-66`) is an
  unvalidated read of any config-named path; fold into the path policy.
- **`git://` in `CLONE_SCHEMES`** (`targets.py:124`) is unauthenticated and
  unencrypted; consider dropping it.
- **No central `..` rejection on `ConnectorItem.relative_path`.**
  `_allows_relative_path` (`sync/connectors.py:182-187`) applies only
  include/exclude/depth. Filesystem, web, Notion and GDrive all sanitize
  (`_safe_segment`, `_slug`, `relative_to`), but Slack (`slack.py:94`),
  Confluence (`confluence.py:120`) and IMAP (`imap.py:109`) interpolate raw
  remote values. Harmless today because those relpaths are never joined
  onto a filesystem root — but `sandbox/connector.py:116` does
  `self._root / item.relative_path`, so the pattern is one caller away
  from a traversal. Add the rejection once, centrally.
- **UI sidecar runs as root.** `ui/Dockerfile:18-26` is `FROM
  nginx:1.27-alpine` with no `USER`; `nginxinc/nginx-unprivileged` closes
  it. Optional `--profile ui` only — the main image is correctly non-root.
- **Worker replicas serve the whole API.** `deployment/roles.py:34-39`
  deliberately does not hide routes per role, so a `--role worker` pod
  still exposes `/config`, `/fs/list` and `POST /sources`
  (`deploy/compose/worker.yaml:16-23` sets `host: 0.0.0.0`,
  `api.enabled: true`). Given M3, worth narrowing.

---

## Verified clean (recorded so the negatives are explicit)

These were checked specifically and found sound — do not spend remediation
time here:

- **No SQL injection.** Every f-string in `persistence/` and `search/`
  interpolates only module constants or `",".join("?" …)` placeholder runs
  (`state_store.py:291,310,984,1039,1095-1103`,
  `search/sqlite_store.py:307-371`, `memory/policy.py:372`). User values —
  query, `source_name`, `section`, `principal`, scopes — are all bound.
  `PRAGMA table_info({table})` (`backends.py:178`) has four callers, all
  passing literals or `EXPORTABLE`/`TABLE_ORDER` members.
- **FTS5 `MATCH` is not injectable.** `sqlite_store.py:289` builds the
  match expression from alphanumeric-only tokens; the raw-text fallback is
  a bound parameter, so the worst case is a parse error caught at `:374`
  and degraded to the LIKE path.
- **No `pickle`, `marshal`, `eval`, `exec`, `os.system`, or `shell=True`**
  anywhere in `src/` or `scripts/`. All nine YAML call sites use
  `safe_load`.
- **Subprocess use is safe.** List-form argv, `--` terminator
  (`targets.py:519-534`), `validate_clone_url` rejecting leading `-` and
  transport helpers (`targets.py:130-167`), `protocol.allow=never` and the
  token passed via `GIT_CONFIG_VALUE_N` rather than argv
  (`targets.py:456-505`).
- **Plugin loading is not attacker-directed.** `entry_points(group=…)`
  over installed distributions with an `issubclass(SourceConnector)` check
  (`sync/connector_registry.py:56-73`); `sources[].type` selects from that
  fixed set. No `importlib.import_module` on user input anywhere.
- **Tar extraction is genuinely well done** (`persistence/backup.py:101-121`):
  name check, `_is_within` resolve check, symlink/hardlink target check,
  *and* `filter="data"`.
- **Upload filename handling** (`api/uploads.py:49-97`): basename-only,
  NFC normalize, separator/control-char denylist, Windows reserved names,
  byte-counted truncation, `unique_path` de-dup.
- **Both credential checks that exist are constant-time** —
  `hmac.compare_digest` at `api/app.py:1063` and `sync/grpc_worker.py:335`,
  each failing closed when the token env var is unset.
- **The session-key vault is sound** (`assistant/credentials.py`):
  `secrets.token_urlsafe(32)`, in-memory only, TTL-swept, `redacted()` on
  every response path, and no cross-contamination — a session `base_url`
  is only ever used with that session's own key (`assistant/chat.py:679-690`).
- **Connector secrets never enter config or state** — all five
  first-party connectors resolve through
  `os.environ[connector.api_key_env]`. (The *choice* of env var is the C1
  problem; the indirection itself is right.)
- **Container hardening is good**: non-root uid 10001, `runAsNonRoot`,
  `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `drop: ALL`
  across `deploy/kubernetes/` and `deploy/kubernetes/scaled/`.
- **No unsafe TLS anywhere** — no `verify=False`, no
  `_create_unverified_context`, no `CERT_NONE`. (M2 is a missing TLS
  *option*, not a disabled check.)
- **No `extractall` on document archives**, and `office._resolve_relative`
  (`office.py:178-193`) correctly refuses `..` escapes — no zip-slip.

---

## Recommended sequencing

1. **C1 + C2 together** — one shared egress/credential chokepoint
   (`security/egress.py`, `security/credentials.py`), then convert every
   call site. Highest severity, and the two findings share a fix surface.
   **H6** rides along on the credential half.
2. **C5** — self-contained, no design questions, and the only finding
   whose impact is native code execution. Factor one loader for both call
   sites.
3. **C4** — smallest diff; the `org`-only default needs no dependency on
   C3.
4. **C3** — the schema + validation work that makes ACL enforcement real,
   then **H1** and **H3** on top of it.
5. **H5** — drop `id`/`path` from the worker wire format, plus the
   `/nodes/content` read-side guard. Pairs naturally with **M2** (same
   transport).
6. **H2, H4, M4, M5, M6** — perimeter and path-policy posture.
7. **M1** — independent, self-contained; can land any time.
8. **M3, M7**, then the Low items.

---

## Files implicated by remediation

Recorded here so the remediation can be scoped without re-deriving it.
**None of these are edited by this change** — this audit is a report only.

- New: `src/pheasant/security/egress.py`, `src/pheasant/security/credentials.py`
- `src/pheasant/config/schema.py` — `SecuritySettings` (allowlists,
  `principal_source`, `allow_private_egress`), `ServerSettings.host`
  default. Per `CLAUDE.md` rule 11, no new *top-level* section is
  proposed, so no `setup_wizard.py`/`LIVE_APPLICABLE_SECTIONS`/
  `docs/configuration.md` triple is triggered — field defaults are read
  off the live dataclasses. Confirm against
  `tests/test_config_surface_freshness.py` before finalizing.
- `src/pheasant/api/app.py` — request models, `_acl_guard` callers, graph
  routes, `/memory`, middleware stack.
- `src/pheasant/mcp_server/tools.py` — `memory_list`; additive only, per
  `CLAUDE.md` rule 8 (no tool renames or removals).
- `src/pheasant/assistant/providers.py`, `src/pheasant/search/vector_store.py`,
  `src/pheasant/security/idp.py`, `src/pheasant/sync/connectors.py`,
  `src/pheasant/connectors/*.py` — egress chokepoint.
- `src/pheasant/security/acl.py` — private-default fall-through.
- `src/pheasant/security/path_policy.py` + `app.py:405-446` +
  `mcp_server/tools.py:106-118` — empty-roots fail-open, reconciled on one
  helper.
- `src/pheasant/sandbox/accel/loader.py`,
  `src/pheasant/ingestion/extractor_sandbox.py` — one shared AOT cache
  loader; `src/pheasant/persistence/paths.py` owns the location.
- `src/pheasant/sync/engine.py`, `src/pheasant/sync/remote_worker.py` —
  worker wire format; `src/pheasant/sync/grpc_worker.py` — TLS.
- `src/pheasant/registry/source_registry.py`, `src/pheasant/analytics.py`
  — `config_json` redaction / projection.
- `src/pheasant/ingestion/office.py`, `src/pheasant/ingestion/extractor.py`
  — XML and the DOCX size bound; `src/pheasant/ingestion/walk.py` —
  symlink containment.
- `deploy/kubernetes/networkpolicy.yaml`, `docker-compose.scale.yml`,
  `docker-entrypoint.sh`, `ui/Dockerfile`.
- `docs/security.md` — correct the ACL-coverage claim, document the new
  controls.

Two `CLAUDE.md` rules bind this work and should be read before starting:
**rule 8** (the MCP tool surface is public API — the `memory_list` and
`search_context` changes must be additive, no renames), and **rule 7**
(standalone mode is sacred — every new control needs a test asserting the
no-infrastructure path is unchanged).

---

## Appendix: test plan for the remediation change

Recorded here for whoever picks up remediation; nothing in this section
was executed as part of this audit.

Regression tests belong in `tests/test_security_hardening.py` beside the
existing seven, in the same adversarial style — each fails if the guard is
removed, each paired with a "still works" assertion. One per finding:

- **C1:** `PUT /search/embeddings` with `api_key_env:
  "AWS_SECRET_ACCESS_KEY"` is refused; a legitimate provider env name
  still works.
- **C2:** `base_url: "http://169.254.169.254/"` and a redirect to it are
  refused at every call site; a public https URL still works.
- **C3:** `acl_enforced: true` with `principal_source: body` fails config
  validation; a body-supplied `principal` is ignored in `header` mode.
- **C4:** two principals write `user`-scope records; each `GET /memory`
  and MCP `memory_list` sees only its own plus `org`.
- **C5:** a cache file owned by another uid, or one whose recorded digest
  does not match, is refused rather than deserialized; a legitimate cache
  still loads and still saves the JIT cost.
- **H1:** an ACL-denied artifact does not appear in `/graph/export/*`.
- **H2:** a request with a foreign `Host` header is refused.
- **H5:** a worker reply carrying a foreign `id` or `path` is rejected (or
  the fields are ignored); a well-formed reply still produces a
  byte-identical sync to the local path — reuse
  `tests/test_sync_idempotency.py`'s local-vs-remote comparison.
- **H6:** `GET /config`, `GET /sources` and `sources.parquet` contain no
  `connector.headers` value.
- **M1:** a billion-laughs `.docx` and an inflating `word/document.xml`
  are both refused, and a normal `.docx` still extracts.
- **M4:** `resolve_under(path, [])` raises rather than allowing.

And the four checks beyond the unit suite that `CLAUDE.md` makes mandatory
for that change:

- **Backend parity (rule 10):** any `state_store` change runs against a
  real local Postgres via `tests/test_backend_parity.py` — the three bugs
  recorded in `CLAUDE.md` §6 are the reason.
- **Idempotency spine (rule 4):** `tests/test_sync_idempotency.py` stays
  green; the egress and worker-wire changes both touch the sync path.
- **Standalone mode (rule 7):** a router-less, no-infrastructure pheasant
  must be unchanged — defaults keep `acl_enforced: false`, and the
  single-container quickstart works end to end.
- **Live run (rule 10):** `docker compose up`, then exercise the C1/C2
  chains against the real image with a local sink to confirm nothing
  leaves. Container builds have caught four bugs the offline suite could
  not.

---

## Open questions

1. Are `api_key_env` / `base_url` meant to be settable over HTTP at all,
   or is locking them to YAML + CLI acceptable? Locking them is the
   cleanest fix for C1 but removes a UI affordance.
2. Should `connector.headers` keep accepting literal values (H6)? Removing
   it is a breaking config change; adding `header_env` beside it and
   redacting on output is not, but leaves the footgun in place.
3. C5's cache location fix interacts with `readOnlyRootFilesystem: true`
   in the Kubernetes manifests — moving the cache under `/state` makes it
   *work* in deployments where it currently no-ops, which is a
   performance win but also newly activates the code path. Worth an
   explicit decision.
