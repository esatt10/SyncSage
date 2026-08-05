# How to enable vector self-search

By default pheasant searches over `text` (SQLite FTS5 + BM25) and `graph`. You
can additionally enable **semantic / vector search**: chunks are embedded at
sync time, and search gains a `vector` mode plus vector candidates inside
`hybrid`.

This is **off by default**. Standalone text/graph search needs no embeddings.

## 1. Enable embeddings

Set `search.embeddings.enabled: true`:

```yaml
search:
  default_mode: hybrid
  embeddings:
    enabled: true
    provider: openai-spec          # "openai-spec" | "stub" (deterministic, offline)
    model: text-embedding-3-small
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY    # env var NAME; the key never lands in config
    dimensions: 256
    batch_size: 64
```

| Provider | Behavior |
|---|---|
| `stub` | Deterministic, offline embeddings (blake2b-based). No network. Use for tests and offline runs. |
| `openai-spec` | Calls `POST {base_url}/embeddings`. Works against any OpenAI-spec endpoint, including **self-hosted** — just point `base_url` at it. |

!!! note "The only sanctioned network call (besides captioning)"
    The `openai-spec` embedder runs in the indexing path, so embedding is the
    one network call pheasant makes at sync time (alongside optional
    captioning/transcription). The `stub` provider keeps the offline path
    network-free.

## 2. Choose a vector store backend

```yaml
search:
  vector_store:
    provider: lancedb              # "lancedb" | "numpy"
    path: /state/vectors           # vectors live under <path>/<kb_id>/
```

| Backend | When |
|---|---|
| `numpy` | Always available (no extra dependency). Fine for small/medium regions. |
| `lancedb` | Faster at scale. Requires the `[vector]` extra: `pip install 'pheasant-kb[vector]'`. |

If you select `lancedb` without installing the extra, fall back to `numpy` or
install it. `PUT /search/embeddings` refuses a backend whose dependency is
missing rather than accepting the change and failing later, and
`GET /search/embeddings` reports `store_providers[].available` so the UI only
offers backends that work here.

## From the UI

Everything above is also in **Settings → Semantic search**, which adds two
things the YAML cannot express:

- **Coverage.** Enabling embeddings only affects *future* syncs — content
  indexed beforehand has no vector. The coverage bar shows the gap, and
  **Build missing vectors** (`POST /search/embeddings/reindex`) closes it by
  embedding what SQLite already holds, without re-reading a single source
  file. Vectors are keyed by content-addressed chunk id, so a second run
  embeds nothing.
- **Invalidation.** Changing `model` or `dimensions` puts existing vectors in
  a different space, where similarity is meaningless. The response flags
  `vectors_invalidated`, and **Rebuild from scratch**
  (`?drop_existing=true`) discards the old space instead of quietly mixing
  two.

Saving with **persist** writes only the `search.embeddings` and
`search.vector_store` keys back into your config file; the rest of the file,
`sources` included, is preserved.

## 3. Self-hosted OpenAI-spec endpoint

To embed with a locally hosted model server (anything that speaks the OpenAI
`/embeddings` API), set `base_url` and `api_key_env` accordingly:

```yaml
search:
  embeddings:
    enabled: true
    provider: openai-spec
    model: bge-small-en-v1.5
    base_url: http://localhost:11434/v1   # your endpoint
    api_key_env: LOCAL_EMBED_KEY          # may be a dummy if the server ignores it
    dimensions: 384
```

## 4. Search with vector / hybrid mode

After a sync that embedded your chunks, search with `mode: "vector"` or
`mode: "hybrid"`:

```bash
curl -X POST http://localhost:8765/search \
  -H "content-type: application/json" \
  -d '{"query": "how do we charge customers", "mode": "vector", "max_results": 5}'
```

- `text` — FTS5 + BM25 only.
- `graph` — node labels, types, attributes, and relationships.
- `vector` — embedding similarity only (requires embeddings enabled).
- `hybrid` — merges and re-ranks all available signals, de-duplicating by node.
  The default.

## The fleet-pinned model

When pheasant is a Synapse region, its embedding space must match the rest of
the fleet so the router can compare contracts. The embedder uses the **same
wire format** the subjective-retrieval router's embedding provider uses, so a
fleet can **pin one model** (one `model` + `dimensions`) across both repos and
every region. Mismatched embedding spaces are rejected at the router. If you
plan to join a fleet, agree on the embedding model first and set the same
`model`/`dimensions` everywhere. See
[Attach to a Synapse fleet](attach-to-synapse.md).
