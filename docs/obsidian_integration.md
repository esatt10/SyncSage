# Obsidian Integration

Obsidian is an optional human-readable projection layer. SyncSage does not require the Obsidian app or plugins at runtime.

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
