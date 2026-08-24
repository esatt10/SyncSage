from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import yaml
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from pheasant.assistant.credentials import SessionKeyStore
from pheasant.config.loader import (
    ConfigError,
    dump_config_yaml,
    effective_config_dict,
    load_config,
    validate_source_paths,
)
from pheasant.config.profiles import profile_names
from pheasant.config.schema import PheasantConfig, SourceConfig, SourceType
from pheasant.deployment.roles import describe as describe_role
from pheasant.deployment.roles import resolve_role, validate_role
from pheasant.deployment.serving import RETRY_AFTER_SECONDS, ConcurrencyLimiter, DrainState
from pheasant.graph.exporter import cytoscape, node_link
from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.ingestion.pipeline import read_text, utc_now
from pheasant.jobs import JobRegistry
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore
from pheasant.registry.knowledge_base_registry import KnowledgeBaseRegistry
from pheasant.registry.source_registry import SourceRegistry
from pheasant.search.hybrid import HybridSearch
from pheasant.search.sqlite_store import SearchStore
from pheasant.security.path_policy import (
    PathPolicyError,
    resolve_config_write_target,
    resolve_under,
)
from pheasant.sync.engine import SyncEngine
from pheasant.sync.fingerprint import EMBEDDING_SCOPE, embedding_fingerprint
from pheasant.sync.remote_worker import ResultCache
from pheasant.telemetry import metrics
from pheasant.version import __version__

logger = logging.getLogger(__name__)

#: Stand-in for the schema's mandatory ``path`` on source types that pull from
#: a service rather than the filesystem. Nothing ever opens it.
PLUGIN_PLACEHOLDER_PATH = "/unused"

#: ``(id, label, description, path_role)`` for the built-in source types, in
#: the order a picker should show them. ``path_role`` is ``"required"`` when
#: the connector actually reads that path and ``"unused"`` when the schema
#: demands the field but the connector gets its content elsewhere.
#: ``SourceType.memory`` is deliberately absent: agent-memory sources are
#: created and owned by the memory store, not registered by hand.
BUILTIN_SOURCE_TYPES: tuple[tuple[str, str, str, str], ...] = (
    (
        "document_folder",
        "Folder of documents",
        "Mixed files — Markdown, PDF, DOCX, HTML, code, configs.",
        "required",
    ),
    (
        "repository",
        "Git repository",
        "A git checkout, with branch and commit metadata on every artifact.",
        "required",
    ),
    (
        "obsidian_vault",
        "Obsidian vault",
        "Notes plus wikilinks, tags and frontmatter as graph structure.",
        "required",
    ),
    (
        "markdown_folder",
        "Folder of Markdown",
        "Markdown only — the lighter version of a document folder.",
        "required",
    ),
    ("single_file", "Single file", "One file, indexed on its own.", "required"),
    (
        "web_collection",
        "Web pages",
        "A list of URLs fetched over HTTP, with ETag-based incremental sync.",
        "unused",
    ),
    ("api", "HTTP API", "A JSON endpoint paged with a cursor (experimental).", "unused"),
    ("s3", "S3 bucket", "An S3-compatible bucket prefix (experimental).", "unused"),
)


class SearchRequest(BaseModel):
    # Step 32.2 — optional caller identity; enforced only when
    # security.acl_enforced is on. The caller (router / deployment
    # perimeter) authenticates; the region enforces visibility.
    principal: str | None = None
    principal_groups: list[str] = []
    knowledge_base: str | None = None
    query: str
    mode: str = "hybrid"
    max_results: int = 10
    source_name: str | None = None
    # Restrict to one part of a document's extracted taxonomy, matched against
    # the heading breadcrumb. Only meaningful for sources with taxonomy on.
    section: str | None = None
    # Step 33.6 — the same retrieval criteria the MCP tool has always had.
    # They lived only on the MCP surface, so the same region answered a query
    # differently depending on which protocol asked; the router, which reaches
    # this region over HTTP, could not scope a search at all.
    exclude_sources: list[str] | None = None
    node_types: list[str] | None = None
    min_score: float | None = None
    # Scope by the *kind* of source (repository, notion, slack, ...) rather
    # than by name. A caller that does not already know every source in the
    # region can still say "only our wikis" or "nothing from git". Each hit
    # reports its own under `provenance.source_type`.
    source_types: list[str] | None = None
    exclude_source_types: list[str] | None = None
    # How this region's agent memory takes part: "auto" (default), "off",
    # "only", "prefer", or an object with scopes/subject/current_only/as_of.
    memory: dict | str | None = None


class MemoryEnableRequest(BaseModel):
    """Provision the agent-memory source.

    ``path`` defaults to ``<state_path>/memory`` rather than a workspace-
    relative folder, because memory is the **only** source type pheasant
    writes to. Every containerised deployment mounts the corpus read-only
    (`/workspace:ro` in both the standard and demo compose files), so
    anchoring memory there registers a source that can never be written —
    found by a live run, where the first write returned a bare 500.
    """

    name: str = "agent-memory"
    path: str | None = None


class MemoryWriteRequest(BaseModel):
    text: str
    scope: str = "user"
    subject: str | None = None
    supersedes: str | None = None
    tags: list[str] = []
    sync: bool = True
    # Step 33.5, all optional and defaulting to the pre-33.5 behavior.
    # `principal` is who asserted this; it is part of the record id, so two
    # callers writing the same sentence in the same second get two records
    # rather than silently sharing one.
    kind: str = "fact"
    principal: str | None = None
    valid_until: str | None = None


class SyncRequest(BaseModel):
    knowledge_base: str | None = None
    source_name: str | None = None
    mode: str = "incremental"
    # Per-run traversal controls. `depth` caps directory depth for this run;
    # `full_scan` lifts both the depth cap and sync.limits. Neither persists.
    depth: int | None = None
    full_scan: bool = False
    # Default True preserves the documented, tested contract: block until
    # the sync finishes and return its full result. A caller that would
    # rather not hold a connection open for a possibly-long sync (the UI's
    # "sync now" button) sets this false and polls `GET /sources`'
    # `syncing`/`sync_error` fields instead.
    wait: bool = True


class RegisterSourceRequest(BaseModel):
    name: str
    type: str = "document_folder"
    path: str
    description: str | None = None
    enabled: bool = True
    max_depth: int | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    repo: dict | None = None
    chunking: dict | None = None
    sync: dict | None = None
    connector: dict | None = None
    # Structural taxonomy extraction (chapters/sections/§ codes). The toggle
    # belongs at registration because it is a property of the *source* — "this
    # corpus is structured documentation" — not of the instance. Omitted or
    # `{"enabled": false}` leaves the source byte-identical to pre-taxonomy.
    taxonomy: dict | None = None
    urls: list[str] | None = None
    sync_now: bool = False
    sync_mode: str = "incremental"
    # See SyncRequest.wait — default True keeps register-then-block the
    # existing behavior; the UI sets this false so registering a source
    # never holds the connection open for however long the first sync
    # takes.
    wait: bool = True


class UpdateSourceRequest(BaseModel):
    type: str | None = None
    path: str | None = None
    description: str | None = None
    enabled: bool | None = None
    max_depth: int | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    taxonomy: dict | None = None
    repo: dict | None = None
    chunking: dict | None = None
    sync: dict | None = None
    connector: dict | None = None
    urls: list[str] | None = None


class PromoteSourceRequest(BaseModel):
    config_path: str | None = None
    write: bool = False


class ConfigWriteRequest(BaseModel):
    config: dict | None = None
    yaml_text: str | None = None


class ChatRequest(BaseModel):
    question: str
    # Opaque handle for a key the user pasted this session. Never the key.
    session_id: str | None = None
    mode: str = "hybrid"
    max_results: int | None = None
    source_name: str | None = None
    # Scope the answer to (or away from) kinds of source. Same axis as
    # `POST /search`'s, applied to every retrieval the answering loop runs.
    source_types: list[str] | None = None
    exclude_source_types: list[str] | None = None
    principal: str | None = None
    principal_groups: list[str] = []
    # Override assistant.workflow for this one question ("simple",
    # "agentic", or any registered plugin name).
    workflow: str | None = None
    # Per-request workflow knobs, merged over assistant.workflow_options.
    options: dict | None = None
    # How this region's agent memory takes part in the answer: "auto", "off",
    # "only", "prefer", or the full policy object (Step 33.10).
    memory: dict | str | None = None


class EmbeddingsRequest(BaseModel):
    """Turn semantic search on/off and configure the provider."""

    enabled: bool | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    dimensions: int | None = None
    batch_size: int | None = None
    store_provider: str | None = None
    # Persist to the config file as well as the live process.
    persist: bool = True
    # Build vectors for already-indexed content straight away.
    reindex: bool = False


class AssistantKeyRequest(BaseModel):
    provider: str
    api_key: str
    model: str | None = None
    base_url: str | None = None


class RetrievalRequest(BaseModel):
    """Tune the typed retrieval criteria (``assistant.retrieval``).

    Every field is optional and ``None`` means "leave it alone" — a PUT that
    sets one knob must not silently reset the other nine to their defaults.
    """

    max_rounds: int | None = None
    per_query_results: int | None = None
    max_context_passages: int | None = None
    retrieval_modes: list[str] | None = None
    expand_graph: bool | None = None
    expand_depth: int | None = None
    expand_per_node: int | None = None
    grade_evidence: bool | None = None
    verify_citations: bool | None = None
    max_facts: int | None = None
    # Write the change to the config file as well as the live process.
    persist: bool = True


class ConfigSectionRequest(BaseModel):
    """One config section's new value, plus what to do with it."""

    values: dict
    persist: bool = True


class KnowledgeBaseRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    persist: bool = True


class QuickAddRequest(BaseModel):
    """One-field source creation: paste a path or URL and go."""

    target: str
    name: str | None = None
    split: bool = False
    #: Extract a structural taxonomy (chapters/sections/§ codes) from this
    #: source's documents. Off by default, matching the per-source config.
    taxonomy: bool = False
    sync_now: bool = True
    sync_mode: str = "incremental"
    # See SyncRequest.wait — default True keeps the existing, tested
    # register-then-block-on-sync contract; the UI's quick-add sets this
    # false so pasting a large repo's URL never blocks the form (and the
    # reverse proxy in front of it) on however long the first index takes.
    wait: bool = True


class RemotePrepareRequest(BaseModel):
    """Immutable coordinator task accepted by an opt-in indexing worker."""

    source: dict
    item: dict
    payload: dict
    git_metadata: list[str | bool | None] | None = None


#: Tasks a worker will accept in one batch. Every task holds its file's bytes
#: in memory, so this is a memory bound, not a politeness limit: at the
#: 25 MB-per-file default a 64-task batch is already a 1.6 GB worst case, and
#: the coordinator's own default batch is far smaller.
MAX_PREPARE_BATCH = 64

#: Entries in the per-worker idempotency cache.
PREPARE_CACHE_SIZE = 256


class RemotePrepareBatchRequest(BaseModel):
    """Several preparation tasks in one request (Phase 35.5).

    ``idempotency_keys`` is content-addressed by the coordinator, so a retry
    after a timeout carries the same keys and is answered from cache instead
    of re-parsed. ``deadline_seconds`` is the caller's remaining budget: the
    worker stops rather than finishing a batch nobody is waiting for.
    """

    tasks: list[RemotePrepareRequest]
    idempotency_keys: list[str] | None = None
    deadline_seconds: float | None = None


#: One line per retrieval knob, so a UI (or an agent reading
#: ``describe_retrieval``) can explain a setting without the caller having to
#: go and read the workflow module's docstring.
RETRIEVAL_FIELD_HELP: dict[str, str] = {
    "max_rounds": "plan → retrieve → grade turns before answering with what is in "
    "hand. 1 disables the re-plan loop.",
    "per_query_results": "passages fetched per query per search mode.",
    "max_context_passages": "total passages offered to the answering step.",
    "retrieval_modes": "search modes to fan out over (text, vector, graph, hybrid). "
    "'vector' is dropped automatically when no vector index is built.",
    "expand_graph": "walk the knowledge graph out of the best hits, reaching "
    "documents that share no vocabulary with the question.",
    "expand_depth": "hops to walk when expanding.",
    "expand_per_node": "neighbours taken per expanded node.",
    "grade_evidence": "ask the model to grade its own evidence before answering.",
    "verify_citations": "drop [n] markers that do not resolve to a real citation.",
    "max_facts": "graph facts surfaced alongside the answer.",
}

#: Config sections that can be changed on a running server, and what changing
#: one actually costs. ``True`` means the live process picks it up; ``False``
#: means it is written to the file and needs a restart. Being honest about the
#: difference is the whole point — a UI that says "saved" for a setting the
#: process is still ignoring has lied to the user.
LIVE_APPLICABLE_SECTIONS: dict[str, bool] = {
    "search": True,
    "assistant": True,
    "graph": True,
    "memory": True,
    "ingestion": False,  # captioner/transcriber are wired at engine construction
    "sync": False,  # watcher/scheduler services are started at boot
    "security": False,  # path policy is read per request, but ACL wiring is not
    "synapse": True,
    "storage": False,
    "server": False,
    "pheasant": False,
}


def _allowed_roots(config: PheasantConfig) -> list[Path]:
    """Roots a UI may browse / register sources under.

    Mirrors the allowlist used by the MCP register tool plus the exports path.
    """
    roots = [
        config.pheasant.workspace_root,
        config.pheasant.exports_path,
        *config.security.allow_workspace_roots,
    ]
    if config.security.allow_user_selected_source_paths:
        roots.append(Path("/"))
    seen: list[Path] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved not in seen and resolved.exists():
            seen.append(resolved)
    return seen


def _configured_roots(config: PheasantConfig) -> list[Path]:
    """Browse roots for the UI, *including* configured-but-unmounted ones.

    Unlike ``_allowed_roots`` (which drops non-existent roots because they can
    never contain a selectable path), this preserves every configured root so
    the browser can render a root the operator added to
    ``security.allow_workspace_roots`` but forgot to mount — flagged
    ``mounted: false`` — instead of silently hiding it.
    """
    roots = [
        config.pheasant.workspace_root,
        config.pheasant.exports_path,
        *config.security.allow_workspace_roots,
    ]
    if config.security.allow_user_selected_source_paths:
        roots.append(Path("/"))
    seen: list[Path] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved not in seen:
            seen.append(resolved)
    return seen


def _config_write_roots(config: PheasantConfig) -> list[Path]:
    """Roots a *config file* may be written into.

    Deliberately not ``_allowed_roots``: that one honors
    ``security.allow_user_selected_source_paths``, which widens the list to
    ``/`` so a user can index any folder they like. Choosing what content to
    index is not the same permission as choosing where the server writes
    YAML, and conflating them turns source promotion into an arbitrary file
    write. Only the explicitly configured roots count here.
    """
    roots = [
        config.pheasant.workspace_root,
        config.pheasant.exports_path,
        *config.security.allow_workspace_roots,
    ]
    seen: list[Path] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved not in seen:
            seen.append(resolved)
    return seen


def _resolve_source_path(path: str, config: PheasantConfig) -> Path:
    if config.security.allow_user_selected_source_paths:
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            raise PathPolicyError(f"Path does not exist: {resolved}")
        return resolved
    return resolve_under(path, _allowed_roots(config))


def _check_source_type(type_name: str) -> bool:
    """Validate a ``sources[].type`` string; return whether it is built-in.

    YAML accepts any type a connector plugin claims (Step 31.1), so the API
    has to as well — otherwise the same source is configurable in a file and
    rejected through the UI.
    """
    from pheasant.sync.connector_registry import list_connector_types

    if type_name in {member.value for member in SourceType}:
        return True
    plugins = list_connector_types()
    if type_name in plugins:
        return False
    raise HTTPException(
        status_code=400,
        detail=f"Unknown source type: {type_name}. Built-in types: "
        + ", ".join(sorted(member.value for member in SourceType))
        + (
            f". Installed connector plugins: {', '.join(plugins)}"
            if plugins
            else ". No connector plugins are installed."
        ),
    )


def _source_from_payload(payload: dict) -> SourceConfig:
    return PheasantConfig.model_validate({"sources": [payload]}).sources[0]


def _source_payload(
    req: RegisterSourceRequest | UpdateSourceRequest,
    resolved_path: Path,
    existing: SourceConfig | None = None,
) -> dict:
    payload = existing.model_dump(mode="json") if existing else {}
    if isinstance(req, RegisterSourceRequest):
        payload.update({"name": req.name, "type": req.type})
        updates = req.model_dump(exclude={"sync_now", "sync_mode"}, exclude_none=True)
    else:
        updates = req.model_dump(exclude_unset=True)
    updates["path"] = str(resolved_path)
    payload.update(updates)
    return payload


#: Edges expanded before anything else at each hop. `contains` is the
#: directory/file hierarchy, and walking it first is what makes a bounded
#: horizon show a *tree* rather than an arbitrary slice of a flat fan-out.
HIERARCHY_EDGE_TYPES = ("contains",)


def graph_neighbors(
    graph: SimpleMultiDiGraph,
    node_id: str,
    depth: int = 1,
    edge_types: list[str] | None = None,
    max_nodes: int | None = None,
    exclude_edge_types: set[str] | None = None,
    exclude_node_types: set[str] | None = None,
) -> dict:
    """Breadth-first neighbor expansion (mirrors PheasantTools.get_graph_neighbors).

    ``max_nodes`` stops the walk once that many neighbors are collected.
    Callers that only keep the first N (the canvas asks for a bounded horizon)
    must pass it: three hops off a hub node reaches most of the graph, and
    enumerating all of it to then throw nearly all of it away is what made a
    depth-3 slice take minutes. Truncation is in BFS order either way, so the
    kept set is identical — only the work is smaller.
    """
    if node_id not in graph:
        return {"node_id": node_id, "depth": depth, "neighbors": []}
    max_depth = max(1, min(int(depth or 1), 10))
    allowed = set(edge_types or [])
    queue: deque = deque([(node_id, 0, [node_id])])
    visited = {node_id}
    neighbors: list[dict] = []
    while queue:
        if max_nodes is not None and len(neighbors) >= max_nodes:
            break
        current, current_depth, path = queue.popleft()
        if current_depth >= max_depth:
            continue
        # Hierarchy first. A source node carries an `indexes` shortcut to every
        # one of its artifacts, so a plain fan-out spends the whole budget
        # jumping straight to files and the directory tree between them never
        # gets walked — the parent/child structure is present in the graph but
        # invisible in any bounded view of it.
        for _source, target, edge_map in _hierarchy_first(graph.out_edges(current)):
            matching = [
                data
                for data in edge_map.values()
                if (not allowed or data.get("type") in allowed)
                and not (exclude_edge_types and data.get("type") in exclude_edge_types)
            ]
            if not matching:
                continue
            # Types the caller hides are pruned here rather than after the
            # fetch: a concept-heavy graph otherwise spends the entire budget
            # on nodes the view is about to discard, and the structure the
            # caller actually asked for never fits.
            if exclude_node_types:
                target_type = graph.nodes.get(target, {}).get("type")
                if target_type in exclude_node_types:
                    continue
            next_depth = current_depth + 1
            # One entry per *node*, not per edge into it. `visited` guarded the
            # queue but not the append, so a node reachable by two paths was
            # listed twice — every consumer treats this as a node list, and
            # `graph_slice` built its `nodes` payload straight off it, so the
            # canvas received duplicate element ids. It also charged the same
            # node to the budget twice, cutting a bounded slice short of the
            # structure it was asked for. First sighting wins, which is BFS
            # order and therefore the shortest path — the same rule `depths`
            # applies. No edge is lost: `graph_slice` derives links from the
            # graph itself, so both parents still draw.
            if target in visited:
                continue
            visited.add(target)
            edge_type_values = sorted({data.get("type") for data in matching if data.get("type")})
            neighbors.append(
                {
                    "node_id": target,
                    "depth": next_depth,
                    "edge_types": edge_type_values,
                    "path": [*path, target],
                    "node": dict(graph.nodes.get(target, {})),
                }
            )
            queue.append((target, next_depth, [*path, target]))
            # A single hub can have thousands of out-edges, so the budget has
            # to bind inside the fan-out, not just between hops.
            if max_nodes is not None and len(neighbors) >= max_nodes:
                break
    return {"node_id": node_id, "depth": depth, "neighbors": neighbors}


def _shortest_path(
    graph: SimpleMultiDiGraph,
    source: str,
    target: str,
    *,
    max_depth: int = 8,
    max_visited: int = 200_000,
) -> list[str] | None:
    """Fewest hops between two nodes, or None.

    Edges are followed in **both** directions on purpose. "How are these two
    related?" is a question about connectivity, not about which way an import
    happens to point — a file and the concept it mentions are related whether
    you walk `mentions` forwards or backwards, and a direction-respecting
    search reports "no path" for pairs a human would call obviously connected.

    Bidirectional BFS: two frontiers meeting in the middle explore
    O(b^(d/2)) instead of O(b^d), which is the difference between a usable
    answer and a graph walk on a hub-heavy index. ``max_visited`` bounds the
    work on a miss so a bad pair cannot pin the server.
    """
    if source == target:
        return [source]
    with graph.reading():
        adjacency: dict[str, set[str]] = {}
        for (edge_source, edge_target), _edge_map in graph.iter_edges():
            adjacency.setdefault(edge_source, set()).add(edge_target)
            adjacency.setdefault(edge_target, set()).add(edge_source)

    # Parent maps double as visited sets; walking them back at the meeting
    # point is what reconstructs the path.
    forward: dict[str, str | None] = {source: None}
    backward: dict[str, str | None] = {target: None}
    front, back = [source], [target]
    for depth in range(max_depth):
        if not front or not back:
            return None
        if len(forward) + len(backward) > max_visited:
            return None
        # Always expand the smaller frontier: it is what keeps the
        # bidirectional saving rather than degenerating to one deep search.
        expand_forward = len(front) <= len(back)
        frontier, seen, other = (
            (front, forward, backward) if expand_forward else (back, backward, forward)
        )
        next_frontier: list[str] = []
        for node in frontier:
            for neighbour in adjacency.get(node, ()):
                if neighbour in seen:
                    continue
                seen[neighbour] = node
                if neighbour in other:
                    return _join_paths(neighbour, forward, backward)
                next_frontier.append(neighbour)
        if expand_forward:
            front = next_frontier
        else:
            back = next_frontier
        if depth + 1 >= max_depth:
            break
    return None


def _join_paths(
    meeting: str,
    forward: dict[str, str | None],
    backward: dict[str, str | None],
) -> list[str]:
    head: list[str] = []
    cursor: str | None = meeting
    while cursor is not None:
        head.append(cursor)
        cursor = forward.get(cursor)
    head.reverse()
    tail: list[str] = []
    cursor = backward.get(meeting)
    while cursor is not None:
        tail.append(cursor)
        cursor = backward.get(cursor)
    return head + tail


def _edge_type_set(value: str | None) -> set[str] | None:
    items = {item.strip() for item in (value or "").split(",") if item.strip()}
    return items or None


def _hierarchy_first(out_edges: list) -> list:
    """Structural edges before shortcuts, order otherwise preserved."""

    def rank(entry) -> int:
        types = {data.get("type") for data in entry[2].values()}
        return 0 if types & set(HIERARCHY_EDGE_TYPES) else 1

    return sorted(out_edges, key=rank)


def graph_slice(
    graph: SimpleMultiDiGraph,
    node_id: str,
    depth: int = 1,
    edge_types: list[str] | None = None,
    limit: int = 100,
    exclude_edge_types: set[str] | None = None,
    exclude_node_types: set[str] | None = None,
) -> dict:
    """Connected sub-graph around a node (mirrors PheasantTools.get_graph_slice)."""
    # Ask for one more neighbour than the caller will receive.  Without that
    # sentinel a bounded UI slice silently looked complete whenever it filled
    # its budget, which made large documents appear to have lost chunks.
    neighbour_limit = max(0, int(limit))
    traversal = graph_neighbors(
        graph,
        node_id,
        depth,
        edge_types,
        max_nodes=neighbour_limit + 1,
        exclude_edge_types=exclude_edge_types,
        exclude_node_types=exclude_node_types,
    )
    all_neighbors = traversal["neighbors"]
    kept = all_neighbors[:neighbour_limit]
    node_ids = [node_id] + [item["node_id"] for item in kept]
    node_set = set(node_ids)
    # Hop distance per node, nearest wins (BFS order, so the first sighting is
    # the shortest path). The UI rings the canvas by this and lets the user
    # widen the horizon a layer at a time instead of rendering the whole graph.
    depths: dict[str, int] = {node_id: 0}
    for item in kept:
        target = str(item["node_id"])
        hop = int(item.get("depth") or 0)
        if hop < depths.get(target, hop + 1):
            depths[target] = hop
    links = []
    allowed = set(edge_types or [])
    for source in node_set:
        if source not in graph:
            continue
        for _src, target, edge_map in graph.out_edges(source):
            if target not in node_set:
                continue
            for key, data in edge_map.items():
                if allowed and data.get("type") not in allowed:
                    continue
                links.append({"source": source, "target": target, "key": key, **data})
    return {
        "node_id": node_id,
        "depth": depth,
        "nodes": [dict(attrs) for attrs in (graph.nodes.get(item) for item in node_ids) if attrs],
        "links": links,
        "depths": depths,
        "truncated": len(all_neighbors) > neighbour_limit,
    }


def create_app(
    config: PheasantConfig | None = None,
    config_path: str | Path | None = None,
    role: str | None = None,
) -> FastAPI:
    resolved_config_path = (
        str(config_path)
        if config_path
        else os.environ.get("PHEASANT_CONFIG", "/config/pheasant.yaml")
    )
    if config is None:
        config = load_config(resolved_config_path)
    # Phase 35.6: which jobs this process takes on. `all` is the default and
    # is byte-identical to pre-roles behavior; the one that changes anything
    # here is `api`, which publishes index work instead of running it.
    role_policy = resolve_role(config, role)
    validate_role(role_policy, config)
    paths = StatePaths.from_config(config)
    paths.ensure()
    state = StateStore.from_config(config, paths.sqlite)
    state.migrate()
    SourceRegistry(config, state).initialize()
    engine = SyncEngine(config, paths, state)
    search = HybridSearch(
        SearchStore(state),
        vector=engine.vector_searcher(),
        node_index=engine.node_index,
        wasm_relationship_search=config.search.wasm_relationship_search,
        steering_enabled=config.memory.steering_enabled,
        default_memory_policy=config.memory.default_policy,
        usage_tracking=config.memory.usage_tracking,
    )

    # Built before the lifespan so the lifespan closure can start its session
    # manager: FastMCP's streamable-http app is useless without its own
    # lifespan running ("Task group is not initialized"), and a plain
    # app.mount() does not run a sub-app's lifespan.
    mcp_asgi_app = _mcp_asgi_app(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio

        # Drain-on-SIGTERM is installed *here*, not before `uvicorn.run()`.
        # uvicorn installs its own graceful-shutdown handler inside `run()`,
        # so a handler installed earlier is simply replaced and never fires.
        # Lifespan startup runs after `capture_signals()`, which is what lets
        # this one chain to uvicorn's rather than clobber it.
        drain_seconds = float(getattr(config.server.api, "drain_seconds", 0) or 0)
        if drain_seconds > 0:
            from pheasant.deployment.serving import install_drain_handler

            try:
                install_drain_handler(app.state.drain, drain_seconds)
            except ValueError:  # pragma: no cover - not the main thread (TestClient)
                logger.debug("drain handler not installed: not running in the main thread")

        startup_sources = [
            source.name for source in config.sources if source.enabled and source.sync.on_startup
        ]
        if startup_sources and role_policy.indexes_locally:
            loop = asyncio.get_running_loop()

            def _run_startup() -> None:
                from pheasant.sync.worker import WorkerBackedEngine

                logger.info("Running startup sync for sources: %s", ", ".join(startup_sources))
                # Indexing happens in a child process so it cannot starve the
                # requests this server exists to answer.
                results = WorkerBackedEngine(engine, app.state.config_path).startup()
                app.state.startup_sync_results = results
                indexed = sum(result.indexed_artifacts for result in results)
                skipped = sum(result.skipped_artifacts for result in results)
                logger.info(
                    "Startup sync complete: sources=%s indexed=%s skipped=%s",
                    len(results),
                    indexed,
                    skipped,
                )

            loop.run_in_executor(None, _run_startup)

        # Warm the agent framework off the request path. Importing langgraph
        # costs ~4s, and it used to be paid, lazily, by whoever asked the first
        # question after a restart — which read as "the planner is slow" when
        # nothing was planning yet. Best-effort and in the background: a
        # missing [agent] extra just means the simple workflow answers.
        def _warm_workflows() -> None:
            from pheasant.assistant.workflows.agentic import warm

            if warm():
                logger.info("Agent workflow ready (langgraph imported and graph compiled)")

        threading.Thread(target=_warm_workflows, name="pheasant-warm", daemon=True).start()
        try:
            if mcp_asgi_app is None:
                yield
            else:
                async with mcp_asgi_app.router.lifespan_context(app):
                    yield
        finally:
            held = getattr(app.state, "metrics_queue", None)
            if held is not None:
                app.state.metrics_queue = None
                try:
                    held.close()
                except Exception:  # pragma: no cover - shutdown must not raise
                    logger.debug("could not close the metrics queue handle", exc_info=True)

    app = FastAPI(title="pheasant", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.state = state
    app.state.engine = engine
    app.state.config_path = resolved_config_path
    # Chat API keys a user pastes in the browser live here and nowhere else:
    # process memory, TTL'd, gone on restart.
    app.state.session_keys = SessionKeyStore(config.assistant.session_key_ttl_minutes)
    # Background work (`wait=false` on quick-add/register/sync/upload) is
    # tracked in one job registry: in-memory, process lifetime, with a phase,
    # a counter and a terminal outcome per job. The `syncing_sources` /
    # `sync_outcomes` dicts this replaced could only say "true" for however
    # many minutes a first index took, which is indistinguishable from a hang.
    app.state.sync_lock = threading.Lock()
    app.state.metrics_queue = None
    app.state.metrics_queue_lock = threading.Lock()
    app.state.jobs = JobRegistry()
    jobs = app.state.jobs
    # Content-addressed results for `/internal/indexing/prepare-batch`, so a
    # coordinator's retry after a timeout is a lookup rather than a second
    # parse. Bounded and LRU: a worker serving a 50k-file source must not
    # accumulate 50k parse results.
    app.state.prepare_cache = ResultCache(PREPARE_CACHE_SIZE)
    metrics.register_default_metrics(__version__)

    # The web UI is a separate workload that talks to this API over HTTP, so the
    # browser origin differs in development — but this API is unauthenticated,
    # so a wildcard here means any page in the user's browser can drive every
    # route on it (read the whole index, rewrite the config, register a source
    # over a sensitive directory). Allow the UI's origins, not the web.
    cors_settings = config.server.api
    if cors_settings.cors_allow_all_origins:
        logger.warning(
            "server.api.cors_allow_all_origins is on: every browser origin may call this "
            "unauthenticated API. Only do this behind an authenticating ingress."
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if cors_settings.cors_allow_all_origins else cors_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Phase 35.6: shed rather than queue, and drain before dying. Both are
    # replica behaviors — the limiter is off by default because with one
    # process there is nowhere for a shed request to go, so waiting is the
    # best available answer.
    app.state.limiter = ConcurrencyLimiter(cors_settings.max_concurrent_requests)
    app.state.drain = DrainState()
    limiter: ConcurrencyLimiter = app.state.limiter
    drain_state: DrainState = app.state.drain

    @app.middleware("http")
    async def bound_concurrency(request, call_next):  # type: ignore[no-untyped-def]
        """Admit or refuse immediately; never block.

        Blocking is the behavior being replaced. A fast 429 is what lets a
        load balancer try another replica while the client is still waiting;
        a slow one is just a timeout with extra steps.
        """

        path = request.url.path
        if not limiter.acquire(path):
            metrics.REGISTRY.inc("pheasant_requests_shed_total", path=_metric_path(path))
            return JSONResponse(
                status_code=429,
                content={
                    "detail": (
                        f"this replica is at its {limiter.limit}-request concurrency limit; "
                        "retry, ideally against another replica"
                    )
                },
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            )
        try:
            return await call_next(request)
        finally:
            limiter.release(path)

    def _metric_path(path: str) -> str:
        """Collapse to the first segment: a label per artifact id is a leak.

        Prometheus cardinality is the failure mode here — `/nodes/content` is
        a useful label, `/nodes/content?id=<one of 600k>` is a memory leak in
        the scrape target.
        """

        head = path.strip("/").split("/", 1)[0]
        return f"/{head}" if head else "/"

    def audit(source_id: str | None, action: str, details: dict | None = None) -> None:
        created_at = utc_now()
        ordinal = len(state.list_source_audit_events(source_id, limit=10000))
        import hashlib
        import json

        digest = hashlib.sha256(
            json.dumps(
                {"source_id": source_id, "action": action, "created_at": created_at},
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:16]
        state.append_source_audit_event(
            f"audit:{digest}:{ordinal}",
            source_id,
            action,
            "ui",
            "http",
            None,
            created_at,
            details,
        )

    @app.get("/health")
    def health() -> dict:
        """Liveness: is this process running at all.

        Carries the role so a pod can be identified from a probe response —
        "which of these five containers is the indexer" is otherwise a
        question you answer by reading manifests.
        """

        return {"status": "ok", "service": "pheasant", "role": role_policy.name}

    @app.get("/ready")
    def ready() -> dict:
        """Readiness: can this process do its role's job.

        Distinct from `/health` on purpose, because Kubernetes uses them for
        different things: a failing liveness probe restarts the pod, a failing
        readiness probe takes it out of the Service. A process whose state
        store has gone away should stop receiving traffic, not be restarted
        into the same problem.

        Deliberately **not** gated on the index being populated. An empty
        knowledge base still answers searches correctly (with nothing), and a
        replica that stayed unready through a multi-hour first index would
        take the whole Service down for that time — which is the opposite of
        what the readiness probe is for.
        """

        payload: dict[str, object] = {
            "status": "ready",
            "knowledge_base": config.knowledge_base_id,
            **describe_role(role_policy),
        }
        if drain_state.draining:
            # SIGTERM has arrived. Reporting not-ready *before* the process
            # stops accepting work is the entire drain mechanism: Kubernetes
            # removes endpoints and sends SIGTERM concurrently, and endpoint
            # propagation is not instant, so a process that exits promptly
            # drops whatever was routed to it in the gap.
            payload["status"] = "draining"
            payload["draining_for_seconds"] = round(drain_state.draining_for, 1)
            return JSONResponse(status_code=503, content=payload)  # type: ignore[return-value]
        try:
            state.rows("SELECT 1 AS ok", ())
        except Exception as exc:  # noqa: BLE001 - any failure means not ready
            logger.warning("readiness probe failed: %s", exc)
            payload["status"] = "not_ready"
            payload["reason"] = "state store unreachable"
            return JSONResponse(status_code=503, content=payload)  # type: ignore[return-value]
        return payload

    def _authorize_worker(authorization: str | None) -> None:
        """Gate + constant-time token check shared by both worker routes."""

        concurrency = config.sync.concurrency
        if not concurrency.remote_worker_enabled:
            raise HTTPException(status_code=404, detail="remote indexing worker is disabled")
        import hmac

        from pheasant.sync.remote_worker import RemoteWorkerError, configured_token

        try:
            expected = configured_token(concurrency.remote_worker_token_env)
        except RemoteWorkerError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid indexing worker token")

    def _check_task_size(payload: dict) -> None:
        max_mb = config.sync.limits.max_file_size_mb
        encoded = str(payload.get("content_base64") or "")
        if max_mb is not None and len(encoded) > int(max_mb) * 1024 * 1024 * 4 // 3 + 4:
            raise HTTPException(status_code=413, detail="indexing task exceeds max_file_size_mb")

    @app.post("/internal/indexing/prepare")
    def remote_prepare(
        req: RemotePrepareRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Stateless authenticated parse/chunk worker for remote coordinators."""

        from pheasant.sync.remote_worker import RemoteWorkerError, prepare_task

        _authorize_worker(authorization)
        _check_task_size(req.payload)
        try:
            parsed = prepare_task(req.model_dump())
        except (KeyError, TypeError, ValueError, RemoteWorkerError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"parsed": parsed}

    @app.post("/internal/indexing/prepare-batch")
    def remote_prepare_batch(
        req: RemotePrepareBatchRequest,
        authorization: Annotated[str | None, Header()] = None,
        x_pheasant_deadline_seconds: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Prepare several tasks in one request (Phase 35.5).

        The value is not the round trips saved — though on a large source that
        is most of the transport cost. It is that a batch carries the caller's
        remaining deadline and a content address per task, so a worker can
        decline work whose caller has given up and answer a duplicate from
        cache. Those two together are what make at-least-once retry cheap
        enough to actually do.
        """

        import time

        from pheasant.sync.remote_worker import (
            DeadlineExceeded,
            RemoteWorkerError,
            prepare_batch_tasks,
        )

        _authorize_worker(authorization)
        if not req.tasks:
            return {"results": []}
        if len(req.tasks) > MAX_PREPARE_BATCH:
            raise HTTPException(
                status_code=413,
                detail=f"batch of {len(req.tasks)} exceeds the {MAX_PREPARE_BATCH}-task limit",
            )
        for task in req.tasks:
            _check_task_size(task.payload)

        budget = req.deadline_seconds
        if budget is None:
            try:
                budget = float(x_pheasant_deadline_seconds or "")
            except ValueError:
                budget = None
        if budget is not None and budget <= 0:
            raise HTTPException(status_code=408, detail="caller deadline already passed")
        started = time.monotonic()

        def remaining() -> float | None:
            return None if budget is None else budget - (time.monotonic() - started)

        try:
            results = prepare_batch_tasks(
                [task.model_dump() for task in req.tasks],
                list(req.idempotency_keys or []),
                cache=app.state.prepare_cache,
                deadline=remaining,
            )
        except DeadlineExceeded as exc:
            raise HTTPException(status_code=408, detail=str(exc)) from exc
        except (KeyError, TypeError, ValueError, RemoteWorkerError) as exc:
            # One bad task fails the batch: the coordinator's fallback is to
            # prepare locally, which handles every task the remote path
            # refuses, so isolating the offender would buy nothing.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        cache = app.state.prepare_cache
        return {
            "results": results,
            "cache": {"hits": cache.hits, "misses": cache.misses, "size": len(cache)},
        }

    def _metrics_queue():
        """The scrape's queue handle, created once and reused.

        Under a lock: two concurrent scrapes would otherwise each build one
        and the loser's handle would leak — on `nats` that is a live
        connection nothing ever closes.
        """

        with app.state.metrics_queue_lock:
            queue = getattr(app.state, "metrics_queue", None)
            if queue is None:
                from pheasant.sync.queue import queue_from_config

                queue = queue_from_config(config, state)
                app.state.metrics_queue = queue
            return queue

    @app.get("/metrics")
    def metrics_endpoint() -> PlainTextResponse:
        """Prometheus exposition text (Phase 35.1).

        Named ``metrics_endpoint``, not ``metrics``: every route in this module
        is a closure over ``create_app``, so a local named ``metrics`` would
        shadow the imported :mod:`pheasant.telemetry.metrics` for *every other
        route in the file* — the search handler would raise ``AttributeError``
        on ``metrics.REGISTRY`` and only at request time.

        Graph size and job state are sampled here rather than tracked
        incrementally: both are cheap to read, and a gauge updated from a
        write path drifts the moment any path forgets to update it.
        """

        graph = engine.graph_builder.graph
        sample: dict[str, object] = {
            "pheasant_graph_nodes": graph.number_of_nodes(),
            "pheasant_graph_edges": graph.number_of_edges(),
            "pheasant_requests_inflight": limiter.inflight,
            "pheasant_draining": 1.0 if drain_state.draining else 0.0,
        }
        sample.update(jobs.metrics_sample())
        # With the durable queue on, the backlog outlives this process, so
        # the in-memory job registry is no longer the whole truth — an HPA
        # reading only it would see zero while a restarted fleet's rows sit
        # waiting. The queue's own depth wins where it exists (Phase 35.5).
        try:
            from pheasant.sync.queue import DEAD, INFLIGHT, PENDING

            # Held open across scrapes rather than built and closed per
            # request. Prometheus scrapes every 15s by default, per replica,
            # and on the `nats` backend building one meant a full TCP +
            # JetStream connect and teardown each time — a broker connection
            # storm proportional to fleet size, paid to read three integers.
            # Closed by the lifespan's `finally`.
            queue = _metrics_queue()
            if queue is not None:
                depth = queue.depth()
                sample["pheasant_index_queue_depth"] = depth.get(PENDING, 0)
                sample["pheasant_index_inflight"] = depth.get(INFLIGHT, 0)
                sample["pheasant_index_dead_letters"] = depth.get(DEAD, 0)
        except Exception:  # pragma: no cover - a scrape must never 500
            logger.debug("could not sample index queue depth", exc_info=True)
        return PlainTextResponse(
            metrics.render_with(sample),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/contract")
    def contract() -> dict:
        """Return this region's published semantic contract (Synapse 21.5).

        404 until a contract has been published (a sync with
        ``synapse.publish`` on). Standalone regions that never enable
        publishing simply always 404 here.
        """
        from pheasant.synapse.publisher import load_contract_text

        text = load_contract_text(config.pheasant.state_path)
        if text is None:
            raise HTTPException(status_code=404, detail="No contract published yet")
        import json as _json

        return _json.loads(text)

    @app.get("/knowledge-bases")
    def knowledge_bases() -> dict:
        return {"knowledge_bases": KnowledgeBaseRegistry(state).list()}

    def _with_sync_state(records: list[dict]) -> list[dict]:
        """Overlay live job state onto persisted source rows.

        Every route that lists sources carries this, so a client polling any
        of them sees what is running and — via `sync_error` — why the last
        background pass failed. `syncing` alone flickers to nothing the
        instant a job completes, which is exactly when a caller most wants to
        know whether it succeeded. `job` adds the phase and counter behind
        that boolean, which is what turns a spinner into a progress bar.

        `progress` (Phase 35.1) narrows that to *this* source's slice of the
        job. `job` is the whole run, so under `sync_all` every source in the
        list showed the same aggregate counter and the one source that was
        stuck looked exactly like the seven that were fine.
        """
        for record in records:
            name = record.get("name")
            if not name:
                continue
            active = jobs.active_for(name)
            last = jobs.last_outcome_for(name)
            record["syncing"] = active is not None
            record["sync_error"] = last.error if last else None
            record["job"] = active.as_dict() if active else None
            record["progress"] = jobs.source_progress(name)
        return records

    @app.get("/sources")
    def sources() -> list[dict]:
        return _with_sync_state(SourceRegistry(config, state).list_sources())

    @app.get("/sources/types")
    def source_types() -> dict:
        """Every source type this deployment can register, built-in or plugin.

        The form that creates sources has to offer exactly what YAML accepts,
        which on a machine with connector plugins installed is more than the
        built-in enum. ``path_role`` tells a caller whether the schema's
        mandatory ``path`` is real for that type or just a placeholder the
        connector never reads.
        """
        from pheasant.sync.connector_registry import list_connector_types

        types = [
            {
                "id": type_id,
                "label": label,
                "description": description,
                "path_role": path_role,
                "builtin": True,
            }
            for type_id, label, description, path_role in BUILTIN_SOURCE_TYPES
        ]
        types.extend(
            {
                "id": name,
                "label": name,
                "description": "Connector plugin (pheasant.connectors entry point).",
                # A plugin pulls from its own service; the path field is
                # schema ceremony unless the plugin documents otherwise.
                "path_role": "unused",
                "builtin": False,
            }
            for name in list_connector_types()
        )
        return {"types": types, "placeholder_path": PLUGIN_PLACEHOLDER_PATH}

    @app.post("/sources")
    def register_source(req: RegisterSourceRequest) -> dict:
        is_builtin = _check_source_type(req.type)
        # A plugin connector reads from its own service, not the filesystem,
        # so the schema's mandatory path is a placeholder — don't make the
        # caller invent a real directory to register a Notion or Slack
        # source. A path that *is* supplied still goes through path policy.
        if not is_builtin and req.path.strip() in {"", PLUGIN_PLACEHOLDER_PATH}:
            resolved = Path(PLUGIN_PLACEHOLDER_PATH)
        else:
            try:
                resolved = _resolve_source_path(req.path, config)
            except PathPolicyError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            source = _source_from_payload(_source_payload(req, resolved))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid source: {exc}") from exc
        SourceRegistry(config, state).register_source(source)
        config.sources = [s for s in config.sources if s.name != source.name]
        config.sources.append(source)
        audit(source.name, "register_source", {"source": source.model_dump(mode="json")})
        result = None
        syncing = False
        job_id = None
        queued: list[str] = []
        if req.sync_now:
            if req.wait:
                try:
                    result = engine.sync_source(source.name, req.sync_mode).__dict__
                except (KeyError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            else:
                job_id, queued = _start_background_sync(source.name, req.sync_mode)
                syncing = True
        return {
            "status": "registered",
            "knowledge_base": config.knowledge_base_id,
            "source": source.model_dump(mode="json"),
            "sync_result": result,
            "syncing": syncing,
            "job_id": job_id,
            "queued_tasks": queued,
            "config_update_required": True,
        }

    @app.put("/sources/{source_id}")
    def update_source(source_id: str, req: UpdateSourceRequest) -> dict:
        source = next((s for s in config.sources if s.name == source_id), None)
        if source is None:
            row = state.get_source(source_id)
            if not row:
                raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
            import json

            source = _source_from_payload(json.loads(row["config_json"]))
        # An edit that doesn't touch the type must not re-validate it: a
        # plugin that got uninstalled shouldn't lock its source out of the
        # form. Only a type the caller actually sends is checked.
        is_builtin = (
            _check_source_type(req.type)
            if req.type is not None
            else str(source.type) in {member.value for member in SourceType}
        )
        if not is_builtin and (req.path or "").strip() in {"", PLUGIN_PLACEHOLDER_PATH}:
            resolved = Path(PLUGIN_PLACEHOLDER_PATH)
        else:
            try:
                resolved = _resolve_source_path(req.path, config) if req.path else source.path
            except PathPolicyError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            updated = _source_from_payload(_source_payload(req, resolved, existing=source))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid source: {exc}") from exc
        engine.graph_builder.remove_source_content(source_id)
        engine.manifests.delete(source_id)
        state.delete_source_artifacts(source_id)
        SourceRegistry(config, state).register_source(updated)
        config.sources = [s for s in config.sources if s.name != source_id]
        config.sources.append(updated)
        engine.graph_store.save(config.knowledge_base_id, engine.graph_builder.graph)
        audit(source_id, "update_source", {"source": updated.model_dump(mode="json")})
        return {
            "status": "updated",
            "source": updated.model_dump(mode="json"),
            "config_update_required": True,
        }

    @app.post("/sources/{source_id}/disable")
    def disable_source(source_id: str) -> dict:
        if not state.get_source(source_id):
            raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
        state.set_source_enabled(source_id, False, "disabled")
        for source in config.sources:
            if source.name == source_id:
                source.enabled = False
        audit(source_id, "disable_source")
        return {"status": "disabled", "source_name": source_id}

    @app.delete("/sources/{source_id}")
    def remove_source(source_id: str) -> dict:
        if not state.get_source(source_id):
            raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
        engine.graph_builder.remove_source_content(source_id)
        engine.graph_store.save(config.knowledge_base_id, engine.graph_builder.graph)
        engine.manifests.delete(source_id)
        state.delete_source(source_id)
        config.sources = [s for s in config.sources if s.name != source_id]
        audit(source_id, "remove_source")
        return {"status": "removed", "source_name": source_id}

    @app.post("/sources/{source_id}/promote")
    def promote_source(source_id: str, req: PromoteSourceRequest | None = None) -> dict:
        req = req or PromoteSourceRequest()
        source = next((s for s in config.sources if s.name == source_id), None)
        if source is None:
            row = state.get_source(source_id)
            if not row:
                raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
            import json

            payload = json.loads(row["config_json"])
            source = PheasantConfig.model_validate({"sources": [payload]}).sources[0]
        source_payload = source.model_dump(mode="json")
        yaml_patch = dump_config_yaml({"sources": [source_payload]})
        wrote = False
        # `config_path` is caller-supplied: constrain it to this server's own
        # config (or an allowed root) so promotion cannot be used to write a
        # YAML file anywhere the process can reach.
        try:
            path = resolve_config_write_target(
                req.config_path,
                server_config_path=app.state.config_path,
                allowed_roots=_config_write_roots(config),
            )
        except PathPolicyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        target = str(path)
        if req.write:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(data, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"Refusing to overwrite {path}: it is not a pheasant config mapping",
                )
            existing = [
                item for item in data.get("sources", []) or [] if item.get("name") != source.name
            ]
            data["sources"] = [*existing, source_payload]
            path.write_text(dump_config_yaml(data), encoding="utf-8")
            wrote = True
        audit(source_id, "promote_runtime_source_to_config", {"write": wrote, "path": target})
        return {
            "status": "promoted" if wrote else "patch_generated",
            "source_name": source_id,
            "yaml_patch": yaml_patch,
            "wrote_config": wrote,
            "config_path": target,
        }

    @app.get("/sources/{source_id}/repo-map")
    def repo_map(source_id: str) -> dict:
        rows = state.rows(
            "SELECT relative_path,type,size_bytes FROM artifacts "
            "WHERE source_id=? ORDER BY relative_path",
            (source_id,),
        )
        return {"source_name": source_id, "files": [dict(row) for row in rows]}

    @app.get("/sources/{source_id}/history")
    def source_history(source_id: str, limit: int = 100, offset: int = 0) -> dict:
        return {
            "events": state.list_source_audit_events(source_id, limit, offset),
            "pagination": {"limit": limit, "offset": offset},
        }

    def _index(
        source_name: str | None,
        mode: str,
        depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        """Index in a worker process, then pick up the result.

        The server deliberately does not index in-process: that work is
        CPU-bound Python and, under the GIL, it starves the very requests this
        API exists to answer. See :mod:`pheasant.sync.worker`.

        A role that does not index refuses here rather than silently doing it
        anyway. `wait: true` asks this process to run a sync and return the
        result, and an api replica cannot honour that — it can only publish,
        which is a different promise. Saying so with a 409 and the fix is
        better than either lying or quietly changing the contract.
        """

        if not role_policy.indexes_locally:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"role '{role_policy.name}' does not index; retry with wait=false to "
                    "queue this sync for an indexer to run"
                ),
            )

        from pheasant.sync.worker import WorkerBackedEngine

        worker = WorkerBackedEngine(engine, app.state.config_path)
        if source_name:
            results = [worker.sync_source(source_name, mode, max_depth=depth, full_scan=full_scan)]
        else:
            results = worker.sync_all(mode, max_depth=depth, full_scan=full_scan)
        failed = [r for r in results if r.status in {"failed", "timeout"}]
        if failed:
            raise HTTPException(
                status_code=500,
                detail=failed[0].details.get("error") or f"sync {failed[0].status}",
            )
        return {"results": [r.__dict__ for r in results]}

    def _run_background_sync(source_name: str | None, mode: str, job_id: str) -> None:
        """The `wait=false` path: same worker-subprocess sync as `_index`, off
        the request thread, reporting into the job registry as it goes.

        The job is guaranteed to reach a terminal state on every exit path —
        success, sync failure, or an unexpected exception — so a source can
        never get stuck showing "syncing" forever.
        """
        from pheasant.sync.worker import WorkerBackedEngine

        def forward(event: dict) -> None:
            meta = event.get("meta") or {}
            jobs.progress(
                job_id,
                phase=event.get("phase"),
                current=event.get("current"),
                total=event.get("total"),
                detail=event.get("detail"),
                source=meta.get("source") or None,
                stats=meta,
            )

        try:
            jobs.progress(
                job_id,
                phase="waiting_for_indexer",
                detail="Queued for the index writer",
            )
            worker = WorkerBackedEngine(engine, app.state.config_path)
            if source_name:
                results = [worker.sync_source(source_name, mode, on_progress=forward)]
            else:
                results = worker.sync_all(mode, on_progress=forward)
        except Exception as exc:  # background thread — never propagate, just record
            logger.exception("background sync failed")
            jobs.finish(job_id, "failed", error=str(exc))
            return
        failed = [r for r in results if r.status in {"failed", "timeout", "limit_exceeded"}]
        jobs.finish(
            job_id,
            "failed" if failed else "succeeded",
            error=(failed[0].details.get("error") or failed[0].status) if failed else None,
            result={
                "results": [
                    {
                        "source_id": r.source_id,
                        "indexed_artifacts": r.indexed_artifacts,
                        "skipped_artifacts": r.skipped_artifacts,
                        "status": r.status,
                    }
                    for r in results
                ]
            },
        )

    def _background_status(job_id: str | None, queued: list[str]) -> str:
        """Three outcomes, three words — `syncing` used to cover all of them.

        `queued` is not `syncing`: nothing is indexing yet, an indexer has to
        claim the task first, and there is no local job to watch.
        """

        if job_id:
            return "syncing"
        return "queued" if queued else "already_syncing"

    def _start_background_sync(source_name: str | None, mode: str) -> tuple[str | None, list[str]]:
        """Start a background sync. Returns ``(job_id, queued_task_ids)``.

        ``job_id`` is None when one is already running over the same source,
        **and** when this process does not index: an ``api`` replica publishes
        to the queue, and a queue task is not a job. Returning its id in the
        ``job_id`` field made every caller poll ``GET /jobs/<task id>``, which
        404s — the registry is in-process and the task belongs to an indexer
        in another pod. The ids are reported separately, under their own name,
        so the response says what actually happened instead of implying
        progress the api role cannot observe.

        Refusing the overlap matters: found live, a source with
        `sync.on_startup: true` and no checkpoint yet (so every attempt does a
        full, expensive pass) got resynced by both a container-startup trigger
        and a manual `wait: false` trigger landing close together — two
        concurrent embedding-heavy syncs over the same source, which tripped
        the embeddings endpoint's rate limit and multiplied disk and CPU work
        for no benefit. The check-and-claim happens under one lock acquisition
        here, not inside the thread, so it is atomic against a second caller
        racing in before the first thread has even started.
        """
        names = [source_name] if source_name else [s.name for s in config.sources if s.enabled]
        if not role_policy.indexes_locally:
            return None, _publish_background_sync(names, mode)
        with app.state.sync_lock:
            to_start = [name for name in names if jobs.active_for(name) is None]
            if not to_start:
                return None, []
            job = jobs.create(
                "sync",
                f"Indexing {source_name}" if source_name else "Indexing all sources",
                to_start,
            )
        threading.Thread(
            target=_run_background_sync,
            args=(source_name, mode, job.id),
            name=f"pheasant-bgsync-{source_name or 'all'}",
            daemon=True,
        ).start()
        return job.id, []

    def _publish_background_sync(names: list[str], mode: str) -> list[str]:
        """Hand the work to an indexer instead of running it here.

        This is what the ``api`` role *is*. Three api replicas that each
        indexed on request would put three processes on one source; publishing
        means whichever indexer is free picks it up, exactly once, and the
        caller still gets an id to poll.

        Task ids are content-addressed on (knowledge base, source, mode) —
        the same rule ``sync_all`` uses — so two replicas answering the same
        user's double-click enqueue one task, not two.
        """

        import hashlib

        from pheasant.sync.queue import IndexTask, queue_from_config

        queue = queue_from_config(config, state)
        if queue is None:  # pragma: no cover - validate_role refuses this at startup
            raise HTTPException(
                status_code=503,
                detail="this process does not index and no queue is configured",
            )
        published: list[str] = []
        try:
            for name in names:
                digest = hashlib.sha256(
                    f"{config.knowledge_base_id}\0{name}\0{mode}".encode()
                ).hexdigest()[:24]
                task = IndexTask(
                    id=f"idx-{digest}",
                    source_id=name,
                    mode=str(mode),
                    payload={"max_depth": None, "full_scan": False},
                    max_attempts=max(1, int(config.sync.queue.max_attempts or 1)),
                )
                try:
                    queue.publish(task)
                    published.append(task.id)
                except Exception as exc:  # noqa: BLE001 - already queued is not an error
                    logger.debug("Index task for %s already queued (%s)", name, exc)
                    published.append(task.id)
        finally:
            queue.close()
        return published

    @app.post("/sync")
    def sync_all(req: SyncRequest) -> dict:
        if not req.wait:
            job_id, queued = _start_background_sync(None, req.mode)
            return {
                "status": _background_status(job_id, queued),
                "job_id": job_id,
                "queued_tasks": queued,
                "sources": [s.name for s in config.sources if s.enabled],
            }
        try:
            return _index(req.source_name, req.mode, req.depth, req.full_scan)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/sync/{source_id}")
    def sync_source(source_id: str, req: SyncRequest | None = None) -> dict:
        mode = req.mode if req else "incremental"
        if req is not None and not req.wait:
            # A source registered at runtime (quick-add, POST /sources) lives
            # in the state registry, not necessarily yet in `config.sources`
            # — SyncEngine._source lazily pulls it in from there on first
            # sync. Validating against `config.sources` alone would 404 a
            # perfectly syncable source (e.g. right after a restart, before
            # anything has re-triggered that lazy load).
            known = any(s.name == source_id for s in config.sources) or bool(
                state.get_source(source_id)
            )
            if not known:
                raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
            job_id, queued = _start_background_sync(source_id, mode)
            return {
                "status": _background_status(job_id, queued),
                "job_id": job_id,
                "queued_tasks": queued,
                "source_id": source_id,
            }
        try:
            report = _index(
                source_id,
                mode,
                req.depth if req else None,
                req.full_scan if req else False,
            )
            # This route has always returned one result object, not a list.
            results = report.get("results") or []
            return results[0] if results else report
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/sources/upload")
    async def upload_documents(
        files: Annotated[list[UploadFile], File()],
        source_name: Annotated[str, Form()] = "uploads",
        sync_now: Annotated[bool, Form()] = True,
        wait: Annotated[bool, Form()] = False,
    ) -> dict:
        """Index documents dropped into the UI, with no filesystem setup.

        The files land in a directory under ``/state/uploads`` which is
        registered as an ordinary ``document_folder`` source — so they flow
        through the same connector → chunk → graph pipeline as everything
        else, get the same idempotent re-sync, and can be removed by deleting
        the source. There is deliberately no second ingestion path.

        Uploading again into the same source name adds to it rather than
        replacing it, which is what "drop a few more files in" should mean.
        """
        from pheasant.api.uploads import safe_filename, store_upload, upload_root

        if not files:
            raise HTTPException(status_code=400, detail="No files uploaded")
        name = safe_filename(source_name or "uploads", fallback="uploads")
        directory = upload_root(Path(config.pheasant.state_path), name)
        limits = config.sync.limits
        max_bytes = (limits.max_file_size_mb or 0) * 1024 * 1024 or None

        stored: list[dict] = []
        rejected: list[dict] = []
        for upload in files:
            data = await upload.read()
            try:
                record = store_upload(
                    directory,
                    upload.filename or "upload",
                    data,
                    max_bytes=max_bytes,
                )
            except ValueError as exc:
                # One bad file must not lose the good ones in the same drop.
                rejected.append({"filename": upload.filename, "error": str(exc)})
                continue
            stored.append(record.__dict__)
        if not stored:
            raise HTTPException(
                status_code=400,
                detail="; ".join(item["error"] for item in rejected) or "Nothing stored",
            )

        registry = SourceRegistry(config, state)
        existing = next((s for s in config.sources if s.name == name), None)
        if existing is None:
            source = _source_from_payload(
                {
                    "name": name,
                    "type": "document_folder",
                    "path": str(directory),
                    "description": f"Documents uploaded through the UI ({name})",
                    # Uploads are arbitrary documents, not a code tree: the
                    # default include list is code-shaped and would silently
                    # drop a dropped PDF or .docx.
                    "include": ["**/*"],
                }
            )
            registry.register_source(source)
            config.sources = [s for s in config.sources if s.name != name]
            config.sources.append(source)
        audit(name, "upload_documents", {"files": [item["filename"] for item in stored]})

        syncing = False
        job_id = None
        queued: list[str] = []
        sync_result = None
        if sync_now:
            if wait:
                try:
                    sync_result = engine.sync_source(name, "incremental").__dict__
                except (KeyError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            else:
                job_id, queued = _start_background_sync(name, "incremental")
                syncing = job_id is not None or bool(queued)
        return {
            "status": "stored",
            "source_name": name,
            "path": str(directory),
            "stored": stored,
            "rejected": rejected,
            "syncing": syncing,
            "job_id": job_id,
            "queued_tasks": queued,
            "sync_result": sync_result,
        }

    @app.get("/fs/host-path")
    def fs_host_path(path: str) -> dict:
        """Can pheasant see this path — and if not, exactly how to fix it.

        The most common first-run failure is a real host path that simply is
        not mounted into the container. Answering "does not exist" there is
        true and useless; this returns the bind mount, the ``docker run``
        flag and the ``allow_workspace_roots`` entry needed to make it work.
        """
        from pheasant.deployment.mounts import in_container, resolve_host_path

        report = resolve_host_path(path)
        report["in_container"] = in_container()
        report["allowed"] = False
        if report.get("container_path"):
            try:
                _resolve_source_path(report["container_path"], config)
                report["allowed"] = True
            except PathPolicyError as exc:
                report["policy_error"] = str(exc)
        return report

    @app.post("/sources/{source_id}/scan")
    def scan_source(source_id: str, depth: int | None = None) -> dict:
        """Estimate what a source would index, without indexing it.

        The pre-flight for the "point it at anything" workflow: file count,
        size, where the weight sits, how many files each depth cap admits,
        and whether the configured limits would refuse the sync.
        """
        try:
            return engine.scan_source(source_id, max_depth=depth)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/memory")
    def memory_write(req: MemoryWriteRequest) -> dict:
        from pheasant.memory.store import MemoryStore, memory_source

        source = memory_source(config, state)
        if source is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "no enabled memory source configured; add a `type: memory` "
                    "source to pheasant.yaml"
                ),
            )
        try:
            record, created = MemoryStore(source.path).append(
                req.text,
                scope=req.scope,
                subject=req.subject,
                supersedes=req.supersedes,
                tags=req.tags,
                kind=req.kind,
                written_by=req.principal,
                valid_until=req.valid_until,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            # A memory source pointed at somewhere unwritable — most often a
            # read-only corpus mount — used to surface as a bare 500 with the
            # traceback only in the container logs. The caller can act on this.
            raise HTTPException(
                status_code=500,
                detail=(
                    f"could not write the memory record to {source.path}: {exc}. "
                    "The memory source needs a writable location; a read-only mount "
                    "will not do."
                ),
            ) from exc
        payload: dict = {"record": record.as_dict(), "created": created, "source": source.name}
        # The record is already durably on disk; this sync only makes it
        # *searchable now*. Failing the whole request when it cannot run — most
        # often because another writer holds the engine lease, which a live run
        # hit immediately — reports "your memory was not saved" when it was.
        # Degrade to deferred indexing instead: the scheduler and the next sync
        # both pick it up.
        if req.sync and created:
            try:
                payload["sync"] = engine.sync_source(source.name, "incremental").__dict__
            except Exception as exc:
                logger.warning("memory write indexed later: %s", exc)
                payload["sync_deferred"] = str(exc)
        # Writing a memory is a mutation of what this region will later assert
        # as fact, which is exactly what the audit log is for. The MCP path has
        # always recorded it; this one silently did not, so the same action was
        # traceable or not depending on which protocol the caller happened to
        # use.
        audit(
            source.name,
            "memory_write",
            {
                "record_id": record.record_id,
                "scope": record.scope,
                "kind": record.kind,
                "created": created,
            },
        )
        return payload

    @app.get("/memory")
    def memory_list(scope: str | None = None, current_only: bool = False) -> dict:
        from pheasant.memory.store import MemoryStore, memory_source

        source = memory_source(config, state)
        if source is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "no enabled memory source configured; add a `type: memory` "
                    "source to pheasant.yaml"
                ),
            )
        try:
            records = MemoryStore(source.path).list_records(scope, current_only=current_only)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"source": source.name, "records": [r.as_dict() for r in records]}

    @app.post("/memory/enable")
    def memory_enable(req: MemoryEnableRequest) -> dict:
        """Provision the agent-memory source (Step 33.11).

        `SourceType.memory` is deliberately absent from `BUILTIN_SOURCE_TYPES`
        — memory sources are owned by the store, not hand-registered through
        the generic source picker, and letting someone point one at an
        arbitrary folder full of unrelated Markdown would make every file in it
        look like something an agent asserted.

        But "not in the picker" had come to mean **unreachable**: a person
        could not turn memory on from the UI at all. A dedicated action keeps
        the invariant (one well-known layout, created by us) while closing that
        gap. Idempotent — enabling twice returns the existing source.
        """
        from pheasant.memory.store import memory_source

        existing = memory_source(config, state)
        if existing is not None:
            return {"status": "already-enabled", "source": existing.model_dump(mode="json")}

        if req.path is None:
            # `/state` is the one location pheasant owns and can always write.
            path = Path(config.pheasant.state_path) / "memory"
        else:
            # An explicit relative path anchors to `pheasant.workspace_root`,
            # exactly as config load anchors any FILESYSTEM_SOURCE_TYPE. Going
            # through `_resolve_source_path` instead resolves against the
            # *process CWD*, which put the folder wherever the server happened
            # to be started from — during development that was the repo
            # checkout, and a record was written into it.
            requested = Path(req.path).expanduser()
            path = (
                requested
                if requested.is_absolute()
                else Path(config.pheasant.workspace_root) / requested
            )
            if not config.security.allow_user_selected_source_paths:
                try:
                    path = resolve_under(str(path), _allowed_roots(config))
                except PathPolicyError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Prove it is writable *before* registering. Otherwise enabling
        # "succeeds" and every later write fails — which is exactly what a
        # read-only corpus mount produced on a live run.
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".pheasant-write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"cannot write memory records to {path}: {exc}. Memory is the one "
                    "source pheasant writes to, so it needs a writable location — a "
                    "read-only corpus mount will not do. Pass an explicit `path`, or "
                    "mount a writable volume."
                ),
            ) from exc

        try:
            source = _source_from_payload(
                {"name": req.name, "type": "memory", "path": str(path), "enabled": True}
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid source: {exc}") from exc

        SourceRegistry(config, state).register_source(source)
        # Same step every other registration takes: the engine reads sources off
        # the live config object, so a source that only reached the registry is
        # invisible to sync.
        config.sources = [s for s in config.sources if s.name != source.name]
        config.sources.append(source)
        audit(source.name, "memory_enable", {"path": str(path)})
        return {"status": "enabled", "source": source.model_dump(mode="json")}

    @app.post("/memory/consolidate")
    def memory_consolidate() -> dict:
        from pheasant.memory.maintenance import run_memory_maintenance

        result = run_memory_maintenance(engine)
        if result is None:
            # 200 with a reason, matching the MCP tool exactly. This used to be
            # a 400, which made the same condition an error over HTTP and a
            # normal outcome over MCP — and it is not a client error: the
            # request was well-formed and the server declined because of its
            # own configuration. A scheduler polling this endpoint should not
            # have to treat "consolidation is switched off" as a failure.
            return {
                "skipped": (
                    "memory consolidation is disabled or no `type: memory` source is configured"
                )
            }
        audit(result["source"], "memory_consolidate", result["report"])
        return result

    @app.post("/security/idp/sync")
    def idp_sync() -> dict:
        from pheasant.security.idp import run_idp_sync

        try:
            return run_idp_sync(config, state)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/security/idp/status")
    def idp_sync_status() -> dict:
        from pheasant.security.idp import idp_status

        return idp_status(config, state)

    # ------------------------------------------------------------------
    # Jobs — everything running in the background, with real progress.
    # ------------------------------------------------------------------
    @app.get("/jobs")
    def list_jobs(active: bool = False, limit: int = 50) -> dict:
        records = jobs.list(active_only=active, limit=limit)
        return {
            "jobs": records,
            "active_count": sum(1 for job in records if job["active"]),
        }

    # Registered BEFORE /jobs/{job_id}: FastAPI matches routes in declaration
    # order, so the parameterised path would otherwise capture "stream" as a
    # job id and answer this with a 404.
    @app.get("/jobs/stream")
    def stream_jobs():
        """Server-sent events, one per job update.

        The alternative — polling `/jobs` on a timer — is the wrong shape for
        something that changes hundreds of times over a few minutes and then
        not at all for an hour.

        Deliberately an **async** generator polling a plain queue, not a sync
        generator blocking on ``queue.get(timeout=...)``. Starlette runs a sync
        generator in a threadpool and cannot interrupt it, so a client that
        disconnects leaves that thread blocking on a queue nobody will ever
        write to again — one leaked thread per dropped connection, forever. An
        async generator is cancelled at the first ``await`` after the
        disconnect, which is at most ``POLL_SECONDS`` away.
        """
        import asyncio
        import json as json_module
        import queue as queue_module

        from starlette.responses import StreamingResponse

        poll_seconds = 0.25
        ping_every = 4.0

        async def publish():
            queue = jobs.subscribe()
            try:
                # Prime the stream with current state, so a client that
                # connects mid-job renders immediately instead of waiting for
                # the next update to tell it anything at all.
                for job in jobs.list(active_only=True):
                    yield f"data: {json_module.dumps({'type': 'job', 'job': job})}\n\n"
                since_ping = 0.0
                while True:
                    drained = False
                    while True:
                        try:
                            item = queue.get_nowait()
                        except queue_module.Empty:
                            break
                        drained = True
                        yield f"data: {json_module.dumps({'type': 'job', 'job': item})}\n\n"
                    if drained:
                        since_ping = 0.0
                    elif since_ping >= ping_every:
                        # Keeps proxies from closing an idle connection, and is
                        # the write that surfaces a dropped client as an error.
                        since_ping = 0.0
                        yield ": ping\n\n"
                    await asyncio.sleep(poll_seconds)
                    since_ping += poll_seconds
            finally:
                jobs.unsubscribe(queue)

        return StreamingResponse(
            publish(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.delete("/jobs")
    def clear_finished_jobs() -> dict:
        return {"cleared": jobs.clear()}

    @app.delete("/jobs/{job_id}")
    def clear_job(job_id: str) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        if job["active"]:
            raise HTTPException(status_code=409, detail="A running job cannot be cleared")
        return {"cleared": jobs.clear(job_id)}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        return job

    @app.get("/sync/status")
    def sync_status() -> dict:
        return {
            "sources": SourceRegistry(config, state).list_sources(),
            "checkpoints": state.list_source_checkpoints(),
        }

    @app.post("/search")
    def search_context(req: SearchRequest) -> dict:
        from pheasant.search.criteria import (
            apply_retrieval_criteria,
            criteria_active,
            criteria_dict,
        )

        # Over-fetch when a post-filter will drop rows, so `max_results` keeps
        # meaning "give me this many" — the same bookkeeping the MCP tool does.
        filtering = criteria_active(
            req.exclude_sources,
            req.node_types,
            req.min_score,
            req.source_types,
            req.exclude_source_types,
        )
        started = time.perf_counter()
        try:
            payload = search.search_context(
                req.knowledge_base or config.knowledge_base_id,
                req.query,
                req.mode,
                req.max_results * 4 if filtering else req.max_results,
                req.source_name,
                graph=engine.graph_builder.graph,
                principal=req.principal,
                principal_groups=req.principal_groups,
                security=config.security,
                section=req.section,
                memory=req.memory,
            )
        except Exception:
            metrics.REGISTRY.inc("pheasant_search_total", mode=req.mode, outcome="error")
            raise
        finally:
            # Timed around retrieval only. Wrapping the post-filter too would
            # fold criteria bookkeeping into what reads as retrieval latency.
            metrics.REGISTRY.observe(
                "pheasant_search_duration_seconds", time.perf_counter() - started, mode=req.mode
            )
        metrics.REGISTRY.inc("pheasant_search_total", mode=req.mode, outcome="ok")
        if filtering:
            payload = dict(payload)
            payload["results"] = apply_retrieval_criteria(
                payload.get("results") or [],
                exclude_sources=req.exclude_sources,
                node_types=req.node_types,
                min_score=req.min_score,
                source_types=req.source_types,
                exclude_source_types=req.exclude_source_types,
            )[: req.max_results]
            payload["criteria"] = criteria_dict(
                req.source_name,
                req.exclude_sources,
                req.node_types,
                req.min_score,
                req.memory,
                req.source_types,
                req.exclude_source_types,
            )
        return payload

    @app.post("/relevant-files")
    def relevant_files(req: SearchRequest) -> dict:
        # Same retrieval as /search, so it must run under the same ACL
        # enforcement: dropping `security`/`principal` here silently returned
        # unfiltered results for every caller whenever acl_enforced was on.
        # No `graph=` on purpose — this route projects to *files*, and graph
        # nodes (concepts, symbols) carry no relative_path, so admitting them
        # would crowd file hits out of the merge and return an empty list.
        payload = search.search_context(
            req.knowledge_base or config.knowledge_base_id,
            req.query,
            "hybrid",
            req.max_results,
            req.source_name,
            principal=req.principal,
            principal_groups=req.principal_groups,
            security=config.security,
            section=req.section,
            # Same reasoning as the ACL note above: this is the same retrieval,
            # so it owes the caller the same memory policy. Omitting it would
            # leave one route still serving corrected records.
            memory=req.memory,
        )
        seen = set()
        files = []
        for result in payload["results"]:
            relative_path = result.get("relative_path")
            if relative_path and relative_path not in seen:
                seen.add(relative_path)
                files.append(result)
        return {"files": files}

    def _acl_guard(
        artifact_id: str | None,
        principal: str | None,
        principal_groups: list[str] | None = None,
    ) -> None:
        """403 unless ``principal`` may read ``artifact_id`` (Step 32.2).

        The content endpoints hand back whole artifact bodies by id or path,
        which bypasses the filtering ``search_context`` does — an ACL-enforcing
        region that filters search results but serves the same bytes from
        /files/summary or /nodes/content has not enforced anything. No-op when
        ``security.acl_enforced`` is off, so pre-32 behavior is unchanged.
        """
        if not config.security.acl_enforced:
            return
        from pheasant.security.acl import expand_principal, is_allowed

        identities = expand_principal(principal, principal_groups, config.security.groups)
        if identities is not None and principal:
            from pheasant.security.idp import fresh_idp_groups

            identities |= fresh_idp_groups(state, principal, config.security.idp)
        default_public = config.security.default_visibility != "private"
        acls = state.artifact_acls([artifact_id]) if artifact_id else {}
        if artifact_id not in acls:
            # Not resolvable to an artifact row: deny, matching the
            # conservative rule the search path applies to bare graph nodes.
            raise HTTPException(status_code=403, detail="Not permitted")
        if not is_allowed(acls[artifact_id], identities, default_public=default_public):
            raise HTTPException(status_code=403, detail="Not permitted")

    @app.get("/files/summary")
    def file_summary(
        path: str,
        source_name: str | None = None,
        principal: str | None = None,
    ) -> dict:
        # GROUP_CONCAT order is arbitrary unless the input rows are ordered,
        # so concatenate over ordered scalar subqueries to keep summaries and
        # content in chunk order.
        rows = state.rows(
            """SELECT artifacts.*,
            (SELECT GROUP_CONCAT(summary, '\n') FROM
                (SELECT summary FROM chunks WHERE artifact_id=artifacts.id
                 ORDER BY chunk_index)) AS summary,
            (SELECT GROUP_CONCAT(text, '\n\n') FROM
                (SELECT text FROM chunks WHERE artifact_id=artifacts.id
                 ORDER BY chunk_index)) AS content
            FROM artifacts
            WHERE artifacts.relative_path=? AND (? IS NULL OR artifacts.source_id=?)
            LIMIT 1""",
            (path, source_name, source_name),
        )
        if not rows:
            return {"path": path, "summary": None}
        _acl_guard(str(rows[0]["id"]), principal)
        return dict(rows[0])

    @app.get("/nodes/content")
    def node_content(node_id: str, principal: str | None = None) -> dict:
        graph = engine.graph_builder.graph
        attrs = graph.nodes.get(node_id)
        if attrs is None:
            raise HTTPException(status_code=404, detail=f"Unknown node: {node_id}")
        node = dict(attrs)
        if node.get("type") == "chunk":
            rows = state.rows(
                "SELECT artifact_id, text FROM chunks WHERE id=? LIMIT 1",
                (node_id,),
            )
            if rows:
                _acl_guard(str(rows[0]["artifact_id"]), principal)
            return {"node_id": node_id, "content": rows[0]["text"] if rows else None}
        _acl_guard(node_id, principal)
        artifact_rows = state.rows("SELECT path FROM artifacts WHERE id=? LIMIT 1", (node_id,))
        if artifact_rows:
            path = Path(artifact_rows[0]["path"])
            if path.exists() and path.is_file():
                content = read_text(path)
                if content:
                    return {"node_id": node_id, "content": content}
        # GROUP_CONCAT ignores a trailing ORDER BY, so order the rows in a
        # subquery to keep the reassembled file content in chunk order.
        rows = state.rows(
            "SELECT GROUP_CONCAT(text, '\n\n') AS content FROM "
            "(SELECT text FROM chunks WHERE artifact_id=? ORDER BY chunk_index)",
            (node_id,),
        )
        content = rows[0]["content"] if rows else None
        return {"node_id": node_id, "content": content}

    @app.get("/taxonomy")
    def taxonomy(
        source: str | None = None,
        path: str | None = None,
        max_nodes: int = 2000,
    ) -> dict:
        """The structural outline of taxonomy-enabled documents.

        Reads the `heading` nodes the sync emitted (it does not re-parse), so
        the tree served here is exactly what the graph and the chunks'
        `heading_path` agree on. Nests by the `level` each heading carries,
        which is the same nesting the graph's `contains` edges encode —
        rebuilt here rather than traversed so one document's outline is one
        response, in document order.

        `path` filters to a single document; `source` to one source. A source
        with taxonomy disabled simply has no heading nodes, so it returns an
        empty tree rather than an error.
        """
        from pheasant.ingestion.taxonomy import (
            Ordinal,
            SectionHeading,
            reconcile_issues,
            taxonomy_tree,
        )

        graph_obj = engine.graph_builder.graph
        by_document: dict[str, list[SectionHeading]] = {}
        seen = 0
        for _node_id, data in graph_obj.node_map().items():
            if data.get("type") != "heading":
                continue
            if source and data.get("source_id") != source:
                continue
            relative = str(data.get("relative_path") or "")
            if path and relative != path:
                continue
            seen += 1
            if seen > max(1, max_nodes):
                break
            parts = tuple(int(p) for p in (data.get("ordinal_parts") or []))
            ordinal = (
                Ordinal(
                    parts=parts,
                    series=str(data.get("ordinal_series") or ""),
                    raw=str(data.get("number") or ""),
                    relative=bool(data.get("ordinal_relative")),
                    suffix=str(data.get("ordinal_suffix") or ""),
                )
                if parts
                else None
            )
            by_document.setdefault(relative, []).append(
                SectionHeading(
                    line=int(data.get("start_line") or 0),
                    level=int(data.get("level") or 1),
                    number=data.get("number"),
                    title=str(data.get("title") or ""),
                    kind=str(data.get("kind") or ""),
                    path=str(data.get("heading_path") or ""),
                    pattern_level=int(data.get("pattern_level") or 0),
                    ordinal=ordinal,
                )
            )

        documents = []
        for relative in sorted(by_document):
            headings = sorted(by_document[relative], key=lambda h: h.line)
            issues = reconcile_issues(headings)
            documents.append(
                {
                    "relative_path": relative,
                    "heading_count": len(headings),
                    "tree": taxonomy_tree(headings),
                    # Gaps / duplicates / out-of-order numbering. For contracts
                    # and procedures "is anything missing?" is the question
                    # people actually ask of a document, and once ordinals are
                    # parsed it is nearly free to answer.
                    "issues": issues,
                }
            )
        return {
            "documents": documents,
            "heading_count": sum(d["heading_count"] for d in documents),
            "issue_count": sum(len(d["issues"]) for d in documents),
            "truncated": seen > max(1, max_nodes),
        }

    @app.get("/graph")
    def graph(
        limit: int | None = None,
        link_limit: int | None = None,
        types: str | None = None,
        exclude_types: str | None = None,
        source: str | None = None,
    ) -> dict:
        def _set(value: str | None) -> set[str] | None:
            items = {t.strip() for t in (value or "").split(",") if t.strip()}
            return items or None

        return node_link(
            engine.graph_builder.graph,
            node_limit=limit,
            link_limit=link_limit,
            node_types=_set(types),
            exclude_node_types=_set(exclude_types),
            source_id=source,
        )

    @app.get("/graph/export/node-link-json")
    def graph_node_link() -> dict:
        return node_link(engine.graph_builder.graph)

    @app.get("/graph/export/cytoscape-json")
    def graph_cyto() -> dict:
        return cytoscape(engine.graph_builder.graph)

    @app.get("/graph/neighbors")
    def graph_neighbors_route(
        node_id: str,
        depth: int = 1,
        edge_types: str | None = None,
        exclude_edge_types: str | None = None,
    ) -> dict:
        types = [t for t in edge_types.split(",") if t] if edge_types else None
        return graph_neighbors(
            engine.graph_builder.graph,
            node_id,
            depth,
            types,
            exclude_edge_types=_edge_type_set(exclude_edge_types),
        )

    @app.get("/graph/slice")
    def graph_slice_route(
        node_id: str,
        depth: int = 1,
        limit: int = 100,
        edge_types: str | None = None,
        exclude_edge_types: str | None = None,
        exclude_types: str | None = None,
    ) -> dict:
        """A bounded sub-graph around a node.

        ``exclude_edge_types`` drops edges from the *traversal*, not just the
        output — the canvas uses it to leave out `indexes`, the source→artifact
        shortcut that otherwise flattens the directory tree out of any bounded
        view of the graph.
        """

        types = [t for t in edge_types.split(",") if t] if edge_types else None
        return graph_slice(
            engine.graph_builder.graph,
            node_id,
            depth,
            types,
            limit,
            exclude_edge_types=_edge_type_set(exclude_edge_types),
            exclude_node_types=_edge_type_set(exclude_types),
        )

    @app.get("/graph/diagnostics")
    def graph_diagnostics(top: int = 20) -> dict:
        """Structural health of the graph, for the full-screen workspace.

        Answers the questions a picture cannot: which nodes are hubs, how much
        of the graph is disconnected, which edge types actually carry weight,
        and how the sources compare. One pinned read of the graph — this walks
        it, so it must not be on a hot path the UI polls.
        """
        graph_obj = engine.graph_builder.graph
        # `iter_edges`/`node_map` hand back the live mappings instead of
        # copying 850k edges per call; both require holding `reading()` for
        # the whole walk, which is exactly what this block does.
        with graph_obj.reading():
            node_types = graph_obj.type_counts()
            total_nodes = graph_obj.number_of_nodes()
            total_links = graph_obj.number_of_edges()
            nodes = graph_obj.node_map()
            degree: dict[str, int] = {}
            edge_types: dict[str, int] = {}
            for (source_id, target_id), edge_map in graph_obj.iter_edges():
                count = len(edge_map)
                degree[source_id] = degree.get(source_id, 0) + count
                degree[target_id] = degree.get(target_id, 0) + count
                for data in edge_map.values():
                    kind = str(data.get("type") or "unknown")
                    edge_types[kind] = edge_types.get(kind, 0) + 1
            hubs = sorted(degree.items(), key=lambda item: (-item[1], item[0]))[: max(1, top)]
            # An orphan is a node no edge touches. On a healthy index this is
            # near zero; a large number means enrichment did not run, or a
            # source indexed content that nothing links to.
            orphans = [node_id for node_id in nodes if node_id not in degree]
            hub_rows = [
                {
                    "node_id": node_id,
                    "degree": count,
                    "label": (nodes.get(node_id) or {}).get("label"),
                    "type": (nodes.get(node_id) or {}).get("type"),
                }
                for node_id, count in hubs
            ]
        return {
            "total_nodes": total_nodes,
            "total_links": total_links,
            "node_types": node_types,
            "edge_types": dict(sorted(edge_types.items(), key=lambda kv: -kv[1])),
            "orphan_count": len(orphans),
            "orphan_sample": orphans[:20],
            "density": round(total_links / total_nodes, 3) if total_nodes else 0.0,
            "hubs": hub_rows,
        }

    @app.get("/graph/path")
    def graph_path(source: str, target: str, max_depth: int = 8) -> dict:
        """Shortest path between two nodes, as a navigable chain.

        "How are these two things related?" is the question a graph is
        uniquely able to answer and the canvas alone cannot — two nodes six
        hops apart are never on screen together. Bidirectional BFS so a miss
        on a large graph costs two shallow frontiers rather than one deep one.
        """
        graph_obj = engine.graph_builder.graph
        if source not in graph_obj:
            raise HTTPException(status_code=404, detail=f"Unknown node: {source}")
        if target not in graph_obj:
            raise HTTPException(status_code=404, detail=f"Unknown node: {target}")
        path = _shortest_path(graph_obj, source, target, max_depth=max_depth)
        return {
            "source": source,
            "target": target,
            "found": path is not None,
            "hops": len(path) - 1 if path else None,
            "path": [
                {"node_id": node_id, **dict(graph_obj.nodes.get(node_id) or {})}
                for node_id in (path or [])
            ],
        }

    @app.get("/nodes/explain")
    def explain_node(node_id: str) -> dict:
        g = engine.graph_builder.graph
        attrs = g.nodes.get(node_id)
        if attrs is None:
            return {"node_id": node_id, "explanation": "Node is not present in the current graph."}
        node = dict(attrs)
        return {
            "node_id": node_id,
            "type": node.get("type"),
            "label": node.get("label"),
            "explanation": f"{node.get('label')} is a {node.get('type')} node indexed by pheasant.",
            "provenance": node.get("provenance"),
            "node": node,
        }

    @app.get("/fs/list")
    def fs_list(path: str | None = None) -> dict:
        roots = _allowed_roots(config)
        if not path:
            browse_roots = _configured_roots(config)
            return {
                "path": None,
                "parent": None,
                "roots": [str(root) for root in browse_roots],
                "entries": [
                    {
                        "name": root.name or str(root),
                        "path": str(root),
                        "is_dir": True,
                        "mounted": root.exists(),
                    }
                    for root in browse_roots
                ],
            }
        try:
            resolved = _resolve_source_path(path, config)
        except PathPolicyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not resolved.exists() or not resolved.is_dir():
            raise HTTPException(status_code=404, detail=f"Not a directory: {resolved}")
        entries = []
        for child in sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith("."):
                continue
            entries.append({"name": child.name, "path": str(child), "is_dir": child.is_dir()})
        parent = resolved.parent
        parent_allowed = config.security.allow_user_selected_source_paths or any(
            parent == r or r in parent.parents or parent == r for r in roots
        )
        return {
            "path": str(resolved),
            "parent": str(parent) if parent_allowed and parent != resolved else None,
            "roots": [str(root) for root in roots],
            "entries": entries,
        }

    @app.get("/config")
    def get_config() -> dict:
        raw_yaml = None
        path = Path(app.state.config_path)
        if path.exists():
            raw_yaml = path.read_text(encoding="utf-8")
        return {
            "path": str(path),
            "effective": config.model_dump(mode="json"),
            "raw_yaml": raw_yaml,
            "profiles": profile_names(),
        }

    @app.put("/config")
    def put_config(req: ConfigWriteRequest) -> dict:
        if req.yaml_text is not None:
            try:
                data = yaml.safe_load(req.yaml_text) or {}
            except yaml.YAMLError as exc:
                raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc
        elif req.config is not None:
            data = req.config
        else:
            raise HTTPException(status_code=400, detail="Provide config or yaml_text")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Config root must be a mapping")
        try:
            candidate = PheasantConfig.model_validate(data)
        except (ConfigError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc
        errors = validate_source_paths(candidate, require_exists=False)
        if errors:
            raise HTTPException(status_code=400, detail="; ".join(errors))
        path = Path(app.state.config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = req.yaml_text if req.yaml_text is not None else dump_config_yaml(data)
        path.write_text(rendered, encoding="utf-8")
        audit(None, "write_config", {"path": str(path)})
        return {
            "status": "written",
            "path": str(path),
            "restart_required": True,
            "effective": candidate.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------
    # Semantic search (embeddings). Everything `search.embeddings` does in
    # YAML, reachable from the UI — including building vectors for content
    # that was indexed before embeddings were switched on.
    # ------------------------------------------------------------------
    # Last vector-backend probe failure, so a store that cannot actually run
    # is reported as such instead of surfacing later as a 500 on reindex.
    store_probe_error: dict[str, str | None] = {"error": None}

    def _reload_vector_stack() -> None:
        """Rebuild the embed-on-sync indexer and query-time searcher in place.

        Embeddings are wired at engine construction, so flipping the config
        without this leaves a live process that has agreed to embed and then
        doesn't — the reindex would report success having written nothing.

        Vector backends import lazily, so constructing an indexer proves
        nothing about the backend being usable: a LanceDB store without the
        ``[vector]`` extra builds fine and only fails on first touch. Probe
        it here so enabling embeddings fails at the moment the user asks for
        it, rather than reporting ``active: true`` and 500ing on reindex.
        """
        from pheasant.search.vector_store import vector_indexer_from_config

        indexer = vector_indexer_from_config(config)
        if indexer is not None:
            try:
                indexer.store.count()
            except Exception as exc:
                store_probe_error["error"] = str(exc)
                engine.vectors = None
                search.vector = None
                raise
        store_probe_error["error"] = None
        engine.vectors = indexer
        search.vector = engine.vector_searcher()

    def _embeddings_status() -> dict:
        from pheasant.search.vector_store import (
            VECTOR_STORE_PROVIDERS,
            vector_store_available,
        )

        settings = config.search.embeddings
        vector_count = 0
        dimensions_on_disk: int | None = None
        store_error: str | None = store_probe_error["error"]
        # An indexer exists but that only means it was *constructed*; the
        # backend imports lazily. Touching the store is what proves it works,
        # so a store that raises here is reported inactive rather than
        # advertising a semantic search that would fail on first use.
        active = engine.vectors is not None and store_error is None
        if engine.vectors is not None:
            try:
                vector_count = int(engine.vectors.store.count())
                vectors = getattr(engine.vectors.store, "all_vectors", None)
                if callable(vectors):
                    sample = vectors()
                    if sample:
                        dimensions_on_disk = len(sample[0][1])
            except Exception as exc:  # a broken store must still render a page
                store_error = str(exc)
                active = False
        chunk_rows = state.rows("SELECT COUNT(*) AS n FROM chunks")
        chunk_count = int(chunk_rows[0]["n"]) if chunk_rows else 0
        return {
            "enabled": settings.enabled,
            "active": active,
            "provider": settings.provider,
            "model": settings.model,
            "base_url": settings.base_url,
            "api_key_env": settings.api_key_env,
            "api_key_present": bool(os.environ.get(settings.api_key_env)),
            "dimensions": settings.dimensions,
            "batch_size": settings.batch_size,
            "store_provider": config.search.vector_store.provider,
            "store_path": str(config.search.vector_store.path or ""),
            "vector_count": vector_count,
            "chunk_count": chunk_count,
            # How much of the index is actually searchable semantically.
            "coverage": round(vector_count / chunk_count, 4) if chunk_count else 0.0,
            "dimensions_on_disk": dimensions_on_disk,
            "store_error": store_error,
            "providers": [
                {
                    "id": "stub",
                    "label": "Deterministic stub (offline)",
                    "needs_key": False,
                    "description": "No network, no model. Useful for trying semantic "
                    "search out and for tests.",
                },
                {
                    "id": "openai-spec",
                    "label": "OpenAI-spec endpoint",
                    "needs_key": True,
                    "description": "POST {base_url}/embeddings — OpenAI, or any "
                    "gateway or self-hosted server speaking the same shape.",
                },
            ],
            "store_providers": [
                {
                    "id": name,
                    "label": label,
                    "available": vector_store_available(name),
                    "hint": hint,
                }
                for name, label, hint in VECTOR_STORE_PROVIDERS
            ],
        }

    @app.get("/search/embeddings")
    def embeddings_status() -> dict:
        return _embeddings_status()

    @app.put("/search/embeddings")
    def embeddings_update(req: EmbeddingsRequest) -> dict:
        from pheasant.search.vector_store import (
            VECTOR_STORE_PROVIDERS,
            vector_store_available,
        )

        settings = config.search.embeddings
        # Refuse a backend this deployment cannot run *before* touching the
        # config, so a bad choice leaves the running process exactly as it was
        # instead of half-applied and inert.
        target_store = req.store_provider or config.search.vector_store.provider
        wants_on = settings.enabled if req.enabled is None else req.enabled
        if wants_on and not vector_store_available(target_store):
            hint = next(
                (hint for name, _, hint in VECTOR_STORE_PROVIDERS if name == target_store),
                None,
            )
            raise HTTPException(
                status_code=400,
                detail=f"Vector store {target_store!r} is not available in this deployment"
                + (f" — {hint}" if hint else "")
                + ". The 'numpy' store needs no extra dependencies.",
            )
        requested = {
            "enabled": req.enabled,
            "provider": req.provider,
            "model": req.model,
            "base_url": req.base_url,
            "api_key_env": req.api_key_env,
            "dimensions": req.dimensions,
            "batch_size": req.batch_size,
        }
        changes = {
            key: value
            for key, value in requested.items()
            if value is not None or (key == "dimensions" and key in req.model_fields_set)
        }
        # Provider, endpoint, model and dimensions together define the vector
        # space. A backend change can also uncover an older index in a
        # different space. Any of them invalidates every existing vector.
        vector_space_change = any(
            key in changes and changes[key] != getattr(settings, key)
            for key in ("provider", "base_url", "model", "dimensions")
        ) or (
            req.store_provider is not None
            and req.store_provider != config.search.vector_store.provider
        )

        for key, value in changes.items():
            setattr(settings, key, value)
        if req.store_provider is not None:
            config.search.vector_store.provider = req.store_provider

        try:
            _reload_vector_stack()
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"Could not enable embeddings: {exc}"
            ) from exc

        # Clear incompatible vectors immediately, even when the caller wants
        # to rebuild later. Leaving them queryable lets the new embedder send
        # (for example) 3,072-wide queries to a 1,536-wide Lance table. Reset
        # drops Lance's Arrow schema as well as its rows; row deletion alone
        # cannot change a FixedSizeList width.
        dropped_vectors = 0
        if vector_space_change and engine.vectors is not None:
            dropped_vectors = engine.vectors.reset()

        wrote_config = False
        if req.persist:
            payload = {
                "search": {
                    "embeddings": settings.model_dump(mode="json"),
                    "vector_store": config.search.vector_store.model_dump(mode="json"),
                }
            }
            wrote_config = _merge_into_config_file(payload)
        audit(None, "update_embeddings", {"changes": list(changes), "persisted": wrote_config})

        result = {
            "status": "updated",
            "wrote_config": wrote_config,
            "vectors_invalidated": vector_space_change,
            "vectors_dropped": dropped_vectors,
            **_embeddings_status(),
        }
        if req.reindex and engine.vectors is not None:
            # A changed vector space was reset above; unchanged settings keep
            # their existing vectors and fill only missing chunk ids.
            result["reindex"] = _rebuild_vectors(drop_existing=False)
            # Record the space we just embedded into, so the next restart sees
            # a matching fingerprint and does not drop and re-embed all over
            # again for a change that has already been applied.
            state.set_fingerprint(EMBEDDING_SCOPE, embedding_fingerprint(config), utc_now())
        return result

    def _rebuild_vectors(drop_existing: bool = False) -> dict:
        """Embed everything already indexed, without re-reading the sources.

        Vectors are keyed by content-addressed chunk id, so this is
        idempotent: chunks already embedded are skipped and a second run
        makes zero embedder calls. The engine owns the same operation (it
        runs it automatically when the embedding config changes under a
        restart); this wrapper adds the HTTP-facing error shape.
        """
        if engine.vectors is None:
            raise HTTPException(
                status_code=400,
                detail=store_probe_error["error"]
                or "Embeddings are not enabled — turn them on first.",
            )
        embedded = 0
        artifacts = state.rows("SELECT id, source_id FROM artifacts ORDER BY id")
        try:
            if drop_existing:
                # A changed model/dimension means the old vectors are garbage.
                for source_id in {
                    str(row["source_id"])
                    for row in state.rows("SELECT DISTINCT source_id FROM artifacts")
                }:
                    engine.vectors.prune_source(source_id, set())

            for artifact in artifacts:
                chunks = state.rows(
                    "SELECT id, text, text_hash FROM chunks WHERE artifact_id=? "
                    "ORDER BY chunk_index",
                    (artifact["id"],),
                )
                if not chunks:
                    continue
                embedded += engine.vectors.index_artifact(
                    str(artifact["source_id"]),
                    str(artifact["id"]),
                    [dict(chunk) for chunk in chunks],
                )
            # index_artifact queues rather than embeds immediately (batches
            # across artifacts so this loop isn't one embedder call per
            # artifact) — flush before reading the count, or a remainder
            # smaller than the queue threshold would be silently unembedded
            # and undercounted.
            engine.vectors.flush()
            vector_count = int(engine.vectors.store.count())
        except (ModuleNotFoundError, ValueError) as exc:
            # A missing optional extra or a mismatched embedding space is a
            # configuration problem the caller can act on, not a server fault.
            store_probe_error["error"] = str(exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(None, "rebuild_vectors", {"embedded_chunks": embedded})
        return {
            "embedded_chunks": embedded,
            "artifacts_scanned": len(artifacts),
            "vector_count": vector_count,
        }

    @app.post("/search/embeddings/reindex")
    def embeddings_reindex(drop_existing: bool = False) -> dict:
        return {**_rebuild_vectors(drop_existing=drop_existing), **_embeddings_status()}

    def _merge_into_config_file(patch: dict) -> bool:
        """Deep-merge ``patch`` into the config file, preserving everything else."""
        from pheasant.config.loader import deep_merge

        path = Path(app.state.config_path)
        if not path.exists():
            return False
        yaml_error = getattr(yaml, "YAMLError", ValueError)
        try:
            existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Configuration file cannot be read for persistence: {path}",
            ) from exc
        except yaml_error:
            return False
        if not isinstance(existing, dict):
            return False
        merged = deep_merge(existing, patch)
        try:
            path.write_text(dump_config_yaml(merged), encoding="utf-8")
        except OSError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Configuration file is not writable: {path}. "
                    "Mount it read-write or save with persistence disabled."
                ),
            ) from exc
        return True

    # ------------------------------------------------------------------
    # Editing the live knowledge base, one section at a time. `PUT /config`
    # replaces the whole file and always demands a restart; these routes let
    # the UI change one thing and be told honestly what happened to it.
    # ------------------------------------------------------------------
    @app.get("/config/sections")
    def config_sections() -> dict:
        effective = config.model_dump(mode="json")
        return {
            "sections": [
                {
                    "id": name,
                    "values": effective.get(name, {}),
                    "live_applicable": live,
                }
                for name, live in LIVE_APPLICABLE_SECTIONS.items()
            ]
        }

    @app.patch("/config/section/{section}")
    def patch_config_section(section: str, req: ConfigSectionRequest) -> dict:
        """Validate, persist and (where safe) hot-apply one config section.

        Validation goes through the *whole* config rather than the section
        alone: sections are not independent (``storage`` paths derive from
        ``pheasant.state_path``), and validating a fragment in isolation
        accepts combinations the loader would reject.
        """
        if section not in LIVE_APPLICABLE_SECTIONS:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown config section: {section}. Known sections: "
                + ", ".join(sorted(LIVE_APPLICABLE_SECTIONS)),
            )
        if not isinstance(req.values, dict):
            raise HTTPException(status_code=400, detail="values must be a mapping")

        from pheasant.config.loader import deep_merge

        current = config.model_dump(mode="json")
        candidate_data = deep_merge(current, {section: req.values})
        try:
            candidate = PheasantConfig.model_validate(candidate_data)
        except (ConfigError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid {section}: {exc}") from exc

        live = LIVE_APPLICABLE_SECTIONS[section]
        applied = False
        if live:
            # Swap the whole settings object rather than patching fields one by
            # one: a partial swap leaves the process in a state that is neither
            # the old config nor the new one, which is the worst of both.
            setattr(config, section, getattr(candidate, section))
            applied = True
            if section == "search":
                try:
                    _reload_vector_stack()
                except Exception as exc:  # embeddings may be misconfigured
                    logger.warning("search section applied but vectors not reloaded: %s", exc)
        wrote = False
        if req.persist:
            wrote = _merge_into_config_file({section: req.values})
        audit(None, "patch_config_section", {"section": section, "persisted": wrote})
        return {
            "status": "updated",
            "section": section,
            "applied": applied,
            "restart_required": not live,
            "wrote_config": wrote,
            "values": candidate.model_dump(mode="json").get(section, {}),
        }

    @app.get("/knowledge-base")
    def knowledge_base() -> dict:
        return {
            "id": config.knowledge_base_id,
            "name": config.pheasant.name,
            "description": config.pheasant.description,
            "environment": config.pheasant.environment,
            "version": __version__,
            "state_path": str(config.pheasant.state_path),
            "config_path": str(app.state.config_path),
        }

    @app.put("/knowledge-base")
    def update_knowledge_base(req: KnowledgeBaseRequest) -> dict:
        """Edit this knowledge base's identity.

        ``description`` is free to change. ``name`` is not cosmetic — it *is*
        ``kb_id``: the graph's root node id, the key every stable artifact ID
        hangs off, and the identity a Synapse router routes to (CLAUDE.md §4
        rule 3). Renaming is allowed, but it is reported as what it is: the
        existing graph belongs to the old name and a full re-index is needed
        to rebuild it under the new one. Silently accepting the rename and
        leaving an orphaned graph behind would be the actual bug.
        """
        changes: dict = {}
        if req.description is not None:
            config.pheasant.description = req.description
            changes["description"] = req.description
        rename = None
        if req.name is not None and req.name != config.pheasant.name:
            previous = config.pheasant.name
            if not req.name.strip():
                raise HTTPException(status_code=400, detail="name must not be empty")
            changes["name"] = req.name
            rename = {
                "previous": previous,
                "current": req.name,
                "reindex_required": True,
                "detail": (
                    f"The indexed graph is stored under {previous!r}. Run a full "
                    f"sync (`pheasant sync --all --mode full`) to rebuild it under "
                    f"{req.name!r}; until then this knowledge base will look empty."
                ),
            }
        if not changes:
            return {"status": "unchanged", **knowledge_base()}
        wrote = False
        if req.persist:
            wrote = _merge_into_config_file({"pheasant": changes})
        if rename:
            # Applied to the file only. Swapping kb_id in the live process
            # would repoint every subsequent write at a graph this process has
            # not loaded, which is a worse outcome than requiring a restart.
            config.pheasant.name = rename["previous"]
        audit(None, "update_knowledge_base", {"changes": sorted(changes), "persisted": wrote})
        return {
            "status": "updated",
            "changed": sorted(changes),
            "wrote_config": wrote,
            "restart_required": rename is not None,
            "rename": rename,
            **knowledge_base(),
        }

    @app.get("/config/effective")
    def config_effective(profile: str = "quickstart") -> dict:
        try:
            return effective_config_dict(app.state.config_path, profile, {})
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # Overview — one call that tells the UI whether there is anything to
    # show yet, and what shape it is. Drives the onboarding empty state.
    # ------------------------------------------------------------------
    @app.get("/overview")
    def overview() -> dict:
        graph_obj = engine.graph_builder.graph
        # One pinned read of aggregates the graph maintains on write. This
        # used to walk every node to tally types on an endpoint the UI hits on
        # every page load — seconds of latency for numbers already known.
        with graph_obj.reading():
            counts = graph_obj.type_counts()
            total_nodes = graph_obj.number_of_nodes()
            total_links = graph_obj.number_of_edges()
        registered = _with_sync_state(SourceRegistry(config, state).list_sources())
        artifacts = state.rows("SELECT COUNT(*) AS n FROM artifacts")
        chunks = state.rows("SELECT COUNT(*) AS n FROM chunks")
        indexed = int(artifacts[0]["n"]) if artifacts else 0
        # "Content" excludes the knowledge-base root and its source nodes:
        # a freshly-created KB has those and nothing else, and showing a
        # lone star node as if it were a graph is exactly the confusion
        # this field exists to prevent.
        structural = counts.get("knowledge_base", 0) + counts.get("source", 0)
        return {
            "knowledge_base": config.knowledge_base_id,
            "name": config.pheasant.name,
            "description": config.pheasant.description,
            "version": __version__,
            "sources": registered,
            "source_count": len(registered),
            "indexed_artifacts": indexed,
            "chunk_count": int(chunks[0]["n"]) if chunks else 0,
            "node_counts": counts,
            "total_nodes": total_nodes,
            "total_links": total_links,
            "has_content": total_nodes > structural and indexed > 0,
            "config_path": str(app.state.config_path),
        }

    # ------------------------------------------------------------------
    # Assistant — grounded chat over the index. Never in the sync path.
    # ------------------------------------------------------------------
    @app.get("/assistant/status")
    def assistant_status(session_id: str | None = None) -> dict:
        from pheasant.assistant.providers import PROVIDERS, resolve_auto_provider

        settings = config.assistant
        providers = [
            {
                "id": spec.id,
                "label": spec.label,
                "default_model": spec.default_model,
                "api_key_env": spec.api_key_env,
                "key_hint": spec.key_hint,
                # Whether the *server* already holds a key for this provider.
                "env_key_present": bool(os.environ.get(spec.api_key_env)),
            }
            for spec in PROVIDERS.values()
        ]
        from pheasant.assistant.chat import resolve_provider

        credential = app.state.session_keys.get(session_id)
        configured = settings.provider
        if configured == "auto":
            configured = resolve_auto_provider(dict(os.environ))
        # `ready` must mean "a credential actually resolves", not "a provider
        # name is configured" — otherwise a config naming a provider whose key
        # env var is unset reports "model connected" while every answer comes
        # back extractive. Resolve exactly the way /assistant/chat does.
        selected = resolve_provider(config, credential, dict(os.environ))
        return {
            "enabled": settings.enabled,
            "providers": providers,
            "configured_provider": configured,
            "configured_model": settings.model,
            "allow_session_keys": settings.allow_session_keys,
            "session": credential.redacted() if credential else None,
            # False means answers will be extractive rather than synthesized.
            "ready": selected is not None,
            "credential_source": selected.get("source") if selected else None,
        }

    @app.post("/assistant/key")
    def assistant_key(req: AssistantKeyRequest) -> dict:
        """Hold a user-supplied key in memory for this session only.

        The key never leaves this process: not to disk, not to the config,
        not into any response body. The caller gets an opaque session id.
        """
        from pheasant.assistant.providers import PROVIDERS

        if not config.assistant.allow_session_keys:
            raise HTTPException(
                status_code=403,
                detail="Session-supplied API keys are disabled (assistant.allow_session_keys)",
            )
        if req.provider not in PROVIDERS:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {req.provider}")
        if not req.api_key.strip():
            raise HTTPException(status_code=400, detail="API key must not be empty")
        token, credential = app.state.session_keys.put(
            req.provider,
            req.api_key.strip(),
            model=req.model,
            base_url=req.base_url,
        )
        return {"session_id": token, "session": credential.redacted()}

    @app.delete("/assistant/key")
    def assistant_key_revoke(session_id: str | None = None) -> dict:
        return {"revoked": app.state.session_keys.revoke(session_id)}

    @app.post("/assistant/chat")
    def assistant_chat(req: ChatRequest) -> dict:
        from pheasant.assistant.chat import answer_question

        if not config.assistant.enabled:
            raise HTTPException(status_code=403, detail="The assistant is disabled")
        if not req.question.strip():
            raise HTTPException(status_code=400, detail="question must not be empty")
        return answer_question(
            req.question,
            search=search,
            knowledge_base=config.knowledge_base_id,
            config=config,
            graph=engine.graph_builder.graph,
            state=state,
            credential=app.state.session_keys.get(req.session_id),
            env=dict(os.environ),
            mode=req.mode,
            max_results=req.max_results,
            source_name=req.source_name,
            source_types=req.source_types,
            exclude_source_types=req.exclude_source_types,
            principal=req.principal,
            principal_groups=req.principal_groups,
            workflow=req.workflow,
            options=req.options,
            memory=req.memory,
        )

    @app.post("/assistant/chat/stream")
    def assistant_chat_stream(req: ChatRequest):
        """The same answer as ``/assistant/chat``, with progress as it happens.

        Server-sent events: one ``step`` per workflow stage the moment it
        finishes, then a single ``answer`` (or ``error``) and the stream
        closes. The agent loop can take a while over a large index, and a
        client that shows "planning… retrieved 35 passages… grading" is
        waiting rather than wondering. The work runs in a worker thread and
        the steps arrive through a queue, so a slow reader can never stall the
        workflow itself.
        """

        import json as json_module
        import queue as queue_module
        import threading

        from starlette.responses import StreamingResponse

        from pheasant.assistant.chat import answer_question

        if not config.assistant.enabled:
            raise HTTPException(status_code=403, detail="The assistant is disabled")
        if not req.question.strip():
            raise HTTPException(status_code=400, detail="question must not be empty")

        events: queue_module.Queue = queue_module.Queue()
        credential = app.state.session_keys.get(req.session_id)
        environ = dict(os.environ)

        def run() -> None:
            try:
                answer = answer_question(
                    req.question,
                    search=search,
                    knowledge_base=config.knowledge_base_id,
                    config=config,
                    graph=engine.graph_builder.graph,
                    state=state,
                    credential=credential,
                    env=environ,
                    mode=req.mode,
                    max_results=req.max_results,
                    source_name=req.source_name,
                    source_types=req.source_types,
                    exclude_source_types=req.exclude_source_types,
                    principal=req.principal,
                    principal_groups=req.principal_groups,
                    workflow=req.workflow,
                    options=req.options,
                    on_step=lambda step: events.put(
                        {
                            "type": "step",
                            "name": step.name,
                            "detail": step.detail,
                            "passages": step.passages,
                        }
                    ),
                )
                events.put({"type": "answer", "answer": answer})
            except Exception as exc:  # surfaced to the client, never a 500 mid-stream
                logger.exception("streaming chat failed")
                events.put({"type": "error", "error": str(exc)})
            finally:
                events.put(None)

        threading.Thread(target=run, name="pheasant-chat-stream", daemon=True).start()

        def publish():
            while True:
                item = events.get()
                if item is None:
                    return
                yield f"data: {json_module.dumps(item)}\n\n"

        return StreamingResponse(
            publish(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # nginx sits in front of this in the compose stack and would
                # otherwise buffer the whole stream into one write.
                "X-Accel-Buffering": "no",
            },
        )

    # ------------------------------------------------------------------
    # Retrieval criteria. The same knobs an MCP client can override per
    # call (`preview_retrieval`), reachable as standing configuration.
    # ------------------------------------------------------------------
    def _retrieval_payload() -> dict:
        from pheasant.assistant.workflows.agentic import DEFAULTS as AGENTIC_DEFAULTS

        settings = config.assistant.retrieval
        return {
            "retrieval": settings.model_dump(mode="json"),
            # What actually reaches the workflow once workflow_options is
            # layered on top — the honest answer to "what is it doing", which
            # is not the same as "what did I set" whenever both are in play.
            "effective": {
                **settings.as_options(),
                **{
                    key: value
                    for key, value in (config.assistant.workflow_options or {}).items()
                    if not isinstance(value, dict)
                },
            },
            "workflow_options": dict(config.assistant.workflow_options or {}),
            "defaults": AGENTIC_DEFAULTS,
            "field_help": RETRIEVAL_FIELD_HELP,
        }

    @app.get("/assistant/retrieval")
    def assistant_retrieval() -> dict:
        return _retrieval_payload()

    @app.put("/assistant/retrieval")
    def assistant_retrieval_update(req: RetrievalRequest) -> dict:
        settings = config.assistant.retrieval
        # `exclude_none` on purpose: a PUT that sets one knob must not reset
        # the other nine to their schema defaults.
        changes = req.model_dump(exclude={"persist"}, exclude_none=True)
        previous = {key: getattr(settings, key) for key in changes}
        for key, value in changes.items():
            setattr(settings, key, value)
        wrote = False
        try:
            if req.persist and changes:
                wrote = _merge_into_config_file(
                    {"assistant": {"retrieval": settings.model_dump(mode="json")}}
                )
        except HTTPException:
            # A failed disk write must not leave a request that reported an
            # error active only in memory until the next restart.
            for key, value in previous.items():
                setattr(settings, key, value)
            raise
        audit(None, "update_retrieval", {"changes": sorted(changes), "persisted": wrote})
        return {
            "status": "updated",
            "changed": sorted(changes),
            "wrote_config": wrote,
            # Retrieval is query-time only, so this takes effect on the next
            # question — no restart, nothing to re-index.
            "applied": True,
            **_retrieval_payload(),
        }

    @app.get("/assistant/workflows")
    def assistant_workflows() -> dict:
        """Every question-answering workflow this deployment can run."""
        from pheasant.assistant.chat import resolve_provider
        from pheasant.assistant.workflows import (
            langgraph_available,
            list_workflows,
            resolve_workflow_name,
        )
        from pheasant.assistant.workflows.agentic import DEFAULTS as AGENTIC_DEFAULTS

        selected = resolve_provider(config, None, dict(os.environ))
        return {
            "workflows": list_workflows(),
            "configured": config.assistant.workflow,
            "active": resolve_workflow_name(
                config.assistant.workflow, has_llm=selected is not None
            ),
            "agent_extra_installed": langgraph_available(),
            "options": dict(config.assistant.workflow_options or {}),
            "option_defaults": {"agentic": AGENTIC_DEFAULTS},
        }

    # ------------------------------------------------------------------
    # MCP connection details, so the UI can hand an agent a working config.
    # ------------------------------------------------------------------
    @app.get("/mcp/info")
    def mcp_info() -> dict:
        from pheasant.mcp_client.agents import agent_mcp_config, render_agent_mcp_json

        transports = dict(config.server.mcp.transports)
        base = f"http://{config.server.host}:{config.server.port}"
        clients = {}
        for client_name in ("claude-code", "cursor"):
            try:
                clients[client_name] = render_agent_mcp_json(
                    agent_mcp_config("local", config_path=str(app.state.config_path))
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("could not render %s mcp config: %s", client_name, exc)
        return {
            "enabled": config.server.mcp.enabled,
            "transports": transports,
            "streamable_http_url": f"{base}/mcp" if transports.get("streamable_http") else None,
            "stdio_command": ["pheasant", "mcp", "--transport", "stdio"],
            "config_path": str(app.state.config_path),
            "tools": _mcp_tool_summaries(),
            "client_configs": clients,
        }

    # ------------------------------------------------------------------
    # Quick add — the UI counterpart to `pheasant up <anything>`.
    # ------------------------------------------------------------------
    @app.post("/sources/quick-add")
    def quick_add(req: QuickAddRequest) -> dict:
        from pheasant.targets import TargetError, fetch_target, resolve_targets

        state_dir = Path(config.pheasant.state_path)
        try:
            targets = resolve_targets(
                [req.target],
                clone_root=state_dir / "sources",
                workspace=state_dir / "external",
                split=req.split,
                name=req.name,
            )
            for target in targets:
                fetch_target(target)
        except TargetError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        registry = SourceRegistry(config, state)
        created: list[dict] = []
        for target in targets:
            payload = target.to_source_dict()
            if req.taxonomy:
                payload["taxonomy"] = {"enabled": True}
            if target.local:
                try:
                    payload["path"] = str(_resolve_source_path(payload["path"], config))
                except PathPolicyError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                source = _source_from_payload(payload)
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=f"Invalid source: {exc}") from exc
            registry.register_source(source)
            # The engine resolves sources off the live config object, so a
            # freshly registered source is invisible to sync until it lands
            # there too (same step the /sources route takes).
            config.sources = [s for s in config.sources if s.name != source.name]
            config.sources.append(source)
            audit(source.name, "quick_add", {"target": req.target, "type": target.type})
            created.append(payload)

        results = []
        syncing: list[str] = []
        job_ids: list[str] = []
        queued_tasks: list[str] = []
        if req.sync_now:
            if req.wait:
                from dataclasses import asdict

                for entry in created:
                    try:
                        results.append(asdict(engine.sync_source(entry["name"], req.sync_mode)))
                    except Exception as exc:  # surfaced per-source; others still run
                        results.append(
                            {"source_id": entry["name"], "status": "error", "error": str(exc)}
                        )
            else:
                # Cloning (above) still happens on this request — it's the
                # bounded part. Indexing is the unbounded part (a big repo's
                # first full parse+chunk+embed pass can run well past any
                # reverse-proxy timeout), so that's what moves to the
                # background; the caller gets sources back already
                # registered and polls GET /sources' `syncing` field.
                for entry in created:
                    job_id, queued = _start_background_sync(entry["name"], req.sync_mode)
                    if job_id:
                        job_ids.append(job_id)
                    queued_tasks.extend(queued)
                    syncing.append(entry["name"])
        return {
            "status": "registered",
            "sources": created,
            "sync_results": results,
            "syncing": syncing,
            "job_ids": job_ids,
            "queued_tasks": queued_tasks,
        }

    # Mounted *before* the UI so the SPA catch-all cannot swallow /mcp.
    if mcp_asgi_app is not None:

        @app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
        async def _mcp_no_slash():
            """Send the slash-less form to the mount, preserving method + body.

            Starlette's ``Mount`` only matches paths that continue past the
            prefix, so the ``/mcp`` we advertise would 405 while ``/mcp/``
            worked. 307 (not 302) is what keeps a POST a POST and carries the
            JSON-RPC body with it.
            """
            from starlette.responses import RedirectResponse

            return RedirectResponse(url="/mcp/", status_code=307)

        app.mount("/mcp", mcp_asgi_app)
    _mount_ui(app, config)
    return app


def _mcp_tool_summaries() -> list[dict]:
    """Name + one-line description for each public MCP tool.

    Read off ``PheasantTools`` rather than hand-maintained, so a tool added
    there shows up in the UI without a second edit (rule 8: the tool
    surface is public API, and this is one of its shop windows).
    """
    from pheasant.mcp_server.tools import PheasantTools

    summaries = []
    for name in sorted(dir(PheasantTools)):
        if name.startswith("_"):
            continue
        member = getattr(PheasantTools, name, None)
        if not callable(member):
            continue
        doc = (member.__doc__ or "").strip().splitlines()
        summaries.append({"name": name, "description": doc[0] if doc else ""})
    return summaries


def _mcp_asgi_app(config: PheasantConfig):
    """The MCP streamable-HTTP ASGI app to mount at ``/mcp``, or None.

    ``GET /mcp/info`` has always advertised ``streamable_http_url:
    <base>/mcp``, but ``pheasant serve`` only ever built the FastAPI app — so
    that URL 404'd, and a client POSTing to it got a 405. Found by pointing a
    real MCP client at a live container: the server was telling every agent to
    connect somewhere it did not listen.

    Returns None when MCP or the transport is disabled, or when the installed
    ``mcp`` package cannot build the app — the caller then mounts nothing and
    the API behaves exactly as it did before.
    """
    try:
        from pheasant.mcp_server.server import streamable_http_app

        return streamable_http_app(config)
    except Exception:  # pragma: no cover - depends on the optional [mcp] extra
        logger.warning("MCP streamable-http app unavailable; /mcp not mounted", exc_info=True)
        return None


def _mount_ui(app: FastAPI, config: PheasantConfig) -> None:
    """Optionally serve a prebuilt UI bundle (Option B in the design doc).

    This is additive and off unless a built bundle exists; the indexing
    container image does not build the UI, so by default nothing is mounted.
    """
    # Recorded either way so callers (the CLI's startup banner) can tell the
    # user whether the UI is actually being served, instead of leaving them to
    # guess why the port shows JSON.
    app.state.ui_dist = None
    if not config.server.ui.enabled:
        return
    dist = os.environ.get("PHEASANT_UI_DIST")
    candidates = [Path(dist)] if dist else []
    candidates.append(Path(__file__).resolve().parents[3] / "ui" / "dist")
    target = next((c for c in candidates if c.exists() and c.is_dir()), None)
    if target is None:
        return
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(target), html=True), name="ui")
    app.state.ui_dist = str(target)
    logger.info("Serving pheasant UI bundle from %s", target)
