# Obsidian Integration

Obsidian is an optional human-readable projection layer. pheasant does not require the Obsidian app or plugins at runtime.

## Connect Obsidian

1. Pick a host folder for the managed vault, for example `./vault` in the repository or another folder outside the repo.
2. Set `deployment.compose.vault_path` in `pheasant.yaml` to that host folder.
3. Keep `pheasant.vault_path: /vault` and `obsidian.note_root: pheasant` in `pheasant.yaml`.
4. Start pheasant:

   ```bash
   pheasant compose-env pheasant.yaml --output .pheasant/compose.env
   docker compose --env-file .pheasant/compose.env up -d
   ```

5. In Obsidian desktop, open the host folder from `deployment.compose.vault_path` as a vault.
6. Export notes after indexing:

   ```bash
   curl -X POST http://localhost:8765/obsidian/export
   ```

   Or call the MCP tool `export_obsidian_notes` from VS Code.

   Preview planned changes without writing:

   ```bash
   curl -X POST http://localhost:8765/obsidian/export \
     -H "content-type: application/json" \
     -d '{"preview": true, "template_profile": "engineering"}'
   ```

pheasant owns the generated `pheasant/` folder inside the vault. User-authored notes can live outside that folder. If you also want pheasant to index an existing Obsidian vault, mount that vault under `/workspace` and add it as an `obsidian_vault` source; do not point that source at the generated `/vault/pheasant` output unless you intentionally want to index generated notes.

## Default vault layout

```text
/vault/pheasant/
  Index.md
  Sources/
  Files/
  Chunks/
```

!!! note "No `Concepts/` folder since 2026-08-03"
    Concept extraction was retired (see `docs/graph_model.md`), so the vault no
    longer projects a Concepts folder or the backlinks into it. On a real
    corpus that folder held 141,529 notes for terms like "limit" and "request
    info" — navigation that led nowhere. Source and file notes still link the
    documents they index, which is the projection people actually walk.

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

## Template profiles

Obsidian export supports `engineering`, `research`, and `project-ops` templates through `obsidian.template_profile` or the MCP/API export request. Profiles keep the same graph-derived links but adjust section headings for the intended workflow.

## Preview and links

Preview mode returns `planned_count`, `changed_count`, and `changed_files` without creating notes. Written vaults include graph-driven navigation from source notes to concept notes, from concept notes to file notes, and from file notes to chunk notes when `create_chunk_notes` is enabled.

## Frontmatter

Generated notes should include `pheasant: true`, `node_id`, `source_id`, `source_type`, `relative_path`, `content_hash`, `last_indexed_at`, branch/commit when available, and pheasant tags.

## Git friendliness

Keep generated notes stable and concise. Store bulky operational graph state in `/state`, not `/vault`, and avoid chunk notes by default.
