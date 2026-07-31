# Ask your knowledge base (the web UI)

The SyncSage UI is a three-pane workspace: **Sources** on the left,
**Chat** in the middle, **Knowledge** (graph / facts / node) on the right.
You ask a question in prose; SyncSage answers from your own indexed content,
cites the passages it used, and lights up the corresponding nodes on the
knowledge graph.

Start it with `syncsage host <target>` (container, UI included) or run the
dev server against a local `syncsage start`.

---

## The chat layer

Every answer goes through the same four steps, and all four are inspectable:

1. **Retrieve** — the ordinary hybrid self-search (`text` + `graph`, plus
   `vector` when embeddings are on) runs against your index.
2. **Cite** — hits become numbered passages. The citation strip under an
   answer shows them all; the ones the answer actually used are at full
   strength, retrieved-but-unused ones are dimmed.
3. **Surface facts** — one hop out from each cited passage into the
   concept/entity/symbol layer of the graph, rendered as
   subject–predicate–object triples in the **Facts** tab. Facts are
   collected round-robin across the cited sources, so a well-connected
   document cannot crowd out the rest.
4. **Answer** — a chat model writes the answer using *only* those passages,
   citing `[1]`, `[2]`, … Clicking a citation chip in the prose focuses that
   node on the graph and opens it in the **Node** tab.

!!! note "No LLM ever runs during indexing"
    The assistant is a query-time surface only. Indexing stays fully
    deterministic (rule-based parsing, content hashing, stable IDs), so
    re-syncing unchanged content produces byte-identical state whether or
    not a model is connected.

### Without a model

Step 4 is the only step that needs a provider. With none reachable, the
answer is **extractive**: the top passages verbatim, with their citations
and graph facts intact. That is the default, it works air-gapped, and it is
what the offline test suite exercises. Connecting a model upgrades the prose,
not the grounding.

If a *configured* model fails (rate limit, network, bad key), the answer
degrades to the same extractive form and says so explicitly — it will not
claim that no model is connected.

---

## Connecting a model

Three providers are supported: **Anthropic**, **OpenAI**, and **Google
Gemini**. There are two ways to supply a key.

### Server environment (operator)

Set the provider's key on the container and leave `assistant.provider` at
`auto`; SyncSage picks the first provider whose variable is populated, in the
order Anthropic → OpenAI → Gemini.

```yaml
assistant:
  provider: auto            # or: anthropic | openai | gemini | none
  # model: claude-sonnet-5  # provider default when unset
```

```bash
docker run -e ANTHROPIC_API_KEY=sk-ant-… ghcr.io/esatt10/syncsage
```

The key is read from the environment at request time. It is never copied into
config, state, or logs.

### Session key (user, in the browser)

Click **Connect model**, pick a provider, and paste a key. What happens to it:

- it is sent once and held in the **server process's memory only**, behind an
  opaque session token — never written to `syncsage.yaml`, never to `/state`,
  never logged;
- the browser stores only the token, and only in `sessionStorage`, so it dies
  with the tab;
- the entry expires on a TTL (`assistant.session_key_ttl_minutes`, default
  720), is dropped when you click **Disconnect**, and is gone entirely on a
  container restart — the process holds no persistence hook for it at all;
- API responses return provider/model/expiry metadata and never the key.

Set `assistant.allow_session_keys: false` to disable this route and require
the environment variable instead.

---

## Reading the graph

The graph panel is deliberately quiet by default. A real index produces far
more `concept` nodes than anything else — often 80% of the graph — so
`concept` and `chunk` are **hidden by default**. The legend at the bottom is
also the filter: click any type to show or hide it.

- **Colour** encodes node type; **shape** is a small vocabulary (rounded
  rectangles are containers, rectangles are documents, circles are ideas,
  diamonds are code symbols) so the canvas reads as structure rather than
  decoration.
- **Size** grows with connectivity, so hubs stand out without a second legend.
- Asking a question **outlines the cited nodes** and fades the rest to
  context strength.
- Selecting a source in the left rail scopes both retrieval and the graph to
  it.

---

## Connecting a coding agent (MCP)

**Connect agent** in the top bar shows this region's MCP transports, a
ready-to-paste `.mcp.json`, and the full tool list an attached agent gets. The
same knowledge base backs both surfaces — the chat panel and an MCP client are
two front doors onto one index.

```bash
syncsage client-config claude-code -c syncsage.yaml -o .mcp.json
```

---

## Related settings

| Setting | Default | What it does |
|---|---|---|
| `assistant.enabled` | `true` | Turn the chat surface off entirely (`/assistant/chat` returns 403). |
| `assistant.provider` | `auto` | `auto`, `anthropic`, `openai`, `gemini`, or `none`. |
| `assistant.model` | provider default | Override the model id. |
| `assistant.base_url` | provider default | Point at a gateway or self-hosted OpenAI-spec endpoint. |
| `assistant.api_key_env` | provider default | Read the key from a differently-named variable. |
| `assistant.allow_session_keys` | `true` | Allow browser-supplied keys. |
| `assistant.session_key_ttl_minutes` | `720` | How long a session key survives. |
| `assistant.max_context_chunks` | `8` | Passages retrieved and offered to the model. |
| `assistant.max_facts` | `12` | Graph facts surfaced per answer. |

Full reference: [Configuration](../configuration.md).
