from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

DEFAULT_EXCLUDES = [
    "**/.git/**",
    "**/.env",
    "**/.env.*",
    "**/*id_rsa*",
    "**/*id_ed25519*",
    "**/*.pem",
    "**/*.key",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/.venv/**",
    "**/venv/**",
    "**/dist/**",
    "**/build/**",
    "**/.mypy_cache/**",
    "**/.pytest_cache/**",
]


class SourceType(StrEnum):
    repository = "repository"
    markdown_folder = "markdown_folder"
    obsidian_vault = "obsidian_vault"
    document_folder = "document_folder"
    web_collection = "web_collection"
    single_file = "single_file"
    s3 = "s3"
    api = "api"


# Source types whose ``path`` is a real local directory/file (as opposed to
# the URL/connector-backed web/api/s3 types). A relative ``path`` on one of
# these is anchored to ``syncsage.workspace_root`` at config-load time.
FILESYSTEM_SOURCE_TYPES = frozenset(
    {
        SourceType.repository,
        SourceType.markdown_folder,
        SourceType.obsidian_vault,
        SourceType.document_folder,
        SourceType.single_file,
    }
)


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
class SyncSageSettings(ModelMixin):
    name: str = "local-syncsage"
    description: str = "Lightweight MCP knowledge graph and retrieval server"
    environment: str = "local"
    log_level: str = "INFO"
    state_path: Path = Path("/state")
    vault_path: Path = Path("/vault")
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
    """

    graph_format: str = "node_link_json"
    graph_snapshots: bool = True
    graph_snapshot_interval_seconds: int = 900
    compression: str = "zstd"
    sqlite_path: Path | None = None
    graph_path: Path | None = None
    manifest_path: Path | None = None
    max_state_size_gb: float = 10


@dataclass
class EmbeddingsSettings(ModelMixin):
    """Optional embed-on-sync provider (Synapse 21.4). Off by default.

    ``provider`` is ``openai-spec`` (POST {base_url}/embeddings, the same
    wire format the subjective-retrieval router uses) or ``stub``
    (deterministic, offline). The API key is read from the environment
    variable named by ``api_key_env`` and never stored in config/state.
    """

    enabled: bool = False
    provider: str = "openai-spec"
    model: str = "text-embedding-3-small"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    dimensions: int = 256
    batch_size: int = 64


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
class IngestionSettings(ModelMixin):
    """Ingestion-path enrichment knobs. Standalone-safe defaults.

    ``captioner`` only takes effect for sources that include image files and
    ``transcriber`` only for sources that include audio files; a text-only
    region never builds either (and the default ``stub`` providers need no
    extra dependency anyway).
    """

    captioner: CaptionerSettings = field(default_factory=CaptionerSettings)
    transcriber: TranscriberSettings = field(default_factory=TranscriberSettings)


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
class SyncSettings(ModelMixin):
    watcher: WatcherSettings = field(default_factory=WatcherSettings)
    git: GitSettings = field(default_factory=GitSettings)
    scheduler: SchedulerSettings = field(default_factory=SchedulerSettings)


@dataclass
class ObsidianSettings(ModelMixin):
    enabled: bool = True
    write_mode: str = "upsert"
    note_root: str = "SyncSage"
    template_profile: str = "engineering"
    create_index_notes: bool = True
    create_source_notes: bool = True
    create_file_notes: bool = True
    create_chunk_notes: bool = False
    create_canvas: bool = True


@dataclass
class SynapseSettings(ModelMixin):
    """Synapse federation (Synapse 21.5). Standalone-safe: all router-facing
    behavior no-ops when ``router_url`` is unset and ``publish`` is false.

    - ``publish`` gates contract publication + the NDJSON event stream from the
      sync engine. Default off → a router-less SyncSage behaves exactly as
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
    # ``integrity.signature`` stays ``null`` and a standalone SyncSage is
    # unchanged. The router verifies the signature against an out-of-band public
    # key when its fleet sets ``require_signed: true``.
    signing_key_ref: str | None = None


@dataclass
class SecuritySettings(ModelMixin):
    allow_workspace_roots: list[Path] = field(
        default_factory=lambda: [Path("/workspace"), Path("/vault"), Path("/exports")]
    )
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
    api_endpoint: str | None = None
    api_items_field: str = "items"
    api_content_field: str = "content"
    s3_bucket: str | None = None
    s3_prefix: str = ""


@dataclass
class SourceConfig(ModelMixin):
    name: str
    type: SourceType
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
    urls: list[str] = field(default_factory=list)


@dataclass
class SyncSageConfig(ModelMixin):
    syncsage: SyncSageSettings = field(default_factory=SyncSageSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    search: SearchSettings = field(default_factory=SearchSettings)
    ingestion: IngestionSettings = field(default_factory=IngestionSettings)
    sync: SyncSettings = field(default_factory=SyncSettings)
    obsidian: ObsidianSettings = field(default_factory=ObsidianSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    synapse: SynapseSettings = field(default_factory=SynapseSettings)
    sources: list[SourceConfig] = field(default_factory=list)

    @classmethod
    def model_validate(cls, data: dict[str, Any]) -> SyncSageConfig:
        def build(dc, raw):
            raw = raw or {}
            if dc is SyncSageSettings:
                for key in ("state_path", "vault_path", "workspace_root", "exports_path"):
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
            if dc is IngestionSettings:
                if "captioner" in raw and isinstance(raw["captioner"], dict):
                    raw["captioner"] = build(CaptionerSettings, raw["captioner"])
                if "transcriber" in raw and isinstance(raw["transcriber"], dict):
                    raw["transcriber"] = build(TranscriberSettings, raw["transcriber"])
            if dc is SyncSettings:
                if "watcher" in raw and isinstance(raw["watcher"], dict):
                    raw["watcher"] = build(WatcherSettings, raw["watcher"])
                if "git" in raw and isinstance(raw["git"], dict):
                    raw["git"] = build(GitSettings, raw["git"])
                if "scheduler" in raw and isinstance(raw["scheduler"], dict):
                    raw["scheduler"] = build(SchedulerSettings, raw["scheduler"])
            return dc(**{k: v for k, v in raw.items() if k in dc.__dataclass_fields__})

        cfg = cls(
            syncsage=build(SyncSageSettings, data.get("syncsage")),
            server=build(ServerSettings, data.get("server")),
            storage=build(StorageSettings, data.get("storage")),
            search=build(SearchSettings, data.get("search")),
            ingestion=build(IngestionSettings, data.get("ingestion")),
            sync=build(SyncSettings, data.get("sync")),
            obsidian=build(ObsidianSettings, data.get("obsidian")),
            security=build(SecuritySettings, data.get("security")),
            synapse=build(SynapseSettings, data.get("synapse")),
            sources=[],
        )
        cfg.sources = []
        for raw in data.get("sources", []) or []:
            raw = dict(raw)
            raw["type"] = SourceType(raw.get("type", "single_file"))
            src_path = Path(raw["path"])
            # Anchor a relative filesystem source path to workspace_root so a
            # config written as `path: docs` means "<workspace_root>/docs", not
            # "<cwd>/docs" — the container CWD is /app, which would silently
            # resolve off the mounted workspace and index nothing.
            if not src_path.is_absolute() and raw["type"] in FILESYSTEM_SOURCE_TYPES:
                src_path = cfg.syncsage.workspace_root / src_path
            raw["path"] = src_path
            if "repo" in raw:
                raw["repo"] = build(RepoSettings, raw["repo"])
            if "chunking" in raw:
                raw["chunking"] = build(ChunkingSettings, raw["chunking"])
            if "sync" in raw:
                raw["sync"] = build(SourceSyncSettings, raw["sync"])
            if "connector" in raw:
                raw["connector"] = build(SourceConnectorSettings, raw["connector"])
            cfg.sources.append(
                SourceConfig(
                    **{k: v for k, v in raw.items() if k in SourceConfig.__dataclass_fields__}
                )
            )
        state = cfg.syncsage.state_path
        cfg.storage.sqlite_path = cfg.storage.sqlite_path or state / "syncsage.db"
        cfg.storage.graph_path = cfg.storage.graph_path or state / "graphs"
        cfg.storage.manifest_path = cfg.storage.manifest_path or state / "manifests"
        return cfg

    @property
    def knowledge_base_id(self) -> str:
        return self.syncsage.name

    @property
    def state_path(self) -> Path:
        return self.syncsage.state_path

    @property
    def vault_path(self) -> Path:
        return self.syncsage.vault_path
