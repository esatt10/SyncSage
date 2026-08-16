from __future__ import annotations

from dataclasses import asdict, dataclass, field
from dataclasses import fields as dataclass_fields
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

#: Patterns that keep credentials out of the index. Unlike the rest of
#: ``DEFAULT_EXCLUDES``, these are **not** merely a default a caller can
#: replace: ``security.default_exclude_secrets`` (on by default) unions them
#: into every filesystem source's effective exclude list. That distinction
#: matters because pheasant supports indexing any readable path — pointing a
#: source at ``$HOME`` with ``include: ["**/*.json", "**/*.yaml"]`` otherwise
#: sweeps up ``~/.docker/config.json``, ``~/.config/gh/hosts.yml`` and
#: friends, and a caller that supplies its own ``exclude`` list used to drop
#: every one of these patterns silently.
SECRET_EXCLUDES = [
    # Environment and dotenv files
    "**/.env",
    "**/.env.*",
    "**/*.envrc",
    # Private keys and certificates
    "**/*id_rsa*",
    "**/*id_dsa*",
    "**/*id_ecdsa*",
    "**/*id_ed25519*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.jks",
    "**/*.keystore",
    "**/*.asc",
    "**/*.gpg",
    # Credential stores people keep in a home directory
    "**/.ssh/**",
    "**/.gnupg/**",
    "**/.aws/**",
    "**/.azure/**",
    "**/.kube/**",
    "**/.docker/config.json",
    "**/.config/gh/**",
    "**/.config/gcloud/**",
    "**/.netrc",
    "**/.npmrc",
    "**/.pypirc",
    "**/.git-credentials",
    "**/credentials",
    "**/credentials.json",
    "**/secrets.yaml",
    "**/secrets.yml",
    "**/*.kdbx",
    # Local keychains / browser profiles
    "**/Library/Keychains/**",
    "**/.mozilla/**",
    "**/.password-store/**",
]

#: Directories that are large, generated, and never worth indexing. Kept
#: separate from the secret list because these are about *cost*, not
#: disclosure, and an operator may legitimately want to drop them.
NOISE_EXCLUDES = [
    "**/.git/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/.venv/**",
    "**/venv/**",
    "**/dist/**",
    "**/build/**",
    "**/target/**",
    "**/.next/**",
    "**/.cache/**",
    "**/.tox/**",
    "**/.gradle/**",
    "**/.terraform/**",
    "**/.mypy_cache/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
]

DEFAULT_EXCLUDES = [*NOISE_EXCLUDES, *SECRET_EXCLUDES]


class SourceType(StrEnum):
    repository = "repository"
    markdown_folder = "markdown_folder"
    obsidian_vault = "obsidian_vault"
    document_folder = "document_folder"
    web_collection = "web_collection"
    single_file = "single_file"
    s3 = "s3"
    api = "api"
    memory = "memory"


class PluginSourceType(str):
    """A connector-plugin source type outside the built-in enum (Step 31.1).

    Behaves as its plain string everywhere, plus a ``.value`` property so
    every existing ``source.type.value`` call site works unchanged. Whether
    a connector actually exists for the name is checked at dispatch time
    (``connector_for_source``), not at config load — config stays loadable
    on a machine that hasn't installed the plugin yet.
    """

    @property
    def value(self) -> str:
        return str(self)


# Source types whose ``path`` is a real local directory/file (as opposed to
# the URL/connector-backed web/api/s3 types). A relative ``path`` on one of
# these is anchored to ``pheasant.workspace_root`` at config-load time.
FILESYSTEM_SOURCE_TYPES = frozenset(
    {
        SourceType.repository,
        SourceType.markdown_folder,
        SourceType.obsidian_vault,
        SourceType.document_folder,
        SourceType.single_file,
        SourceType.memory,
    }
)


_TRUTHY = {"true", "yes", "on", "1"}
_FALSY = {"false", "no", "off", "0"}


def _coerce_scalar_fields(dc: type, raw: dict[str, Any]) -> None:
    """Coerce int/float/bool fields in-place; raise ValueError on garbage.

    The dataclass constructors accept whatever they are given, so without
    this a config (or a UI payload) carrying e.g. ``max_chars: "oops"``
    validates "successfully" and only blows up mid-sync. Coercion keeps the
    friendly behaviours (``"8765"`` → 8765, ``"true"`` → True) while turning
    unusable values into an immediate, catchable error.
    """
    for f in dataclass_fields(dc):
        if f.name not in raw or raw[f.name] is None:
            continue
        annotation = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", "")
        base = annotation.replace(" ", "").removesuffix("|None")
        value = raw[f.name]
        try:
            if base == "int" and not isinstance(value, bool) and not isinstance(value, int):
                raw[f.name] = int(value)
            elif base == "float" and not isinstance(value, (int, float)):
                raw[f.name] = float(value)
            elif base == "bool" and not isinstance(value, bool):
                text = str(value).strip().lower()
                if text in _TRUTHY:
                    raw[f.name] = True
                elif text in _FALSY:
                    raw[f.name] = False
                else:
                    raise ValueError(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{dc.__name__}.{f.name} expects {base}, got {value!r}") from exc


@dataclass
class ModelMixin:
    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        def conv(v: Any) -> Any:
            if isinstance(v, Path):
                return str(v) if mode == "json" else v
            if isinstance(v, Enum):
                return v.value
            if isinstance(v, ModelMixin):
                return v.model_dump(mode=mode)
            if isinstance(v, list):
                return [conv(i) for i in v]
            if isinstance(v, dict):
                return {k: conv(val) for k, val in v.items()}
            return v

        return {k: conv(v) for k, v in asdict(self).items()}


@dataclass
class PheasantSettings(ModelMixin):
    name: str = "local-pheasant"
    description: str = "Lightweight MCP knowledge graph and retrieval server"
    environment: str = "local"
    log_level: str = "INFO"
    state_path: Path = Path("/state")
    workspace_root: Path = Path("/workspace")
    exports_path: Path = Path("/exports")


@dataclass
class McpSettings(ModelMixin):
    enabled: bool = True
    transports: dict[str, bool] = field(
        default_factory=lambda: {"stdio": True, "streamable_http": True, "sse": False}
    )


@dataclass
class ApiSettings(ModelMixin):
    enabled: bool = True
    openapi: bool = True
    # Browser origins allowed to call this API. The API is unauthenticated by
    # design (a local-first tool behind the operator's own perimeter), which
    # is exactly why the origin list must not be "*": with a wildcard, any
    # page the user happens to visit can script the whole surface — read the
    # index, rewrite the config, point the embedder at an attacker's host and
    # ship a server-side API key with it. The defaults cover the UI sidecar
    # and local dev; add explicit origins for anything else.
    cors_origins: list[str] = field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8765",
            "http://127.0.0.1:8765",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    )
    # Escape hatch for deployments that genuinely front this with their own
    # authenticating ingress and need `*` back. Opt-in, never the default.
    cors_allow_all_origins: bool = False


@dataclass
class UiSettings(ModelMixin):
    enabled: bool = True
    graph_visualization: bool = True


@dataclass
class ServerSettings(ModelMixin):
    host: str = "0.0.0.0"
    port: int = 8765
    mcp: McpSettings = field(default_factory=McpSettings)
    api: ApiSettings = field(default_factory=ApiSettings)
    ui: UiSettings = field(default_factory=UiSettings)


@dataclass
class StorageSettings(ModelMixin):
    """On-disk state layout + graph snapshot/retention policy (Synapse 21.6A).

    - ``graph_snapshots`` toggles zstd-compressed timestamped graph snapshots
      written after a successful sync. Defaults **on** — a snapshot is additive
      history beside the (unchanged) ``graph.latest.json`` and is bounded by the
      retention cap below, so standalone behavior stays sane.
    - ``graph_snapshot_interval_seconds`` is the minimum spacing between
      snapshots; syncs closer together than this reuse the most recent snapshot.
    - ``compression`` selects the snapshot codec (``zstd`` only, for now).
    - ``max_state_size_gb`` caps total snapshot bytes per KB; oldest snapshots
      are evicted first when exceeded. ``graph.latest.json``, the SQLite db, and
      the contract are never evicted.
    - ``graph_checkpoint_seconds`` is the minimum spacing between mid-sync
      writes of ``graph.latest.json``. The graph used to reach disk only when a
      sync *completed*, so stopping the container during a first index threw
      that work away. 0 disables checkpointing (end-of-sync save only).
    """

    graph_format: str = "node_link_json"
    graph_snapshots: bool = True
    graph_snapshot_interval_seconds: int = 900
    graph_checkpoint_seconds: int = 60
    compression: str = "zstd"
    sqlite_path: Path | None = None
    graph_path: Path | None = None
    manifest_path: Path | None = None
    max_state_size_gb: float = 10

    # -- Phase 35.2: where state actually lives -------------------------------
    #: ``sqlite`` (default) or ``postgres``. SQLite is a file and permits one
    #: writer process per knowledge base, which is pheasant's hard scaling
    #: ceiling; Postgres is what lets 35.4 give each *source* its own lease so
    #: several indexers can commit at once. Leaving this alone keeps a region
    #: byte-identical to pre-35.2 and needing no infrastructure at all.
    backend: str = "sqlite"
    #: Name of the environment variable holding the libpq DSN. A DSN carries a
    #: password, so — like every other credential in pheasant — only the
    #: variable *name* is ever written to YAML. There is deliberately no field
    #: to paste the DSN itself into.
    dsn_env: str = "PHEASANT_DATABASE_URL"
    #: Server-side connections this process may hold. Unlike a SQLite file
    #: handle a Postgres connection is a server process, so this is a real
    #: resource on the database, not just on the client.
    pool_size: int = 10


@dataclass
class EmbeddingsSettings(ModelMixin):
    """Optional embed-on-sync provider (Synapse 21.4). Off by default.

    ``provider`` is ``openai-spec`` (POST {base_url}/embeddings, the same
    wire format the pheasant-flock router uses) or ``stub``
    (deterministic, offline). The API key is read from the environment
    variable named by ``api_key_env`` and never stored in config/state.

    ``dimensions`` is unset (``None``) by default: the ``dimensions``
    parameter is simply omitted from the embedding request, so the
    provider returns the model's own native size (1536 for
    ``text-embedding-3-small``, 3072 for ``text-embedding-3-large``, ...) —
    changing ``model`` therefore changes the vector width without a second
    edit. Set an explicit number only to shrink vectors for storage
    (OpenAI's ``-3`` models support Matryoshka truncation) or to pin an
    exact size across a Synapse fleet.
    """

    enabled: bool = False
    provider: str = "openai-spec"
    model: str = "text-embedding-3-small"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    dimensions: int | None = None
    batch_size: int = 64
    #: Bounded retry on *transient* embedding failures (TLS blips, 429s, 5xx).
    #: Indexing a large corpus is hundreds of HTTPS calls, and without this a
    #: single flaky one aborts the whole sync — a real 12,667-file run died
    #: ~45 minutes in on an `SSLV3_ALERT_BAD_RECORD_MAC`. A wrong key or a
    #: malformed request is never retried.
    max_retries: int = 4
    retry_backoff_seconds: float = 1.0


@dataclass
class VectorStoreSettings(ModelMixin):
    """Vector index backend; vectors live under ``<path>/<kb_id>/``.

    ``provider`` is ``lancedb`` (default; optional ``[vector]`` extra) or
    ``numpy`` (always available flat file). ``path`` defaults to
    ``<state>/vectors``.
    """

    provider: str = "lancedb"
    path: Path | None = None


@dataclass
class SearchSettings(ModelMixin):
    default_mode: str = "hybrid"
    max_results_default: int = 10
    embeddings: EmbeddingsSettings = field(default_factory=EmbeddingsSettings)
    vector_store: VectorStoreSettings = field(default_factory=VectorStoreSettings)
    # Synapse Step 34.5b: run search.graph_search._scan_edges through the
    # vendored WASM accelerator instead of pure Python. Default off — needs
    # the [wasm] extra; falls back to pure Python on any failure or if the
    # extra isn't installed. 34.4 benchmark: a consistent 2-8x win, growing
    # with edge count, at every scale tested (500-64,000 edges).
    wasm_relationship_search: bool = False


@dataclass
class CaptionerSettings(ModelMixin):
    """Image captioner for multi-modal ingestion (Synapse 25.4 session A).

    When an image source is configured, image files are captioned into text
    that flows through the normal chunk -> embed -> graph path (architecture
    §8). ``provider`` is ``stub`` (default; deterministic + offline, the only
    path used by tests) or ``openai-spec`` (POST {base_url}/chat/completions
    with an ``image_url`` content part — a vision-capable chat model). The API
    key is read from the environment variable named by ``api_key_env`` and
    never stored. A sidecar ``<image>.caption.txt`` always wins, providing an
    authored caption with no model or network.
    """

    provider: str = "stub"
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    prompt: str = "Describe this image in one concise sentence for search indexing."


@dataclass
class TranscriberSettings(ModelMixin):
    """Audio transcriber for multi-modal ingestion (Synapse 25.4 session B).

    When an audio source is configured, audio files are transcribed into text
    that flows through the normal chunk -> embed -> graph path (architecture
    §8). ``provider`` is ``stub`` (default; deterministic + offline, the only
    path used by tests — no audio library required) or ``openai-spec`` (POST
    {base_url}/audio/transcriptions, a multipart upload to a speech-to-text
    model). The API key is read from the environment variable named by
    ``api_key_env`` and never stored. A sidecar ``<audio>.transcript.txt``
    always wins, providing an authored transcript with no model or network.
    """

    provider: str = "stub"
    model: str = "whisper-1"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"


@dataclass
class ExtractorSettings(ModelMixin):
    """Document text extractor for PDF/DOCX (and optionally HTML) ingestion.

    Before this existed, ``.pdf`` and ``.docx`` were *accepted* by the
    ingestion pipeline and then produced no text at all: the artifact was
    discovered, hashed and given a graph node, but ``read_text`` returned
    ``""``, so it contributed zero chunks and was unfindable by content.

    Unlike the captioner/transcriber, no provider here makes a network call or
    uses a model — the text is already in the file — so every option is fully
    offline and deterministic.

    ``provider``:

    - ``auto`` (default) — ``pymupdf``/``python-docx`` when importable (both
      already core deps), falling back to the pure-stdlib builtin. Never
      raises into a sync.
    - ``native`` — prefer the third-party libraries (best fidelity on PDFs
      with CID/Type0 fonts or complex layout).
    - ``builtin`` — standard library only: ``zlib`` + content-stream scanning
      for PDF, ``zipfile`` + ``xml.etree`` for DOCX. No third-party imports.
    - ``sandboxed`` — the builtin PDF tokenizer inside the Phase-34 WASM
      sandbox (fuel + memory cap, zero host capabilities). For regions
      ingesting PDFs from untrusted connector sources; needs the ``[wasm]``
      extra and raises with a hint if it is missing rather than silently
      running unsandboxed.

    ``html_text`` strips markup from ``.html``/``.htm``/``.xhtml`` so their
    prose indexes instead of their tags. It defaults to **false** because
    those extensions have always been indexed as raw markup: turning it on
    changes the indexed text (and therefore chunk boundaries) of an existing
    knowledge base, so it is an explicit operator choice rather than a
    surprise on upgrade.

    A sidecar ``<file>.extract.txt`` always wins, providing authored text with
    no extractor at all (mirrors the caption/transcript sidecars).
    """

    provider: str = "auto"
    html_text: bool = False


@dataclass
class IngestionSettings(ModelMixin):
    """Ingestion-path enrichment knobs. Standalone-safe defaults.

    ``captioner`` only takes effect for sources that include image files,
    ``transcriber`` only for sources that include audio files, and
    ``extractor`` only for sources that include PDF/DOCX files (or when
    ``extractor.html_text`` is on); a text-only region never builds any of
    them (and the defaults need no extra dependency anyway).
    """

    captioner: CaptionerSettings = field(default_factory=CaptionerSettings)
    transcriber: TranscriberSettings = field(default_factory=TranscriberSettings)
    extractor: ExtractorSettings = field(default_factory=ExtractorSettings)


@dataclass
class WatcherSettings(ModelMixin):
    enabled: bool = True
    max_watch_paths: int = 100
    debounce_ms: int = 1500
    batch_window_ms: int = 5000


@dataclass
class GitSettings(ModelMixin):
    enabled: bool = True
    detect_commit_changes: bool = True
    detect_branch_switch: bool = True
    reindex_on_commit: bool = True


@dataclass
class SchedulerSettings(ModelMixin):
    enabled: bool = True
    interval_seconds: int = 900


@dataclass
class SyncLimitsSettings(ModelMixin):
    """Guardrails on how much one filesystem source may pull in.

    pheasant lets a source point at any readable path, which makes
    "accidentally indexed my home directory" a realistic mistake rather than
    a hypothetical one. These limits turn that mistake into a refusal with an
    actionable message instead of a process that consumes memory until it
    dies. They are checked *during* traversal, before any file is read.

    Any field set to ``None`` disables that particular limit. A sync that
    trips a limit indexes nothing — a partial index would be
    non-deterministic, and silently indexing the first N files of a home
    directory is a worse outcome than a clear stop. Pass ``full_scan`` on the
    sync surfaces (or raise the limits here) to index it anyway.
    """

    #: Matching files, after include/exclude. A large monorepo is ~100k.
    max_files: int | None = 50_000
    #: Skip any single file bigger than this — a 2 GB model checkpoint or
    #: database dump has no business in a text index and would be read whole.
    max_file_size_mb: int | None = 25
    #: Total matched content. Chunking and embedding both scale off this.
    max_total_mb: int | None = 4096
    #: Symlinks are not followed by default: a home directory routinely
    #: contains links that escape the source root or form loops.
    follow_symlinks: bool = False


@dataclass
class SyncConcurrencySettings(ModelMixin):
    """Bounded indexing concurrency, shared by every configuration surface.

    File workers only prepare immutable parse results. SQLite, graph,
    manifest, and vector-store mutations remain coordinated by the engine so
    increasing a cap cannot change stable IDs or persisted graph bytes.
    """

    max_parallel_sources: int = 4
    max_parallel_files: int = 8
    max_parallel_embeddings: int = 4
    file_executor: str = "thread"
    remote_worker_urls: list[str] = field(default_factory=list)
    remote_worker_enabled: bool = False
    remote_worker_token_env: str = "PHEASANT_INDEX_WORKER_TOKEN"
    remote_worker_timeout_seconds: int = 120
    lock_timeout_seconds: int = 120


@dataclass
class SyncSettings(ModelMixin):
    watcher: WatcherSettings = field(default_factory=WatcherSettings)
    git: GitSettings = field(default_factory=GitSettings)
    scheduler: SchedulerSettings = field(default_factory=SchedulerSettings)
    limits: SyncLimitsSettings = field(default_factory=SyncLimitsSettings)
    concurrency: SyncConcurrencySettings = field(default_factory=SyncConcurrencySettings)


@dataclass
class GraphSettings(ModelMixin):
    """How much graph the knowledge graph should actually keep.

    ``concept_min_documents`` is the number of distinct documents that must
    share a term before it becomes a concept *node*. A concept exists to link
    the documents that share it, so one that links a single document is pure
    weight: measured on microsoft/agent-framework, 74.5% of concept nodes were
    mentioned by exactly one document, and concepts made up 96% of a
    577k-node graph — memory, traversal budget and enrichment time spent
    connecting nothing.

    Nothing becomes unfindable: the term stays on the artifact's
    ``concept_terms``, in ``artifact_terms``, and in the artifact's searchable
    text. Set to 1 to keep every term as a node (the pre-2026-08 behavior).
    """

    concept_min_documents: int = 2
    #: Wire agent-memory records into the graph (Step 33.7): `about` edges to
    #: what a record refers to, plus `supersedes` between corrections. A no-op
    #: without a memory source, so turning it off only matters to a region that
    #: has one and would rather keep memory out of its graph.
    memory_entity_bridging: bool = True
    # Synapse Step 34.5a: run graph.enrichment.resolve_cross_source_edges
    # through the vendored WASM accelerator instead of pure Python. Default
    # off — needs the [wasm] extra; falls back to pure Python on any
    # failure or if the extra isn't installed. 34.4 benchmark: a
    # conditional win — loses to Python below ~1,300-2,500 edges (today's
    # demo corpus sits at ~2,900, right at the breakeven point), wins
    # modestly above that. Opt in for large/growing multi-source graphs.
    wasm_cross_source_resolution: bool = False

    # -- Phase 35.3: the in-RAM graph has a measured ceiling ------------------
    #: Warn once per sync when the graph passes this many nodes. **Not a
    #: refusal** — unlike `sync.limits`, which stops a source before any work
    #: happens, by the time this trips the graph already exists and refusing
    #: would throw away a completed index.
    #:
    #: The default is derived from measurement, not taste. `graph/capacity.py`
    #: measured a real-shaped graph at four scales; per node the cost is flat
    #: at ~2.4 KB of process RSS, and the binding constraint is not RAM but
    #: `storage.graph_checkpoint_seconds`: at 1.58M nodes one checkpoint takes
    #: ~20 s to serialize against a 60 s interval, so a third of the sync is
    #: spent writing the graph out. 750k nodes (~120k files) keeps that under
    #: ~10%, which is the point at which sharding into several regions is the
    #: better answer. Set to None to disable the warning.
    max_nodes: int | None = 750_000

@dataclass
class SynapseSettings(ModelMixin):
    """Synapse federation (Synapse 21.5). Standalone-safe: all router-facing
    behavior no-ops when ``router_url`` is unset and ``publish`` is false.

    - ``publish`` gates contract publication + the NDJSON event stream from the
      sync engine. Default off → a router-less pheasant behaves exactly as
      before. The local ``GET /contract`` serving path still works once a
      contract has been published.
    - ``router_url`` is the Synapse router base URL; when set, the engine POSTs
      the ``sync.completed`` event (with the inline contract) to
      ``<router_url>/v1/synapse/events`` (fail-soft).
    - ``fleet_id`` / ``endpoint`` are stamped into the contract so the router
      can pin the fleet embedding space and pull/route to this region.
    """

    publish: bool = False
    router_url: str | None = None
    fleet_id: str | None = None
    endpoint: str | None = None
    webhook_timeout_seconds: float = 5.0
    # Synapse 24.4: optional Ed25519 contract signing. ``signing_key_ref`` is a
    # secret *reference* (``env://NAME`` or a bare env-var name) resolving to a
    # base64 32-byte Ed25519 private seed — the plaintext key never lands in the
    # config or on disk. When unset (default) the contract's
    # ``integrity.signature`` stays ``null`` and a standalone pheasant is
    # unchanged. The router verifies the signature against an out-of-band public
    # key when its fleet sets ``require_signed: true``.
    signing_key_ref: str | None = None


@dataclass
class RetrievalSettings(ModelMixin):
    """How hard the answering workflows look before they answer.

    These knobs already existed — as untyped keys inside
    ``assistant.workflow_options``, documented only in a workflow module's
    ``DEFAULTS`` dict. That made them invisible to the config surface: not in
    the schema, not validated, not editable from the UI, and not something an
    MCP client could ask about. This block is their typed home.

    Precedence is deliberately *low*: these are merged **under**
    ``assistant.workflow_options``, which is merged under the per-request
    ``options``. So an existing config that tuned ``workflow_options`` keeps
    winning, and an agent overriding a criterion for one call still wins over
    both (see :func:`pheasant.assistant.workflows.resolve_options`).

    A field left at ``None`` is not merged at all — the workflow's own
    ``DEFAULTS`` apply, which is what makes this block additive rather than a
    second source of truth for values it does not care about.
    """

    #: plan → retrieve → grade turns before answering with what is in hand.
    max_rounds: int | None = 2
    #: Passages fetched per query per search mode.
    per_query_results: int | None = 6
    #: Total passages offered to the synthesis step.
    max_context_passages: int | None = 10
    #: Search modes to fan out over. "vector" is dropped automatically when
    #: no vector index is built, so leaving it on is safe.
    retrieval_modes: list[str] | None = field(default_factory=lambda: ["text", "vector"])
    #: Walk the graph out of the best hits for structurally-related material.
    expand_graph: bool | None = True
    expand_depth: int | None = 1
    expand_per_node: int | None = 3
    #: Ask the model to grade its own evidence before answering.
    grade_evidence: bool | None = True
    #: Drop [n] markers that do not resolve to a real citation.
    verify_citations: bool | None = True
    #: Graph facts surfaced alongside the answer.
    max_facts: int | None = 12

    def as_options(self) -> dict[str, Any]:
        """The subset that is actually set, as workflow-option keys."""
        return {key: value for key, value in self.model_dump().items() if value is not None}


@dataclass
class AssistantSettings(ModelMixin):
    """Grounded chat over the knowledge graph (query-time only).

    The assistant is a **retrieval-time** surface: it runs the ordinary
    hybrid self-search, assembles the hits into a grounded prompt, and asks
    a chat model to answer with citations. It is never part of the indexing
    path, so determinism there is untouched — and with no provider
    configured it falls back to a deterministic extractive answer, which is
    what the offline test suite exercises.

    ``provider: "auto"`` picks the first provider whose ``api_key_env`` is
    populated in the server environment (Anthropic, then OpenAI, then
    Gemini). ``allow_session_keys`` lets a UI user paste a key for the
    lifetime of a browser session: it is held in process memory only, keyed
    by an opaque token, and is never written to config, state, or logs.
    """

    enabled: bool = True
    provider: str = "auto"  # auto | anthropic | openai | gemini | none
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    allow_session_keys: bool = True
    session_key_ttl_minutes: int = 720
    max_context_chunks: int = 8
    max_output_tokens: int = 4096
    request_timeout_seconds: float = 90.0
    max_facts: int = 12
    # Which question-answering workflow runs. "auto" picks the LangGraph
    # agent when the [agent] extra is installed AND a model is reachable,
    # else the single-pass workflow. Any name registered through the
    # `pheasant.agent_workflows` entry-point group is also valid.
    workflow: str = "auto"
    # Per-workflow knobs, passed through untouched (see the workflow's
    # DEFAULTS for the keys it honors). Merged OVER `retrieval` below, so a
    # config that already tuned these keeps behaving exactly as it did.
    workflow_options: dict[str, Any] = field(default_factory=dict)
    # Typed retrieval criteria (rounds, depth, breadth). See RetrievalSettings.
    retrieval: RetrievalSettings = field(default_factory=RetrievalSettings)


@dataclass
class MemorySettings(ModelMixin):
    """Agent-memory consolidation policy (Step 33.2).

    Consolidation archives superseded records (an explicit correction is the
    only default trigger); per-scope TTL decay is opt-in — ``None`` means the
    scope never expires. Archived record files are renamed in place
    (``.md.archived``) and never deleted.
    """

    consolidation_enabled: bool = True
    session_ttl_days: int | None = None
    user_ttl_days: int | None = None
    org_ttl_days: int | None = None

    # --- retrieval (Steps 33.6-33.9) -------------------------------------
    #: How memory takes part in a search that does not say: ``auto`` (like any
    #: other source), ``off``, ``only`` or ``prefer``. A per-call ``memory``
    #: argument always wins over this.
    default_policy: str = "auto"
    #: Let ``alias``/``preference``/``exclusion`` records steer *ranking*, not
    #: just be retrievable (Step 33.8). Off by default: a memory that silently
    #: re-orders results is a surprise unless it was asked for.
    steering_enabled: bool = False
    #: Count which memories retrieval actually returns, so salience can reflect
    #: use (Step 33.9). Off by default — it is a write on the read path, and
    #: recording what a person looks up is a choice an operator should make.
    usage_tracking: bool = False
    #: Archive the least salient records once the store exceeds this many.
    #: ``None`` = unbounded, which is the pre-33.9 behavior.
    max_records: int | None = None
    #: Cap on `about` edges drawn per record by the graph bridge (Step 33.7).
    #: Total `about` edges stay bounded by this times the record count — the
    #: ceiling the retired concept layer never had.
    about_max_targets: int = 3


@dataclass
class IdPSettings(ModelMixin):
    """External identity-provider group sync (Step 32.4).

    Disabled by default — the 32.2 config-mapped ``security.groups`` stays
    the deterministic core. When enabled, the region pulls a SCIM 2.0
    ``/Groups`` listing every ``sync_interval_minutes`` (scheduler beat, or
    ``POST /security/idp/sync``) into SQLite; the bearer token is read from
    the environment variable named by ``api_key_env`` and never stored.
    ``staleness_max_minutes`` is the SLA: a mapping older than this grants
    nothing (fail closed) until the next successful sync.
    """

    enabled: bool = False
    provider: str = "scim"
    base_url: str = ""
    api_key_env: str = "IDP_TOKEN"
    sync_interval_minutes: int = 60
    staleness_max_minutes: int = 1440


@dataclass
class SecuritySettings(ModelMixin):
    allow_workspace_roots: list[Path] = field(
        default_factory=lambda: [Path("/workspace"), Path("/exports")]
    )
    # Step 32.2 — principal-aware retrieval. Off by default: a standalone /
    # single-user region behaves byte-identically to pre-32. When enforced,
    # un-ACL'd artifacts follow default_visibility ("public" keeps local
    # sources searchable; "private" requires an authenticated principal),
    # and `groups` maps principal ids to group identities (IdP sync = 32.4).
    acl_enforced: bool = False
    default_visibility: str = "public"
    groups: dict[str, list[str]] = field(default_factory=dict)
    idp: IdPSettings = field(default_factory=IdPSettings)
    allow_user_selected_source_paths: bool = True
    read_only_sources: bool = True
    deny_path_traversal: bool = True
    default_exclude_secrets: bool = True


@dataclass
class RepoSettings(ModelMixin):
    branch_policy: str = "current"
    include_uncommitted: bool = True
    commit_trigger: bool = True
    dependency_graph: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkingSettings(ModelMixin):
    enabled: bool = True
    strategy: str = "semantic"
    max_chars: int = 4000
    overlap_chars: int = 400


@dataclass
class SourceSyncSettings(ModelMixin):
    on_startup: bool = True
    on_file_change: str | bool = "debounce"
    on_git_commit: bool = True
    interval_seconds: int | None = None


@dataclass
class SourceConnectorSettings(ModelMixin):
    allow_experimental: bool = False
    request_timeout_seconds: int = 10
    headers: dict[str, str] = field(default_factory=dict)
    # Name of the environment variable holding the connector's API token
    # (Step 31.2+ SaaS connectors). The secret itself never lands in config.
    api_key_env: str | None = None
    api_endpoint: str | None = None
    api_items_field: str = "items"
    api_content_field: str = "content"
    s3_bucket: str | None = None
    s3_prefix: str = ""
    # Synapse Step 34.1+: "native" (default, in-process trusted Python) or
    # "sandboxed" (fuel/memory-capped WASM guest via pheasant.sandbox).
    # Opt-in per source; unset is byte-identical to pre-34.1.
    runtime: str = "native"
    # Hostnames a sandboxed connector's guest may reach via host_fetch.
    # Empty (default) denies every fetch.
    allowed_hosts: list[str] = field(default_factory=list)
    # Path to a guest module (.wat text or compiled .wasm) for a sandboxed
    # connector. None uses the connector class's bundled reference guest.
    wasm_module_path: str | None = None


@dataclass
class TaxonomySettings(ModelMixin):
    """Structural taxonomy extraction for one source (books, procedures, legal).

    Highly structured documents carry their own outline — Part / Chapter /
    Article / Section / § / 1.2.3 / (a). With this on, that outline is
    detected per artifact and used three ways: each chunk is labelled with the
    section it falls inside (``chunks.heading_path``, which ``chunks_fts``
    already weights at 2.0 — double the body text), `heading` graph nodes and
    `has_heading` edges are emitted so the taxonomy is traversable, and
    ``GET /taxonomy`` can render the tree.

    Detection is rule-based, deterministic and offline — no model, no network.

    ``enabled`` defaults to **false**, and is a *per-source* switch rather than
    a global one, because the numbering rules are genuinely ambiguous on
    ordinary prose: ``1. Introduction`` in a standards document is a section,
    ``1. Buy milk`` in a note is a list item, and nothing in the line tells
    them apart. Turning it on per source is how the operator says "this
    source really is structured". Enabling it also changes what the FTS index
    holds for that source, so it wants a deliberate re-sync.

    ``detect`` narrows the rule set when a corpus only uses some conventions;
    an empty list means all rules. Valid names: ``markdown``, ``keyword``
    (Chapter/Article/Section/...), ``code`` (``§``), ``numbered``
    (``1.2.3``), ``lettered`` (``(a)``/``(iv)``), ``caps`` (ALL-CAPS lines —
    the noisiest, and the first one to drop if a corpus shouts).
    """

    enabled: bool = False
    max_depth: int = 6
    detect: list[str] = field(default_factory=list)
    #: Emit `heading` graph nodes + `has_heading` edges. Chunk labelling is
    #: independent of this: a source can populate `heading_path` for search
    #: without growing the graph.
    graph_nodes: bool = True
    #: Cut chunks at section boundaries so one chunk is one section (still
    #: subdivided when a section exceeds ``chunking.max_chars``).
    #:
    #: On by default *within* an enabled taxonomy, because it is what makes
    #: the feature useful rather than decorative. Without it a whole contract
    #: that fits in one 4000-char chunk gets a single ``heading_path`` — its
    #: first heading — and a chunk spanning several sections is labelled with
    #: only the section its first line falls in, which is actively
    #: misleading. With it, "what does § 12.3 say" retrieves § 12.3.
    #:
    #: It does change chunk boundaries for the source, but enabling taxonomy
    #: is already a deliberate re-index; set it false to keep the existing
    #: boundaries and accept coarser labels.
    split_on_sections: bool = True


@dataclass
class SourceConfig(ModelMixin):
    name: str
    type: SourceType | PluginSourceType
    path: Path
    description: str | None = None
    enabled: bool = True
    max_depth: int | None = None
    include: list[str] = field(
        default_factory=lambda: [
            "**/*.py",
            "**/*.md",
            "**/*.txt",
            "**/*.yaml",
            "**/*.yml",
            "**/*.toml",
            "**/*.json",
        ]
    )
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    repo: RepoSettings = field(default_factory=RepoSettings)
    chunking: ChunkingSettings = field(default_factory=ChunkingSettings)
    sync: SourceSyncSettings = field(default_factory=SourceSyncSettings)
    connector: SourceConnectorSettings = field(default_factory=SourceConnectorSettings)
    taxonomy: TaxonomySettings = field(default_factory=TaxonomySettings)
    urls: list[str] = field(default_factory=list)
    #: Per-source override of ``sync.limits``. ``None`` inherits the global
    #: block; set it to widen or tighten one source without touching others.
    limits: SyncLimitsSettings | None = None


@dataclass
class PheasantConfig(ModelMixin):
    pheasant: PheasantSettings = field(default_factory=PheasantSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    search: SearchSettings = field(default_factory=SearchSettings)
    ingestion: IngestionSettings = field(default_factory=IngestionSettings)
    sync: SyncSettings = field(default_factory=SyncSettings)
    graph: GraphSettings = field(default_factory=GraphSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    synapse: SynapseSettings = field(default_factory=SynapseSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    assistant: AssistantSettings = field(default_factory=AssistantSettings)
    sources: list[SourceConfig] = field(default_factory=list)

    @classmethod
    def model_validate(cls, data: dict[str, Any]) -> PheasantConfig:
        def build(dc, raw):
            raw = raw or {}
            _coerce_scalar_fields(dc, raw)
            if dc is PheasantSettings:
                for key in ("state_path", "workspace_root", "exports_path"):
                    if key in raw:
                        raw[key] = Path(raw[key])
            if dc is StorageSettings:
                for key in ("sqlite_path", "graph_path", "manifest_path"):
                    if key in raw and raw[key] is not None:
                        raw[key] = Path(raw[key])
            if dc is SecuritySettings:
                if "allow_workspace_roots" in raw:
                    raw["allow_workspace_roots"] = [
                        Path(item) for item in raw["allow_workspace_roots"] or []
                    ]
                if "idp" in raw and isinstance(raw["idp"], dict):
                    raw["idp"] = build(IdPSettings, raw["idp"])
            if dc is ServerSettings:
                if "mcp" in raw and isinstance(raw["mcp"], dict):
                    raw["mcp"] = build(McpSettings, raw["mcp"])
                if "api" in raw and isinstance(raw["api"], dict):
                    raw["api"] = build(ApiSettings, raw["api"])
                if "ui" in raw and isinstance(raw["ui"], dict):
                    raw["ui"] = build(UiSettings, raw["ui"])
            if dc is VectorStoreSettings:
                if raw.get("path") is not None:
                    raw["path"] = Path(raw["path"])
            if dc is SearchSettings:
                if "embeddings" in raw and isinstance(raw["embeddings"], dict):
                    raw["embeddings"] = build(EmbeddingsSettings, raw["embeddings"])
                if "vector_store" in raw and isinstance(raw["vector_store"], dict):
                    raw["vector_store"] = build(VectorStoreSettings, raw["vector_store"])
            if dc is AssistantSettings:
                if "retrieval" in raw and isinstance(raw["retrieval"], dict):
                    raw["retrieval"] = build(RetrievalSettings, raw["retrieval"])
            if dc is IngestionSettings:
                if "captioner" in raw and isinstance(raw["captioner"], dict):
                    raw["captioner"] = build(CaptionerSettings, raw["captioner"])
                if "transcriber" in raw and isinstance(raw["transcriber"], dict):
                    raw["transcriber"] = build(TranscriberSettings, raw["transcriber"])
                if "extractor" in raw and isinstance(raw["extractor"], dict):
                    raw["extractor"] = build(ExtractorSettings, raw["extractor"])
            if dc is SyncSettings:
                if "watcher" in raw and isinstance(raw["watcher"], dict):
                    raw["watcher"] = build(WatcherSettings, raw["watcher"])
                if "git" in raw and isinstance(raw["git"], dict):
                    raw["git"] = build(GitSettings, raw["git"])
                if "scheduler" in raw and isinstance(raw["scheduler"], dict):
                    raw["scheduler"] = build(SchedulerSettings, raw["scheduler"])
                if "limits" in raw and isinstance(raw["limits"], dict):
                    raw["limits"] = build(SyncLimitsSettings, raw["limits"])
                if "concurrency" in raw and isinstance(raw["concurrency"], dict):
                    raw["concurrency"] = build(SyncConcurrencySettings, raw["concurrency"])
            return dc(**{k: v for k, v in raw.items() if k in dc.__dataclass_fields__})

        cfg = cls(
            pheasant=build(PheasantSettings, data.get("pheasant")),
            server=build(ServerSettings, data.get("server")),
            storage=build(StorageSettings, data.get("storage")),
            search=build(SearchSettings, data.get("search")),
            ingestion=build(IngestionSettings, data.get("ingestion")),
            sync=build(SyncSettings, data.get("sync")),
            graph=build(GraphSettings, data.get("graph")),
            security=build(SecuritySettings, data.get("security")),
            synapse=build(SynapseSettings, data.get("synapse")),
            memory=build(MemorySettings, data.get("memory")),
            assistant=build(AssistantSettings, data.get("assistant")),
            sources=[],
        )
        cfg.sources = []
        for raw in data.get("sources", []) or []:
            raw = dict(raw)
            try:
                raw["type"] = SourceType(raw.get("type", "single_file"))
            except ValueError:
                raw["type"] = PluginSourceType(str(raw.get("type")))
            src_path = Path(raw["path"])
            # Anchor a relative filesystem source path to workspace_root so a
            # config written as `path: docs` means "<workspace_root>/docs", not
            # "<cwd>/docs" — the container CWD is /app, which would silently
            # resolve off the mounted workspace and index nothing.
            if not src_path.is_absolute() and raw["type"] in FILESYSTEM_SOURCE_TYPES:
                src_path = cfg.pheasant.workspace_root / src_path
            raw["path"] = src_path
            if "repo" in raw:
                raw["repo"] = build(RepoSettings, raw["repo"])
            if "chunking" in raw:
                raw["chunking"] = build(ChunkingSettings, raw["chunking"])
            if "sync" in raw:
                raw["sync"] = build(SourceSyncSettings, raw["sync"])
            if "connector" in raw:
                raw["connector"] = build(SourceConnectorSettings, raw["connector"])
            if "taxonomy" in raw:
                raw["taxonomy"] = build(TaxonomySettings, raw["taxonomy"])
            if raw.get("limits") is not None:
                raw["limits"] = build(SyncLimitsSettings, raw["limits"])
            _coerce_scalar_fields(SourceConfig, raw)
            cfg.sources.append(
                SourceConfig(
                    **{k: v for k, v in raw.items() if k in SourceConfig.__dataclass_fields__}
                )
            )
        state = cfg.pheasant.state_path
        cfg.storage.sqlite_path = cfg.storage.sqlite_path or state / "pheasant.db"
        cfg.storage.graph_path = cfg.storage.graph_path or state / "graphs"
        cfg.storage.manifest_path = cfg.storage.manifest_path or state / "manifests"
        return cfg

    def effective_source(
        self,
        source: SourceConfig,
        *,
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> SourceConfig:
        """The source as the sync path should actually see it.

        Two deployment-wide policies are folded in here rather than at every
        call site, because the connector API takes only ``(source, state)``
        and third-party plugins must keep working unchanged:

        * ``security.default_exclude_secrets`` unions :data:`SECRET_EXCLUDES`
          into the exclude list. This has to happen *after* any caller-
          supplied ``exclude``, because supplying one replaces the field
          wholesale — which used to drop every credential pattern silently.
        * ``sync.limits`` fills in a per-source budget when the source does
          not carry its own.

        ``max_depth`` overrides the source's own depth for this call, and
        ``full_scan`` means "I know what I am asking for": no depth cap and
        no budget. Both are per-invocation, never persisted.
        """
        import copy

        resolved = copy.deepcopy(source)
        if self.security.default_exclude_secrets:
            existing = list(resolved.exclude or [])
            for pattern in SECRET_EXCLUDES:
                if pattern not in existing:
                    existing.append(pattern)
            resolved.exclude = existing
        if full_scan:
            inherited = resolved.limits or self.sync.limits
            resolved.max_depth = None
            resolved.limits = SyncLimitsSettings(
                max_files=None,
                max_file_size_mb=None,
                max_total_mb=None,
                follow_symlinks=bool(getattr(inherited, "follow_symlinks", False)),
            )
            return resolved
        if max_depth is not None:
            resolved.max_depth = max_depth
        if resolved.limits is None:
            resolved.limits = self.sync.limits
        return resolved

    @property
    def knowledge_base_id(self) -> str:
        return self.pheasant.name

    @property
    def state_path(self) -> Path:
        return self.pheasant.state_path
