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
