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
    normalized = _normalize_term(concept)
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
    tokens = [_normalize_term(token) for token in _split_identifier(Path(relative_path).stem)]
    normalized_tokens = [token for token in tokens if token and token not in STOPWORDS]
    words = [
        _normalize_term(match)
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
