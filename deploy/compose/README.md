# Pheasant configuration profiles

These files are generated from the live configuration schema with `pheasant
setup`; the JSON answer files are the editable source of truth. No secret is
stored in YAML.

| Profile | State and coordination | Search/assistant | Intended size |
|---|---|---|---|
| `local-small.yaml` | Local SQLite, no broker or workers | BM25/text search and extractive answers; MCP and durable memory remain enabled | Laptop, offline, small corpus |
| `local-advanced.yaml` | Single-node SQLite | Hybrid search, LanceDB, both WASM accelerators, `text-embedding-3-small`, and the `gpt-5.6-luna` agentic workflow | One capable workstation/container |
| `fleet.yaml` | PostgreSQL, NATS JetStream, shared durable volumes, and stateless gRPC preparation workers | Same advanced retrieval stack with aggressive concurrency limits | Multi-container, horizontally scaled ingestion |

`worker.yaml` is the deliberately minimal trust-boundary config for the
fleet's stateless gRPC workers. It has no source list, database DSN, OpenAI key,
MCP server, or UI.

## Regenerate after changing an answer file

Run these from the repository root:

```bash
python -m pheasant setup --answers deploy/compose/answers/local-small.json --accept-defaults --plain --target local --output deploy/compose/local-small.yaml --force
python -m pheasant setup --answers deploy/compose/answers/local-advanced.json --accept-defaults --plain --target docker --output deploy/compose/local-advanced.yaml --force
python -m pheasant setup --answers deploy/compose/answers/scalable.json --accept-defaults --plain --target compose --output deploy/compose/fleet.yaml --force
python -m pheasant setup --answers deploy/compose/answers/worker.json --accept-defaults --plain --target compose --output deploy/compose/worker.yaml --force
```

## Run the profiles

Copy the environment template once for any Compose profile:

```bash
cp deploy/compose/.env.example .env
```

Blank-canvas Docker, one container and no required key:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yml up -d --build
```

Small, entirely local without Docker:

```bash
pip install -e ".[mcp]"
pheasant start -c deploy/compose/local-small.yaml
```

Advanced single-node Docker with SQLite, LanceDB and OpenAI:

```bash
docker compose --env-file .env \
  -f deploy/compose/docker-compose.advanced.yml up -d --build
```

Scalable fleet:

```bash
# Set OPENAI_API_KEY and a random PHEASANT_INDEX_WORKER_TOKEN in .env.
docker compose --env-file .env -f deploy/compose/docker-compose.scale.yml up -d --build \
  --scale indexer=4 --scale worker=8
```

Fresh UI-managed reset, when existing Pheasant volumes should be cleared:

```bash
docker compose -f deploy/compose/docker-compose.fresh.yml \
  up -d --build --force-recreate
```

The fresh manifest is intentionally destructive only to its named Pheasant
volumes. See [Run the UI](../../docs/how-to/run-the-ui.md#fresh-ui-native-reset)
before using it.

The UI is at <http://127.0.0.1:8765> and streamable HTTP MCP is at
`http://127.0.0.1:8765/mcp` for both Docker profiles.

## Throughput and durability notes

- The fleet profile batches 128 chunks per embedding request and permits 16
  embedding requests in flight. Those are intentionally aggressive ceilings;
  the OpenAI account's RPM/TPM tier is the actual limit. If logs show repeated
  429 responses, reduce `max_parallel_embeddings` before reducing batch size.
- gRPC workers accelerate file decoding, parsing, extraction and chunking.
  They do not accelerate the external embedding API. Scale indexers when there
  are multiple sources and scale workers for CPU-heavy preparation.
- URL-managed repositories use the persistent `pheasant-workspace` volume by
  default. The indexer mounts it read/write so its persisted clone recipe can
  fetch and fast-forward before each sync; the API mounts it read-only. Set
  `PHEASANT_FLEET_WORKSPACE_PATH` to use a specific host directory instead.
- The memory volume is mounted read/write by both API and indexers. Calls to
  MCP `memory_write` (or `POST /memory`) index immediately, while watcher and
  scheduler settings provide recovery if a write or process is interrupted.
  Ordinary chat questions and answers are not silently recorded as memory;
  an agent must explicitly choose what to remember.
- PostgreSQL and NATS make the source queue and manifests durable. LanceDB
  remains under the shared `/state/vectors` volume; the API reads it and the
  indexer tier writes it.
