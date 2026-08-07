"""``pheasant setup`` — the interactive, sectioned configuration wizard.

This replaces the prose "config wizard" that used to live in
``agent/config_wizard_prompt.md`` and be executed by a coding agent. That
approach had three problems this module exists to not have:

* **Determinism.** The same answers here always produce the same
  ``pheasant.yaml``. Everything else in the indexing path is deterministic on
  purpose (CLAUDE.md §4 rule 1); configuration had no business being the
  exception.
* **Reachability.** A user with a terminal and Docker can run this. They could
  not run a procedure that required a coding agent to interpret it.
* **Freshness.** Every default here is read off the live dataclasses in
  :mod:`pheasant.config.schema` via :func:`schema_default` — never copied. A
  field whose default changes changes here too, with no second edit and
  nothing to rot. :mod:`tests.test_config_surface_freshness` holds the
  remaining mechanical line: every config section must be *covered* by a
  section below.

Structure: a flat list of :class:`Section`, each with a blurb that explains
what the section is for before anything is asked, and a list of
:class:`Question` whose ``default`` is always a working answer — pressing
Enter through the entire wizard is a supported, sensible path.

Secrets never enter the YAML. A ``kind="secret"`` question stores the *name*
of an environment variable in the config and the *value* in a ``.env`` file
written with mode 0600, which is also checked against ``.gitignore``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pheasant.config.schema import PheasantConfig

#: Where an interrupted run leaves its answers, so a second invocation can
#: pick up where it stopped rather than starting the interview again.
PROGRESS_FILENAME = ".pheasant-setup.json"

QuestionKind = str  # text | bool | int | float | choice | list | secret | path


def schema_default(dotted: str) -> Any:
    """The live default for a dotted config path, read off the dataclasses.

    ``schema_default("search.embeddings.model")`` instantiates a default
    :class:`~pheasant.config.schema.PheasantConfig` and walks to the field, so
    a default that changes in the schema changes in the wizard with no second
    edit. A typo raises immediately (at import time, via the module-level
    section list) rather than silently offering ``None``.
    """
    cursor: Any = PheasantConfig()
    for part in dotted.split("."):
        cursor = getattr(cursor, part)
    if isinstance(cursor, Path):
        return str(cursor)
    return cursor


@dataclass
class Choice:
    value: str
    label: str
    detail: str = ""


@dataclass
class Question:
    """One prompt. ``key`` is the dotted config path the answer is written to."""

    key: str
    prompt: str
    help: str = ""
    kind: QuestionKind = "text"
    default: Any = None
    choices: Sequence[Choice] = ()
    #: Only asked when this returns True for the answers gathered so far.
    when: Callable[[dict[str, Any]], bool] | None = None
    #: Asked only with ``--advanced``; otherwise the default is taken silently.
    advanced: bool = False
    #: ``kind="secret"`` only: dotted path of the field naming the env var.
    secret_env_key: str | None = None

    def resolved_default(self) -> Any:
        if self.default is not None or self.kind == "secret":
            return self.default
        return schema_default(self.key)


@dataclass
class Section:
    id: str
    title: str
    blurb: str
    #: Top-level ``PheasantConfig`` fields this section configures. The
    #: freshness test asserts every one of them is claimed by some section.
    covers: tuple[str, ...]
    questions: list[Question] = field(default_factory=list)


# --------------------------------------------------------------------------
# The interview
# --------------------------------------------------------------------------


def build_sections() -> list[Section]:
    """Every section, in the order they are asked.

    Deliberately a function rather than a module constant: it reads live
    schema defaults, and building it at import time would freeze them against
    a config object constructed before any test monkeypatching.
    """
    return [
        Section(
            id="identity",
            title="Knowledge base",
            blurb=(
                "What this knowledge base is called. The name is its ID everywhere "
                "else — in the graph root node, in MCP tool calls, and (if you ever "
                "join a Synapse fleet) in the contract it publishes. Changing it "
                "later means re-indexing, so pick something stable."
            ),
            covers=("pheasant",),
            questions=[
                Question(
                    key="pheasant.name",
                    prompt="Knowledge base name",
                    help="Lowercase, no spaces. Used as the stable ID.",
                ),
                Question(
                    key="pheasant.description",
                    prompt="One-line description",
                    help="Shown in the UI and published in the Synapse contract.",
                ),
                Question(
                    key="pheasant.log_level",
                    prompt="Log level",
                    kind="choice",
                    choices=[
                        Choice("INFO", "INFO", "Normal operation."),
                        Choice("DEBUG", "DEBUG", "Verbose; useful when a sync misbehaves."),
                        Choice("WARNING", "WARNING", "Quiet."),
                    ],
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="paths",
            title="Where things live on disk",
            blurb=(
                "pheasant splits its storage three ways: /state is operational truth "
                "(SQLite, the graph, manifests) and is user data — back it up; /vault "
                "is the human-readable Obsidian projection; /exports holds regenerable "
                "payloads. The defaults are the container paths; running outside "
                "Docker you probably want local directories."
            ),
            covers=("storage",),
            questions=[
                Question(
                    key="pheasant.state_path",
                    prompt="State directory",
                    kind="path",
                    help="SQLite + graph + manifests. Treat as user data.",
                ),
                Question(
                    key="pheasant.vault_path",
                    prompt="Vault directory (Obsidian projection)",
                    kind="path",
                ),
                Question(
                    key="pheasant.exports_path",
                    prompt="Exports directory",
                    kind="path",
                    advanced=True,
                ),
                Question(
                    key="pheasant.workspace_root",
                    prompt="Workspace root",
                    kind="path",
                    help="A relative source path is resolved against this.",
                ),
                Question(
                    key="storage.graph_snapshots",
                    prompt="Keep compressed graph snapshots after each sync?",
                    kind="bool",
                    help="Timestamped zstd history beside the live graph. Cheap; "
                    "bounded by the size cap below.",
                    advanced=True,
                ),
                Question(
                    key="storage.max_state_size_gb",
                    prompt="Cap total snapshot size (GB)",
                    kind="float",
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="sources",
            title="Sources",
            blurb=(
                "What to index. Point this at anything readable: a folder, a git "
                "checkout or clone URL, an Obsidian vault, a single file, a list of "
                "web pages, or a connector (notion, slack, gdrive, confluence, imap). "
                "Paste one target per prompt; blank when you are done. You can add "
                "more later from the UI or with `pheasant up <target>`."
            ),
            covers=("sources",),
            questions=[],  # handled by _ask_sources
        ),
        Section(
            id="search",
            title="Search",
            blurb=(
                "How self-search answers a query. 'hybrid' merges keyword (BM25), "
                "vector and graph arms by reciprocal rank fusion; 'text' is keyword "
                "only and needs no model or extra dependency."
            ),
            covers=("search",),
            questions=[
                Question(
                    key="search.default_mode",
                    prompt="Default search mode",
                    kind="choice",
                    choices=[
                        Choice("hybrid", "hybrid", "Keyword + vector + graph, fused."),
                        Choice("text", "text", "Keyword only. No model, no extras."),
                        Choice("vector", "vector", "Semantic only. Needs embeddings on."),
                        Choice("graph", "graph", "Relationship traversal only."),
                    ],
                ),
                Question(
                    key="search.max_results_default",
                    prompt="Results per query by default",
                    kind="int",
                ),
            ],
        ),
        Section(
            id="embeddings",
            title="Semantic search (embeddings)",
            blurb=(
                "Optional. Off by default and pheasant is fully usable without it — "
                "keyword search needs no model, no network and no API key. Turn it on "
                "to match on meaning rather than words. The 'stub' provider is "
                "deterministic and offline, which is a real way to try the plumbing "
                "before spending anything."
            ),
            covers=(),  # part of `search`, claimed above
            questions=[
                Question(
                    key="search.embeddings.enabled",
                    prompt="Enable semantic search?",
                    kind="bool",
                ),
                Question(
                    key="search.embeddings.provider",
                    prompt="Embedding provider",
                    kind="choice",
                    choices=[
                        Choice(
                            "openai-spec",
                            "OpenAI-spec endpoint",
                            "OpenAI, or any gateway/self-hosted server speaking the same shape.",
                        ),
                        Choice("stub", "Deterministic stub", "Offline, no key, no network."),
                    ],
                    when=lambda a: bool(a.get("search.embeddings.enabled")),
                ),
                Question(
                    key="search.embeddings.model",
                    prompt="Embedding model",
                    when=lambda a: a.get("search.embeddings.provider") == "openai-spec",
                ),
                Question(
                    key="search.embeddings.base_url",
                    prompt="Embeddings base URL",
                    when=lambda a: a.get("search.embeddings.provider") == "openai-spec",
                    advanced=True,
                ),
                Question(
                    key="search.embeddings.api_key_env",
                    prompt="Environment variable holding the API key",
                    help="Only the NAME goes in the config. The value goes to .env.",
                    when=lambda a: a.get("search.embeddings.provider") == "openai-spec",
                ),
                Question(
                    key="__secret__.embeddings",
                    prompt="Paste the embedding API key (blank to set it yourself later)",
                    kind="secret",
                    secret_env_key="search.embeddings.api_key_env",
                    when=lambda a: a.get("search.embeddings.provider") == "openai-spec",
                ),
                Question(
                    key="search.vector_store.provider",
                    prompt="Vector store backend",
                    kind="choice",
                    choices=[
                        Choice("numpy", "numpy", "Flat file. Always available, no extra."),
                        Choice("lancedb", "lancedb", "Needs the [vector] extra."),
                    ],
                    when=lambda a: bool(a.get("search.embeddings.enabled")),
                ),
            ],
        ),
        Section(
            id="assistant",
            title="Answering questions (the assistant)",
            blurb=(
                "Grounded chat over the index. This is query-time only — it never "
                "touches the indexing path. With no model configured, answers are "
                "extractive (the retrieved passages, attributed), which works offline "
                "and is what the test suite runs on. 'auto' picks whichever provider "
                "has a key in the environment."
            ),
            covers=("assistant",),
            questions=[
                Question(
                    key="assistant.enabled",
                    prompt="Enable the assistant?",
                    kind="bool",
                ),
                Question(
                    key="assistant.provider",
                    prompt="Chat provider",
                    kind="choice",
                    choices=[
                        Choice("auto", "auto", "First provider with a key in the env."),
                        Choice("anthropic", "Anthropic", "ANTHROPIC_API_KEY"),
                        Choice("openai", "OpenAI", "OPENAI_API_KEY"),
                        Choice("gemini", "Gemini", "GEMINI_API_KEY"),
                        Choice("none", "None", "Extractive answers only. No network."),
                    ],
                    when=lambda a: bool(a.get("assistant.enabled")),
                ),
                Question(
                    key="__secret__.assistant",
                    prompt="Paste the chat API key (blank to set it yourself later)",
                    kind="secret",
                    secret_env_key="__provider_key_env__",
                    when=lambda a: a.get("assistant.provider") in {"anthropic", "openai", "gemini"},
                ),
                Question(
                    key="assistant.workflow",
                    prompt="Answering workflow",
                    kind="choice",
                    choices=[
                        Choice(
                            "auto",
                            "auto",
                            "Agentic when [agent] is installed and a "
                            "model is reachable, else single-pass.",
                        ),
                        Choice("agentic", "agentic", "Plan → retrieve → grade loop."),
                        Choice("simple", "simple", "One retrieval pass. Fastest."),
                    ],
                    when=lambda a: bool(a.get("assistant.enabled")),
                ),
            ],
        ),
        Section(
            id="retrieval",
            title="Retrieval tuning",
            blurb=(
                "How hard the assistant looks before it answers. More rounds and more "
                "passages means better answers on hard questions and slower, more "
                "expensive ones on easy questions. These are the same knobs an agent "
                "can override per-call over MCP, so you can test a setting before "
                "committing it here."
            ),
            covers=(),  # part of `assistant`, claimed above
            questions=[
                Question(
                    key="assistant.retrieval.max_rounds",
                    prompt="Retrieval rounds (plan → retrieve → grade turns)",
                    kind="int",
                    help="1 disables the re-plan loop.",
                ),
                Question(
                    key="assistant.retrieval.per_query_results",
                    prompt="Passages fetched per query per mode",
                    kind="int",
                ),
                Question(
                    key="assistant.retrieval.max_context_passages",
                    prompt="Passages offered to the answer step",
                    kind="int",
                ),
                Question(
                    key="assistant.retrieval.expand_graph",
                    prompt="Walk the knowledge graph out of the best hits?",
                    kind="bool",
                    help="Reaches documents that share no vocabulary with the "
                    "question but are connected structurally.",
                ),
                Question(
                    key="assistant.retrieval.expand_depth",
                    prompt="Graph expansion depth",
                    kind="int",
                    when=lambda a: bool(a.get("assistant.retrieval.expand_graph")),
                    advanced=True,
                ),
                Question(
                    key="assistant.retrieval.grade_evidence",
                    prompt="Ask the model to grade its evidence before answering?",
                    kind="bool",
                    advanced=True,
                ),
                Question(
                    key="assistant.retrieval.retrieval_modes",
                    prompt="Search modes to fan out over (comma-separated)",
                    kind="list",
                    help="text, vector, graph, hybrid. 'vector' is dropped "
                    "automatically when no vector index exists.",
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="sync",
            title="Keeping the index fresh",
            blurb=(
                "Re-indexing is incremental and idempotent: unchanged content is "
                "skipped by content hash, so a scheduled beat over a quiet source "
                "costs almost nothing. The watcher reacts to file changes; the "
                "scheduler is the backstop for anything the watcher cannot see."
            ),
            covers=("sync",),
            questions=[
                Question(
                    key="sync.watcher.enabled",
                    prompt="Watch source directories for changes?",
                    kind="bool",
                ),
                Question(
                    key="sync.scheduler.enabled",
                    prompt="Re-sync on a timer?",
                    kind="bool",
                ),
                Question(
                    key="sync.scheduler.interval_seconds",
                    prompt="Timer interval (seconds)",
                    kind="int",
                    when=lambda a: bool(a.get("sync.scheduler.enabled")),
                ),
                Question(
                    key="sync.limits.max_files",
                    prompt="Refuse a source with more files than",
                    kind="int",
                    help="A guardrail, not a target: pointing a source at a home "
                    "directory should stop with a message, not consume memory "
                    "until it dies.",
                    advanced=True,
                ),
                Question(
                    key="sync.limits.max_file_size_mb",
                    prompt="Skip any single file larger than (MB)",
                    kind="int",
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="graph",
            title="Knowledge graph",
            blurb=(
                "Artifacts, chunks, symbols and concepts, wired by contains / "
                "mentions / imports / references / similar_to edges. The one knob "
                "worth thinking about is how many documents must share a term before "
                "it earns a concept node — a concept that links one document is pure "
                "weight."
            ),
            covers=("graph",),
            questions=[
                Question(
                    key="graph.concept_min_documents",
                    prompt="Documents that must share a term to make it a concept",
                    kind="int",
                    help="1 keeps every term (much larger graph).",
                ),
            ],
        ),
        Section(
            id="ingestion",
            title="Documents, images and audio",
            blurb=(
                "Text that cannot be reached by decoding the bytes. Documents "
                "(PDF, DOCX, PPTX, XLSX, DOC, RTF, EPUB) are extracted, images "
                "captioned and audio transcribed, all into text that flows through "
                "the ordinary chunk → graph path. An authored sidecar "
                "(<file>.extract.txt / .caption.txt / .transcript.txt) always wins, "
                "and none of the three is built unless a source's include globs "
                "admit those extensions. Without the extractor a document is "
                "findable by its path and invisible by its content."
            ),
            covers=("ingestion",),
            questions=[
                Question(
                    key="ingestion.extractor.provider",
                    prompt="Document text extraction",
                    kind="choice",
                    choices=[
                        Choice("auto", "Automatic", "pymupdf/python-docx, else stdlib."),
                        Choice("native", "Third-party readers", "Best fidelity."),
                        Choice("builtin", "Standard library only", "No third-party imports."),
                        Choice(
                            "sandboxed",
                            "WASM-sandboxed PDF",
                            "For untrusted PDFs; needs the [wasm] extra.",
                        ),
                    ],
                    advanced=True,
                ),
                Question(
                    key="ingestion.captioner.provider",
                    prompt="Image captioner",
                    kind="choice",
                    choices=[
                        Choice("stub", "Deterministic stub", "Offline, no key."),
                        Choice("openai-spec", "Vision model", "OpenAI-spec chat endpoint."),
                    ],
                    advanced=True,
                ),
                Question(
                    key="ingestion.transcriber.provider",
                    prompt="Audio transcriber",
                    kind="choice",
                    choices=[
                        Choice("stub", "Deterministic stub", "Offline, no audio library."),
                        Choice("openai-spec", "Speech-to-text", "OpenAI-spec audio endpoint."),
                    ],
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="server",
            title="Server and MCP",
            blurb=(
                "The HTTP API, the web UI and the MCP server all run on one port. "
                "The API is unauthenticated by design (a local-first tool behind your "
                "own perimeter), which is why the CORS origin list is explicit rather "
                "than a wildcard — with '*', any page in your browser can drive every "
                "route on it."
            ),
            covers=("server",),
            questions=[
                Question(key="server.port", prompt="Port", kind="int"),
                Question(
                    key="server.host",
                    prompt="Bind address",
                    help="0.0.0.0 inside a container is correct; publish the port to "
                    "127.0.0.1 on the host.",
                    advanced=True,
                ),
                Question(
                    key="server.ui.enabled",
                    prompt="Serve the web UI?",
                    kind="bool",
                ),
                Question(
                    key="server.mcp.enabled",
                    prompt="Enable the MCP server (for coding agents)?",
                    kind="bool",
                ),
            ],
        ),
        Section(
            id="security",
            title="Security",
            blurb=(
                "Two things matter here. Secret exclusion unions a credential-pattern "
                "list into every filesystem source, so pointing a source at a home "
                "directory does not sweep up ~/.aws or ~/.ssh — leave it on. ACL "
                "enforcement is for multi-user deployments; a single-user region "
                "should leave it off."
            ),
            covers=("security",),
            questions=[
                Question(
                    key="security.default_exclude_secrets",
                    prompt="Always exclude credential files from indexing?",
                    kind="bool",
                ),
                Question(
                    key="security.allow_user_selected_source_paths",
                    prompt="Allow sources anywhere on the filesystem?",
                    kind="bool",
                    help="Off restricts sources to the allow-listed roots below.",
                ),
                Question(
                    key="security.acl_enforced",
                    prompt="Enforce per-principal ACLs on retrieval?",
                    kind="bool",
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="obsidian",
            title="Obsidian projection",
            blurb=(
                "Optionally mirror the index into a human-readable Obsidian vault: "
                "one note per source and file, with backlinks and a canvas. Purely "
                "additive — nothing else reads it."
            ),
            covers=("obsidian",),
            questions=[
                Question(
                    key="obsidian.enabled",
                    prompt="Write the Obsidian vault projection?",
                    kind="bool",
                ),
            ],
        ),
        Section(
            id="memory",
            title="Agent memory",
            blurb=(
                "Agents can write memories through MCP; they become ordinary indexed "
                "content, so recall is just search. Consolidation archives superseded "
                "records (renamed in place, never deleted). TTLs are opt-in — blank "
                "means a scope never expires."
            ),
            covers=("memory",),
            questions=[
                Question(
                    key="memory.consolidation_enabled",
                    prompt="Consolidate superseded memories?",
                    kind="bool",
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="synapse",
            title="Synapse federation",
            blurb=(
                "Optional. Joins this knowledge base to a Synapse fleet as a region: "
                "it publishes a semantic contract describing what it knows, and a "
                "router uses that to decide when to ask it. Everything here no-ops "
                "when publishing is off — a standalone pheasant is unaffected."
            ),
            covers=("synapse",),
            questions=[
                Question(
                    key="synapse.publish",
                    prompt="Publish a semantic contract to a Synapse router?",
                    kind="bool",
                ),
                Question(
                    key="synapse.router_url",
                    prompt="Router base URL",
                    default="",
                    when=lambda a: bool(a.get("synapse.publish")),
                ),
                Question(
                    key="synapse.fleet_id",
                    prompt="Fleet ID",
                    default="",
                    when=lambda a: bool(a.get("synapse.publish")),
                ),
                Question(
                    key="synapse.endpoint",
                    prompt="This region's endpoint, as the router will reach it",
                    default="",
                    when=lambda a: bool(a.get("synapse.publish")),
                ),
            ],
        ),
    ]


# --------------------------------------------------------------------------
# Prompting
# --------------------------------------------------------------------------


class Prompter:
    """Terminal I/O, isolated so the whole wizard is testable.

    :class:`ScriptedPrompter` below is the test double; nothing in the wizard
    reads ``input()`` or writes ``print()`` directly.
    """

    def __init__(self, *, color: bool | None = None) -> None:
        self.color = os.environ.get("NO_COLOR") is None if color is None else color

    def say(self, text: str = "") -> None:
        print(text)

    def rule(self, title: str) -> None:
        print()
        print(self._bold(f"── {title} " + "─" * max(0, 62 - len(title))))

    def ask(self, prompt: str, default_hint: str) -> str:
        suffix = f" [{default_hint}]" if default_hint else ""
        try:
            return input(f"{prompt}{suffix}: ").strip()
        except EOFError:  # piped stdin that ran out — take the defaults
            print()
            return ""

    def ask_secret(self, prompt: str) -> str:
        import getpass

        try:
            return getpass.getpass(f"{prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""

    def _bold(self, text: str) -> str:
        return f"\033[1m{text}\033[0m" if self.color else text


class ScriptedPrompter(Prompter):
    """A prompter driven by a list of canned answers (tests, ``--answers``)."""

    def __init__(self, answers: Iterable[str]) -> None:
        super().__init__(color=False)
        self._answers = list(answers)
        self.transcript: list[str] = []

    def say(self, text: str = "") -> None:
        self.transcript.append(text)

    def rule(self, title: str) -> None:
        self.transcript.append(f"== {title}")

    def ask(self, prompt: str, default_hint: str) -> str:
        self.transcript.append(f"? {prompt} [{default_hint}]")
        return self._answers.pop(0).strip() if self._answers else ""

    def ask_secret(self, prompt: str) -> str:
        self.transcript.append(f"?? {prompt}")
        return self._answers.pop(0).strip() if self._answers else ""


def _format_default(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if value is None:
        return ""
    return str(value)


def _coerce(kind: QuestionKind, raw: str, default: Any) -> Any:
    if raw == "":
        return default
    if kind == "bool":
        lowered = raw.lower()
        if lowered in {"y", "yes", "true", "on", "1"}:
            return True
        if lowered in {"n", "no", "false", "off", "0"}:
            return False
        raise ValueError("answer yes or no")
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "list":
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


#: Provider id → the env var its key is read from. Mirrors
#: ``assistant.providers.PROVIDERS`` without importing it, so the wizard has no
#: dependency on the assistant package being importable.
PROVIDER_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


class Wizard:
    """Runs the interview and renders its results."""

    def __init__(
        self,
        prompter: Prompter | None = None,
        *,
        advanced: bool = False,
        accept_defaults: bool = False,
        preset: dict[str, Any] | None = None,
    ) -> None:
        self.prompter = prompter or Prompter()
        self.advanced = advanced
        self.accept_defaults = accept_defaults
        self.answers: dict[str, Any] = dict(preset or {})
        self.secrets: dict[str, str] = {}
        self.sources: list[dict[str, Any]] = []

    # -- the interview ----------------------------------------------------

    def run(self, sections: list[Section] | None = None) -> dict[str, Any]:
        sections = sections or build_sections()
        p = self.prompter
        p.say()
        p.say("pheasant setup — this writes pheasant.yaml and, if you supply any")
        p.say("secrets, a .env file with mode 0600. Press Enter to accept a default;")
        p.say("every default is a working answer.")
        if not self.advanced:
            p.say("Re-run with --advanced to be asked about every option.")
        for section in sections:
            self._run_section(section)
        return self.answers

    def _run_section(self, section: Section) -> None:
        p = self.prompter
        p.rule(section.title)
        for line in _wrap(section.blurb):
            p.say(f"  {line}")
        p.say()
        if section.id == "sources":
            self._ask_sources()
            return
        for question in section.questions:
            self._ask(question)

    def _ask(self, question: Question) -> None:
        if question.when is not None and not question.when(self.answers):
            return
        default = question.resolved_default()
        if question.advanced and not self.advanced:
            if question.key not in self.answers and question.kind != "secret":
                self.answers[question.key] = default
            return
        if question.key in self.answers:
            return  # supplied by --answers or a resumed run
        if question.kind == "secret":
            self._ask_secret(question)
            return
        if self.accept_defaults:
            self.answers[question.key] = default
            return
        if question.help:
            for line in _wrap(question.help, width=72):
                self.prompter.say(f"    {line}")
        if question.kind == "choice":
            for choice in question.choices:
                marker = "*" if choice.value == default else " "
                detail = f" — {choice.detail}" if choice.detail else ""
                self.prompter.say(f"    {marker} {choice.value}: {choice.label}{detail}")
        while True:
            raw = self.prompter.ask(f"  {question.prompt}", _format_default(default))
            try:
                value = _coerce(question.kind, raw, default)
            except ValueError as exc:
                self.prompter.say(f"    ! {exc}")
                continue
            if question.kind == "choice" and value not in {c.value for c in question.choices}:
                self.prompter.say(
                    "    ! pick one of: " + ", ".join(c.value for c in question.choices)
                )
                continue
            self.answers[question.key] = value
            return

    def _ask_secret(self, question: Question) -> None:
        env_name = self._secret_env_name(question)
        if not env_name:
            return
        if os.environ.get(env_name):
            self.prompter.say(f"    {env_name} is already set in this environment — skipping.")
            return
        if self.accept_defaults:
            return
        self.prompter.say(f"    Stored in .env as {env_name}, never in pheasant.yaml.")
        value = self.prompter.ask_secret(f"  {question.prompt}")
        if value:
            self.secrets[env_name] = value

    def _secret_env_name(self, question: Question) -> str | None:
        key = question.secret_env_key
        if key == "__provider_key_env__":
            return PROVIDER_KEY_ENV.get(str(self.answers.get("assistant.provider")))
        if not key:
            return None
        value = self.answers.get(key)
        if value is None:
            value = schema_default(key)
        return str(value) if value else None

    def _ask_sources(self) -> None:
        if self.sources:
            return
        if self.accept_defaults:
            return
        from pheasant.targets import TargetError, resolve_target

        p = self.prompter
        while True:
            raw = p.ask("  Target (path, git URL, https:// page, s3://, notion:…)", "done")
            if not raw or raw.lower() == "done":
                break
            try:
                target = resolve_target(
                    raw,
                    clone_root=Path(".pheasant/sources"),
                    workspace=Path(".pheasant/external"),
                )
            except TargetError as exc:
                p.say(f"    ! {exc}")
                continue
            payload = target.to_source_dict()
            existing = {source["name"] for source in self.sources}
            if payload["name"] in existing:
                payload["name"] = f"{payload['name']}-{len(self.sources) + 1}"
            self.sources.append(payload)
            p.say(f"    + {payload['name']} ({payload['type']}) ← {payload['path']}")
        if not self.sources:
            p.say("    No sources yet — add them later from the UI or `pheasant up <target>`.")

    # -- rendering --------------------------------------------------------

    def config_dict(self) -> dict[str, Any]:
        """Assemble the answers into a config mapping, validated before return."""
        data: dict[str, Any] = {}
        for key, value in self.answers.items():
            if key.startswith("__"):
                continue
            _set_in(data, key, value)
        if self.sources:
            data["sources"] = [dict(source) for source in self.sources]
        # Validate here rather than at write time: a wizard that writes a file
        # the loader then rejects has wasted the whole interview.
        PheasantConfig.model_validate(json.loads(json.dumps(data, default=str)))
        return data

    def env_values(self) -> dict[str, str]:
        return dict(self.secrets)


def _set_in(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cursor = data
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def _wrap(text: str, width: int = 74) -> list[str]:
    import textwrap

    return textwrap.wrap(" ".join(text.split()), width=width) or [""]


# --------------------------------------------------------------------------
# Output files
# --------------------------------------------------------------------------


def render_config_yaml(data: dict[str, Any]) -> str:
    """YAML for a config mapping, sources rendered the way quickstart does.

    ``yaml.safe_dump`` writes list-of-dict items in a shape the dependency-light
    YAML shim in this repo cannot read back, so sources go through the same
    hand-rolled renderer ``pheasant up`` uses.
    """
    from pheasant.config.loader import dump_config_yaml
    from pheasant.quickstart import _render_sources_block

    payload = dict(data)
    sources = payload.pop("sources", None)
    rendered = dump_config_yaml(payload)
    if sources:
        rendered += _render_sources_block(sources)
    return rendered


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def merge_env_file(path: Path, values: dict[str, str]) -> tuple[str, list[str], list[str]]:
    """Merge ``values`` into an existing .env, preserving everything else.

    Returns ``(text, added, updated)``. Comments, ordering and unrelated keys
    survive: a setup run is not permitted to silently drop a key someone put
    there by hand. An existing key with the *same* value is left alone so the
    file is not rewritten for nothing.
    """
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: dict[str, int] = {}
    for index, line in enumerate(existing_lines):
        match = _ENV_LINE.match(line)
        if match:
            seen[match.group(1)] = index

    added: list[str] = []
    updated: list[str] = []
    lines = list(existing_lines)
    for name, value in values.items():
        rendered = f"{name}={_quote_env(value)}"
        if name in seen:
            if lines[seen[name]] != rendered:
                lines[seen[name]] = rendered
                updated.append(name)
        else:
            added.append(name)
            lines.append(rendered)
    text = "\n".join(lines).rstrip("\n")
    return (text + "\n" if text else ""), added, updated


def _quote_env(value: str) -> str:
    if value and not any(char.isspace() or char in "#'\"$`\\" for char in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def write_env_file(path: Path, values: dict[str, str]) -> dict[str, Any]:
    """Write the .env with mode 0600, creating parents as needed.

    0600 before the content is written, not after: a secrets file that is
    world-readable for even the length of one write is a secrets file that was
    world-readable.
    """
    text, added, updated = merge_env_file(path, values)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - filesystems without POSIX modes
        pass
    path.write_text(text, encoding="utf-8")
    return {"path": str(path), "added": added, "updated": updated, "mode": "0600"}


def ensure_gitignored(env_path: Path, repo_root: Path | None = None) -> str | None:
    """Make sure the .env is ignored by git. Returns a note when it acted.

    Writing a secrets file into a git working tree without this is how keys end
    up in a public repository, and "the wizard told me to create it" is not a
    defence anyone gets to use.
    """
    root = repo_root or env_path.parent
    if not (root / ".git").exists():
        return None
    gitignore = root / ".gitignore"
    name = env_path.name
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    patterns = {line.strip() for line in existing.splitlines()}
    if name in patterns or ".env" in patterns or "*.env" in patterns:
        return None
    suffix = "" if existing.endswith("\n") or not existing else "\n"
    gitignore.write_text(f"{existing}{suffix}{name}\n", encoding="utf-8")
    return f"Added {name} to {gitignore}"


def startup_commands(config_path: Path, target: str, port: int) -> list[str]:
    """The exact commands to run next, for the chosen deployment target."""
    if target == "docker":
        return [
            f"docker run --rm -p {port}:8765 \\",
            f"  -v {config_path.resolve()}:/config/pheasant.yaml:ro \\",
            "  -v $PWD:/workspace:ro -v pheasant-state:/state \\",
            "  ghcr.io/esatt10/pheasant:latest",
        ]
    if target == "compose":
        return [
            f"pheasant compose-env {config_path} -o .env.compose",
            "docker compose up -d",
        ]
    return [
        'pip install -e ".[mcp]"',
        f"pheasant sync -c {config_path} --all",
        f"pheasant start -c {config_path}",
    ]


# --------------------------------------------------------------------------
# Progress checkpointing
# --------------------------------------------------------------------------


def save_progress(path: Path, wizard: Wizard) -> None:
    """Answers so far, minus every secret.

    Secrets are deliberately not checkpointed: the whole point of the .env is
    that the key exists in exactly one place with mode 0600, and a resume file
    holding a copy would quietly make that two.
    """
    payload = {"answers": wizard.answers, "sources": wizard.sources}
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def load_progress(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.exists():
        return {}, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}, []
    return dict(payload.get("answers") or {}), list(payload.get("sources") or [])
