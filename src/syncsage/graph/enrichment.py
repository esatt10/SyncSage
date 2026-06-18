from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from syncsage.config.schema import SourceConfig
from syncsage.ingestion.pipeline import ParsedArtifact

ENRICHED_NODE_TYPES = {"symbol", "entity", "concept", "external_reference"}
ENRICHED_EDGE_TYPES = {
    "references",
    "imports",
    "calls",
    "similar_to",
    "derived_from",
    "mentions",
}

STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "but",
    "can",
    "for",
    "from",
    "has",
    "into",
    "its",
    "not",
    "the",
    "this",
    "with",
    "your",
}

# Synapse 21.6B: concept normalization. A small, deterministic stoplist of
# words whose trailing "s"/"es"/"ies" must NOT be singularized — either
# because the singular changes meaning (status -> statu) or the word is not a
# plural at all (its, this, analysis, basis, ...). No NLP dependency is used;
# the rules below are pure-python and reproducible so concept ids stay stable.
SINGULARIZE_STOPLIST = frozenset(
    {
        "analysis",
        "axis",
        "basis",
        "bias",
        "bus",
        "canvas",
        "class",
        "css",
        "data",
        "dns",
        "focus",
        "gas",
        "https",
        "ies",
        "index",
        "ios",
        "is",
        "its",
        "less",
        "lens",
        "news",
        "os",
        "pass",
        "process",
        "series",
        "species",
        "status",
        "this",
        "thus",
        "tls",
        "ux",
        "yes",
    }
)


@dataclass(frozen=True)
class EnrichmentNode:
    id: str
    type: str
    label: str
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EnrichmentEdge:
    source: str
    target: str
    type: str
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArtifactEnrichment:
    nodes: list[EnrichmentNode] = field(default_factory=list)
    edges: list[EnrichmentEdge] = field(default_factory=list)
    terms: list[dict[str, Any]] = field(default_factory=list)
    symbols: list[dict[str, Any]] = field(default_factory=list)
    concept_terms: set[str] = field(default_factory=set)

    def extend(self, other: ArtifactEnrichment) -> None:
        self.nodes.extend(other.nodes)
        self.edges.extend(other.edges)
        self.terms.extend(other.terms)
        self.symbols.extend(other.symbols)
        self.concept_terms.update(other.concept_terms)


class ArtifactEnrichmentPass(Protocol):
    name: str

    def run(
        self,
        kb_id: str,
        source: SourceConfig,
        artifact: ParsedArtifact,
    ) -> ArtifactEnrichment:
        ...


class CodeEnrichmentPass:
    name = "code"

    def run(
        self,
        kb_id: str,
        source: SourceConfig,
        artifact: ParsedArtifact,
    ) -> ArtifactEnrichment:
        if Path(artifact.relative_path).suffix.lower() != ".py":
            return ArtifactEnrichment()
        text = artifact_text(artifact)
        enrichment = _base_concepts(kb_id, source, artifact, text)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return enrichment

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _add_external_reference(
                        enrichment,
                        kb_id,
                        source,
                        artifact,
                        alias.name,
                        "imports",
                        "python_import",
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                _add_external_reference(
                    enrichment,
                    kb_id,
                    source,
                    artifact,
                    node.module,
                    "imports",
                    "python_import",
                )
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                symbol_type = "class" if isinstance(node, ast.ClassDef) else "function"
                _add_symbol(
                    enrichment,
                    kb_id,
                    source,
                    artifact,
                    node.name,
                    symbol_type,
                    node.lineno,
                    getattr(node, "end_lineno", node.lineno),
                )
                if isinstance(node, ast.ClassDef):
                    _add_entity(enrichment, kb_id, source, artifact, node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for target in _assignment_targets(node):
                    if target.isupper():
                        _add_symbol(
                            enrichment,
                            kb_id,
                            source,
                            artifact,
                            target,
                            "constant",
                            node.lineno,
                            getattr(node, "end_lineno", node.lineno),
                        )
            elif isinstance(node, ast.Call):
                call_name = _call_name(node.func)
                if call_name:
                    _add_call(enrichment, kb_id, source, artifact, call_name, node.lineno)
        return enrichment


class MarkdownDocumentEnrichmentPass:
    name = "markdown_document"

    def run(
        self,
        kb_id: str,
        source: SourceConfig,
        artifact: ParsedArtifact,
    ) -> ArtifactEnrichment:
        suffix = Path(artifact.relative_path).suffix.lower()
        if suffix not in {".md", ".txt", ".html", ".xml"}:
            return ArtifactEnrichment()
        text = artifact_text(artifact)
        enrichment = _base_concepts(kb_id, source, artifact, text)
        for heading in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*$", text):
            _add_concept(enrichment, kb_id, source, artifact, _clean_inline(heading), 2.0)
        for target in _markdown_links(text):
            ref_type = "url" if target.startswith(("http://", "https://")) else "document_link"
            _add_external_reference(
                enrichment,
                kb_id,
                source,
                artifact,
                target,
                "references",
                ref_type,
            )
        for citation in re.findall(r"\[@?([A-Za-z0-9_.:-]+)\]", text):
            _add_external_reference(
                enrichment,
                kb_id,
                source,
                artifact,
                citation,
                "references",
                "citation",
            )
        for entity in _entity_candidates(text):
            _add_entity(enrichment, kb_id, source, artifact, entity)
        return enrichment


class SemanticSimilarityPass:
    name = "semantic_similarity"

    def run(
        self,
        artifacts: list[tuple[str, dict[str, Any]]],
    ) -> list[EnrichmentEdge]:
        edges: list[EnrichmentEdge] = []
        for index, (left_id, left) in enumerate(artifacts):
            left_terms = set(left.get("concept_terms") or [])
            if len(left_terms) < 2:
                continue
            for right_id, right in artifacts[index + 1:]:
                right_terms = set(right.get("concept_terms") or [])
                if len(right_terms) < 2:
                    continue
                shared = left_terms & right_terms
                union = left_terms | right_terms
                score = len(shared) / len(union)
                if len(shared) < 2 and score < 0.25:
                    continue
                attrs = {
                    "confidence": round(min(1.0, score), 3),
                    "shared_concepts": sorted(shared)[:12],
                    "enrichment_pass": self.name,
                }
                edges.append(EnrichmentEdge(left_id, right_id, "similar_to", attrs))
                edges.append(EnrichmentEdge(right_id, left_id, "similar_to", attrs))
        return edges


ARTIFACT_NODE_TYPES = {"file", "markdown_note", "document"}


@dataclass(frozen=True)
class _ArtifactRef:
    node_id: str
    source_id: str
    relative_path: str


def resolve_cross_source_edges(
    nodes: list[tuple[str, dict[str, Any]]],
    edges: list[tuple[str, str, str, str | None]],
) -> list[EnrichmentEdge]:
    """Resolve references whose targets live in a *different* source.

    Synapse 21.6B cross-source pass. Deterministic and rule-based (no LLM,
    rule 1). Runs over the *whole* graph after every source's enrichment has
    been applied — references can only resolve once both the referencing and
    the target source are present, so this is a global post-pass (mirroring
    the post-hoc :class:`SemanticSimilarityPass`).

    ``nodes`` is ``(node_id, attrs)`` for every node; ``edges`` is
    ``(source, target, edge_type, reference_type)`` for the artifact ->
    external_reference edges that carry a resolvable target. For each such
    edge whose ``reference`` resolves to an artifact in a *different* source,
    an ``imports`` (python imports) or ``references`` (links) edge is emitted
    from the referencing artifact to the resolved artifact. Edges are upserted
    by the caller, so re-running is idempotent.
    """

    by_path: dict[str, list[_ArtifactRef]] = {}
    for node_id, attrs in nodes:
        if attrs.get("type") not in ARTIFACT_NODE_TYPES:
            continue
        rel = attrs.get("relative_path")
        src = attrs.get("source_id")
        if not rel or not src:
            continue
        by_path.setdefault(_norm_rel(rel), []).append(_ArtifactRef(node_id, src, rel))

    ext_nodes = {
        node_id: attrs for node_id, attrs in nodes if attrs.get("type") == "external_reference"
    }
    out: list[EnrichmentEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for artifact_id, ext_id, edge_type, reference_type in edges:
        ext = ext_nodes.get(ext_id)
        if ext is None:
            continue
        source_id = ext.get("source_id")
        reference = ext.get("reference")
        if not reference or not source_id:
            continue
        targets = _resolve_reference(reference, reference_type, by_path)
        for target in targets:
            if target.node_id == artifact_id:
                continue
            if target.source_id == source_id:
                # Same source: existing intra-source enrichment already covers
                # it; cross-source resolution intentionally only links across.
                continue
            key = (artifact_id, target.node_id, edge_type)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                EnrichmentEdge(
                    artifact_id,
                    target.node_id,
                    edge_type,
                    {
                        "source_id": source_id,
                        "target_source_id": target.source_id,
                        "reference": reference,
                        "reference_type": reference_type,
                        "cross_source": True,
                        "enrichment_pass": "cross_source_resolution",
                    },
                )
            )
    # Deterministic ordering so re-runs / snapshots are byte-stable.
    out.sort(key=lambda e: (e.source, e.target, e.type))
    return out


def _resolve_reference(
    reference: str,
    reference_type: str | None,
    by_path: dict[str, list[_ArtifactRef]],
) -> list[_ArtifactRef]:
    if reference_type == "python_import":
        return _resolve_python_import(reference, by_path)
    if reference_type in {"document_link", "url"}:
        return _resolve_document_link(reference, by_path)
    return []


def _resolve_python_import(
    module: str,
    by_path: dict[str, list[_ArtifactRef]],
) -> list[_ArtifactRef]:
    parts = [part for part in module.replace("\\", "/").split(".") if part]
    if not parts:
        return []
    base = "/".join(parts)
    candidates = (f"{base}.py", f"{base}/__init__.py")
    for candidate in candidates:
        matches = _match_suffix(candidate, by_path)
        if matches:
            return matches
    return []


def _resolve_document_link(
    target: str,
    by_path: dict[str, list[_ArtifactRef]],
) -> list[_ArtifactRef]:
    if target.startswith(("http://", "https://", "mailto:")):
        return []
    cleaned = target.split("#", 1)[0].split("?", 1)[0].strip()
    if not cleaned:
        return []
    cleaned = cleaned.lstrip("./")
    norm = _norm_rel(cleaned)
    direct = by_path.get(norm)
    if direct:
        return direct
    # Obsidian-style wiki link without extension -> try common doc suffixes.
    if "." not in Path(norm).name:
        for suffix in (".md", ".txt"):
            matches = by_path.get(norm + suffix)
            if matches:
                return matches
    return _match_suffix(norm, by_path)


def _match_suffix(
    candidate: str,
    by_path: dict[str, list[_ArtifactRef]],
) -> list[_ArtifactRef]:
    candidate = _norm_rel(candidate)
    direct = by_path.get(candidate)
    if direct:
        return direct
    matches: list[_ArtifactRef] = []
    needle = "/" + candidate
    for path, refs in by_path.items():
        if path == candidate or path.endswith(needle):
            matches.extend(refs)
    matches.sort(key=lambda ref: (ref.source_id, ref.relative_path, ref.node_id))
    return matches


def _norm_rel(value: str) -> str:
    return value.replace("\\", "/").strip("/").lower()


def artifact_text(artifact: ParsedArtifact) -> str:
    return "\n\n".join(chunk.text for chunk in artifact.chunks)


def _base_concepts(
    kb_id: str,
    source: SourceConfig,
    artifact: ParsedArtifact,
    text: str,
) -> ArtifactEnrichment:
    enrichment = ArtifactEnrichment()
    for concept in _concept_candidates(text, artifact.relative_path):
        _add_concept(enrichment, kb_id, source, artifact, concept, 1.0)
    return enrichment


def _add_symbol(
    enrichment: ArtifactEnrichment,
    kb_id: str,
    source: SourceConfig,
    artifact: ParsedArtifact,
    name: str,
    symbol_type: str,
    start_line: int,
    end_line: int,
) -> None:
    symbol_id = _node_id(
        "symbol",
        kb_id,
        source.name,
        artifact.relative_path,
        f"{name}-{start_line}",
    )
    attrs = {
        "source_id": source.name,
        "artifact_id": artifact.id,
        "relative_path": artifact.relative_path,
        "language": "python",
        "symbol_type": symbol_type,
        "name": name,
        "qualified_name": name,
        "start_line": start_line,
        "end_line": end_line,
        "enrichment_pass": "code",
    }
    enrichment.nodes.append(EnrichmentNode(symbol_id, "symbol", name, attrs))
    enrichment.edges.append(
        EnrichmentEdge(artifact.id, symbol_id, "mentions", {"source_id": source.name})
    )
    enrichment.edges.append(
        EnrichmentEdge(symbol_id, artifact.id, "derived_from", {"source_id": source.name})
    )
    enrichment.terms.append(_term(artifact, symbol_id, "symbol", name, 3.0))
    enrichment.symbols.append(
        {
            "id": symbol_id,
            "artifact_id": artifact.id,
            "source_id": source.name,
            "language": "python",
            "symbol_type": symbol_type,
            "name": name,
            "qualified_name": name,
            "start_line": start_line,
            "end_line": end_line,
            "signature": None,
            "docstring_summary": None,
        }
    )
    for concept in _identifier_terms(name):
        _add_concept(enrichment, kb_id, source, artifact, concept, 1.5)


def _add_call(
    enrichment: ArtifactEnrichment,
    kb_id: str,
    source: SourceConfig,
    artifact: ParsedArtifact,
    name: str,
    line: int,
) -> None:
    symbol_id = _node_id("symbol", kb_id, source.name, "call", name)
    attrs = {
        "source_id": source.name,
        "symbol_type": "call_target",
        "name": name,
        "language": "python",
        "line": line,
        "enrichment_pass": "code",
    }
    enrichment.nodes.append(EnrichmentNode(symbol_id, "symbol", name, attrs))
    enrichment.edges.append(
        EnrichmentEdge(
            artifact.id,
            symbol_id,
            "calls",
            {"source_id": source.name, "line": line},
        )
    )
    enrichment.terms.append(_term(artifact, symbol_id, "symbol", name, 1.5))


def _add_external_reference(
    enrichment: ArtifactEnrichment,
    kb_id: str,
    source: SourceConfig,
    artifact: ParsedArtifact,
    target: str,
    edge_type: str,
    reference_type: str,
) -> None:
    label = _reference_label(target)
    node_id = _node_id("external_reference", kb_id, source.name, reference_type, target)
    attrs = {
        "source_id": source.name,
        "artifact_id": artifact.id,
        "reference": target,
        "reference_type": reference_type,
        "enrichment_pass": "reference_extraction",
    }
    enrichment.nodes.append(EnrichmentNode(node_id, "external_reference", label, attrs))
    enrichment.edges.append(
        EnrichmentEdge(
            artifact.id,
            node_id,
            edge_type,
            {"source_id": source.name, "reference_type": reference_type},
        )
    )
    enrichment.terms.append(_term(artifact, node_id, "external_reference", label, 1.25))


def _add_concept(
    enrichment: ArtifactEnrichment,
    kb_id: str,
    source: SourceConfig,
    artifact: ParsedArtifact,
    concept: str,
    weight: float,
) -> None:
    normalized = _normalize_concept(concept)
    if not normalized:
        return
    node_id = _node_id("concept", kb_id, source.name, normalized)
    attrs = {
        "source_id": source.name,
        "enrichment_pass": "concept_extraction",
    }
    enrichment.nodes.append(EnrichmentNode(node_id, "concept", normalized, attrs))
    enrichment.edges.append(
        EnrichmentEdge(artifact.id, node_id, "mentions", {"source_id": source.name})
    )
    enrichment.edges.append(
        EnrichmentEdge(node_id, artifact.id, "derived_from", {"source_id": source.name})
    )
    enrichment.terms.append(_term(artifact, node_id, "concept", normalized, weight))
    enrichment.concept_terms.add(normalized)


def _add_entity(
    enrichment: ArtifactEnrichment,
    kb_id: str,
    source: SourceConfig,
    artifact: ParsedArtifact,
    entity: str,
) -> None:
    normalized = entity.strip()
    if not normalized or normalized.lower() in STOPWORDS:
        return
    node_id = _node_id("entity", kb_id, source.name, normalized)
    attrs = {
        "source_id": source.name,
        "artifact_id": artifact.id,
        "entity_type": "named_mention",
        "enrichment_pass": "entity_extraction",
    }
    enrichment.nodes.append(EnrichmentNode(node_id, "entity", normalized, attrs))
    enrichment.edges.append(
        EnrichmentEdge(artifact.id, node_id, "mentions", {"source_id": source.name})
    )
    enrichment.edges.append(
        EnrichmentEdge(node_id, artifact.id, "derived_from", {"source_id": source.name})
    )
    enrichment.terms.append(_term(artifact, node_id, "entity", normalized, 1.5))


def _concept_candidates(text: str, relative_path: str) -> set[str]:
    # Synapse 21.6B: normalize + singularize candidates up front so plural and
    # singular surface forms count toward the same concept (e.g. "systems" and
    # "system" both increment one bucket) and collapse to one node.
    tokens = [_normalize_concept(token) for token in _split_identifier(Path(relative_path).stem)]
    normalized_tokens = [token for token in tokens if token and token not in STOPWORDS]
    words = [
        _normalize_concept(match)
        for match in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text)
    ]
    words = [word for word in words if word and word not in STOPWORDS]
    counts = Counter(words)
    concepts = set(normalized_tokens)
    concepts.update(word for word, count in counts.items() if count >= 2)
    concepts.update(_keyword_bigrams(words))
    return {concept for concept in concepts if concept and concept not in STOPWORDS}


def _keyword_bigrams(words: list[str]) -> set[str]:
    concepts: set[str] = set()
    for left, right in zip(words, words[1:], strict=False):
        if left in STOPWORDS or right in STOPWORDS:
            continue
        if left == right:
            continue
        concepts.add(f"{left} {right}")
    return concepts


def _entity_candidates(text: str) -> set[str]:
    candidates = set(re.findall(r"\b[A-Z][A-Za-z0-9]+(?:[A-Z][A-Za-z0-9]+)+\b", text))
    candidates.update(re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}\b", text))
    return candidates


def _markdown_links(text: str) -> set[str]:
    links = set(re.findall(r"\[[^\]]+\]\(([^)]+)\)", text))
    links.update(re.findall(r"\[\[([^\]]+)\]\]", text))
    links.update(re.findall(r"https?://[^\s)>\]]+", text))
    return {link.strip() for link in links if link.strip()}


def _assignment_targets(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _term(
    artifact: ParsedArtifact,
    node_id: str,
    node_type: str,
    value: str,
    weight: float,
) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "source_id": artifact.source_id,
        "node_id": node_id,
        "node_type": node_type,
        "term": value,
        "normalized_term": _normalize_term(value),
        "weight": weight,
    }


def _identifier_terms(value: str) -> set[str]:
    terms = {_normalize_term(value)}
    parts = [_normalize_term(part) for part in _split_identifier(value)]
    parts = [part for part in parts if part and part not in STOPWORDS]
    terms.update(parts)
    if len(parts) > 1:
        terms.add(" ".join(parts))
    return {term for term in terms if term}


def _split_identifier(value: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return re.split(r"[^A-Za-z0-9]+", spaced)


def _clean_inline(value: str) -> str:
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    return value.strip(" #\t")


def _normalize_term(value: str) -> str:
    parts = [_slug_part(part) for part in _split_identifier(value)]
    return " ".join(part for part in parts if part)


def _singularize_word(word: str) -> str:
    """Lemma-light singularization of a single lowercased word.

    Deterministic, pure-python, no NLP dependency (rule 1 / "no new deps").
    Rules, applied to words length >= 4 not in ``SINGULARIZE_STOPLIST``:
    ``…ies`` -> ``…y`` (libraries -> library), ``…(s|x|z|ch|sh)es`` -> drop
    ``es`` (boxes -> box, classes is stoplisted), other ``…s`` -> drop ``s``
    (systems -> system). Words ending in ``ss`` are left untouched. The
    transform is idempotent: singularizing an already-singular word is a
    no-op, so a concept's stable id derivation stays consistent across syncs.
    """

    if len(word) < 4 or word in SINGULARIZE_STOPLIST or word.endswith("ss"):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("es") and word[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def _normalize_concept(value: str) -> str:
    """Normalize a concept surface term before node creation (Synapse 21.6B).

    Lowercase + slug via ``_normalize_term`` (existing behavior), then
    singularize each token so "Systems"/"system"/"systems" collapse to a
    single ``concept`` node. Because ``_node_id`` derives the concept id from
    this normalized term, the id stays deterministic for a given input.
    """

    normalized = _normalize_term(value)
    if not normalized:
        return ""
    return " ".join(_singularize_word(token) for token in normalized.split(" "))


def _reference_label(value: str) -> str:
    parsed = urlparse(value)
    if parsed.netloc:
        return parsed.netloc + parsed.path
    return value


def _node_id(prefix: str, *parts: str) -> str:
    return prefix + ":" + ":".join(_slug_part(part) for part in parts if part)


def _slug_part(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    return value.strip("-._") or "item"
