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
