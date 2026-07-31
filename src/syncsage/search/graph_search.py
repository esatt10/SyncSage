from __future__ import annotations

import re
from typing import Any

from syncsage.graph.simple import SimpleMultiDiGraph

# Attribute keys that carry no useful search signal (timestamps, opaque hashes,
# foreign keys). Skipping them keeps attribute matches meaningful instead of
# surfacing every node on a stray hex match.
_SKIP_KEYS = {
    "created_at",
    "updated_at",
    "knowledge_base_id",
    "hash",
    "text_hash",
    "sha256",
    "id",
    "key",
}

_STRUCTURED_KEYS = {"label", "name", "qualified_name", "type"}


def search_graph(
    graph: SimpleMultiDiGraph,
    query: str,
    max_results: int = 10,
    source_name: str | None = None,
    include_relationships: bool = True,
) -> list[dict[str, Any]]:
    """Search across graph nodes, relationships and their attributes.

    Nodes are scored on their label, name, type and remaining attribute values;
    relationships are matched on edge type, endpoint labels and edge attributes.
    Returns result items in the same shape the SQLite store emits so callers can
    merge text and graph hits uniformly.
    """

    q = query.strip().lower()
    tokens = _tokens(query)
    if not q and not tokens:
        return []

    node_hits: list[tuple[float, dict[str, Any]]] = []
    for node_id, attrs in graph.nodes(data=True):
        if source_name and attrs.get("source_id") != source_name:
            continue
        score, field = _node_score(attrs, tokens, q)
        if score <= 0:
            continue
        node_hits.append((score, _node_result(node_id, attrs, score, field)))

    node_hits.sort(key=lambda item: (-item[0], str(item[1].get("title") or "")))
    results = [hit for _, hit in node_hits[:max_results]]

    if include_relationships and len(results) < max_results:
        rel_hits = _relationship_hits(graph, tokens, q, source_name)
        rel_hits.sort(key=lambda item: -item[0])
        for _, hit in rel_hits[: max_results - len(results)]:
            results.append(hit)

    for rank, item in enumerate(results, start=1):
        item["rank"] = rank
    return results


def _relationship_hits(
    graph: SimpleMultiDiGraph,
    tokens: list[str],
    q: str,
    source_name: str | None,
) -> list[tuple[float, dict[str, Any]]]:
    hits: list[tuple[float, dict[str, Any]]] = []
    for (source, target), edge_map in graph.edges():
        source_label = str(graph.nodes.get(source, {}).get("label") or source)
        target_label = str(graph.nodes.get(target, {}).get("label") or target)
        for _key, data in edge_map.items():
            if source_name and data.get("source_id") != source_name:
                continue
            etype = str(data.get("type") or "related")
            type_score = _field_score(etype, tokens, q, 0.65, 0.55, 0.45)
            endpoint_score = max(
                _field_score(source_label, tokens, q, 0.5, 0.4, 0.3),
                _field_score(target_label, tokens, q, 0.5, 0.4, 0.3),
            )
            attr_score = 0.0
            for attr_key, value in data.items():
                if attr_key in _SKIP_KEYS or attr_key == "type":
                    continue
                attr_score = max(attr_score, _field_score(_stringify(value), tokens, q, 0.5, 0.35, 0.3))
            score = max(type_score, endpoint_score, attr_score)
            if score <= 0:
                continue
            hits.append(
                (
                    score,
                    {
                        "kind": "relationship",
                        "node_id": source,
                        "type": "relationship",
                        "edge_type": etype,
                        "source": source,
                        "target": target,
                        "source_label": source_label,
                        "target_label": target_label,
                        "title": f"{source_label} —{etype}→ {target_label}",
                        "label": etype,
                        "score": round(score, 3),
                        "reason": "Graph relationship match",
                        "summary": f"{source_label} {etype} {target_label}",
                    },
                )
            )
    return hits


def _node_result(node_id: str, attrs: dict[str, Any], score: float, field: str | None) -> dict[str, Any]:
    label = str(attrs.get("label") or node_id)
    return {
        "kind": "node",
        "node_id": node_id,
        "type": attrs.get("type"),
        "title": label,
        "label": label,
        "relative_path": attrs.get("relative_path"),
        "source_id": attrs.get("source_id"),
        "score": round(score, 3),
        "match": field,
        "reason": f"Graph node match on {field}" if field else "Graph node match",
        "summary": _node_summary(attrs),
        "provenance": attrs.get("provenance")
        or {
            "source_id": attrs.get("source_id"),
            "relative_path": attrs.get("relative_path"),
            "path": attrs.get("path"),
        },
    }


def _node_summary(attrs: dict[str, Any]) -> str:
    summary = attrs.get("summary")
    if summary:
        return str(summary)[:240]
    relative = attrs.get("relative_path")
    if relative:
        return str(relative)
    return str(attrs.get("label") or "")[:240]


def _node_score(attrs: dict[str, Any], tokens: list[str], q: str) -> tuple[float, str | None]:
    candidates: list[tuple[float, str]] = []
    label = str(attrs.get("label") or "")
    if label:
        candidates.append((_field_score(label, tokens, q, 1.0, 0.9, 0.6), "label"))
    name = str(attrs.get("name") or attrs.get("qualified_name") or "")
    if name:
        candidates.append((_field_score(name, tokens, q, 0.95, 0.85, 0.55), "name"))
    ntype = str(attrs.get("type") or "")
    if ntype and any(token in ntype.lower() for token in tokens):
        candidates.append((0.5, "type"))
    for key, value in attrs.items():
        if key in _SKIP_KEYS or key in _STRUCTURED_KEYS:
            continue
        text = _stringify(value)
        if not text:
            continue
        score = _field_score(text, tokens, q, 0.7, 0.45, 0.3)
        if score > 0:
            candidates.append((score, f"attribute:{key}"))
    if not candidates:
        return 0.0, None
    return max(candidates, key=lambda item: item[0])


def _field_score(
    text: str,
    tokens: list[str],
    q: str,
    base_exact: float,
    base_all: float,
    base_any: float,
) -> float:
    if not text:
        return 0.0
    low = text.lower()
    if q and low == q:
        return base_exact
    if q and q in low:
        return (base_exact + base_all) / 2
    if tokens and all(token in low for token in tokens):
        return base_all
    if tokens and any(token in low for token in tokens):
        return base_any
    return 0.0


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return " ".join(_stringify(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_stringify(item) for item in value.values())
    return str(value)


def _tokens(query: str) -> list[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9_]{2,}", query.lower()):
        tokens.add(raw)
        for part in re.split(r"[_\W]+", raw):
            if len(part) > 1:
                tokens.add(part)
    return sorted(tokens)
