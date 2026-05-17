# Obsidian Integration

Obsidian is an optional human-readable projection layer. SyncSage does not require the Obsidian app or plugins at runtime.

## Connect Obsidian

1. Pick a host folder for the managed vault, for example `./vault` in the repository or another folder outside the repo.
2. Set `deployment.compose.vault_path` in `syncsage.yaml` to that host folder.
3. Keep `syncsage.vault_path: /vault` and `obsidian.note_root: SyncSage` in `syncsage.yaml`.
4. Start SyncSage:

   ```bash
   syncsage compose-env syncsage.yaml --output .syncsage/compose.env
   docker compose --env-file .syncsage/compose.env up -d
   ```

5. In Obsidian desktop, open the host folder from `deployment.compose.vault_path` as a vault.
6. Export notes after indexing:

   ```bash
   curl -X POST http://localhost:8765/obsidian/export
   ```

   Or call the MCP tool `export_obsidian_notes` from VS Code.

SyncSage owns the generated `SyncSage/` folder inside the vault. User-authored notes can live outside that folder. If you also want SyncSage to index an existing Obsidian vault, mount that vault under `/workspace` and add it as an `obsidian_vault` source; do not point that source at the generated `/vault/SyncSage` output unless you intentionally want to index generated notes.

## Default vault layout

```text
/vault/SyncSage/
  Index.md
  Sources/
  Repositories/
  Documents/
  Graphs/
  Queries/
```

## Note types

| Note type | Default | Purpose |
|---|---:|---|
| Knowledge base index | Yes | Entry point into generated knowledge. |
| Source note | Yes | Source summary and health. |
| Repository map | Yes | Directory/module overview. |
| File note | Yes | Concise summary and relationships for key files. |
| Chunk note | No | Optional; can be noisy. |
| Topic/query note | Optional | Saved searches and topical clusters. |
| Canvas graph | Optional | Visual graph representation. |

## Frontmatter

Generated notes should include `syncsage: true`, `node_id`, `source_id`, `source_type`, `relative_path`, `content_hash`, `last_indexed_at`, branch/commit when available, and SyncSage tags.

## Git friendliness

Keep generated notes stable and concise. Store bulky operational graph state in `/state`, not `/vault`, and avoid chunk notes by default.
