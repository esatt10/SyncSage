"""Planning a shard split (Phase 35.4).

The capacity work measured where one region stops being sensible; this turns
that into a concrete proposal — *these sources go to region 1, those to region
2* — with the configs to run them.

**Shards are whole sources.** That is the load-bearing decision, and it is not
about implementation convenience. Retrieval quality depends on related content
living in one graph: `resolve_cross_source_edges` links an import or a Markdown
link to the file it names, and `get_graph_neighbors` walks those links. Splitting
one repository across two regions severs exactly the edges that make the graph
worth having, while splitting *between* repositories costs nothing, because
those edges were never going to exist.

Hashing paths across shards — the obvious mechanical answer, and the one the
plan originally called for — would do the opposite: it distributes evenly and
severs every edge. Even balance is the wrong objective when the thing being
balanced is a graph.

So this is bin-packing over sources, greedy largest-first, which is the classic
LPT heuristic and lands within 4/3 of optimal. Optimal packing is NP-hard and
the input is a size estimate, so a guaranteed-close answer from a
one-line-explainable rule beats an exact answer to the wrong question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pheasant.capacity import GRAPH_SHARE_OF_RSS, NODES_PER_FILE, RSS_BYTES_PER_NODE

# Re-exported, not redefined. These were a second copy of the same three
# numbers, so `pheasant shard plan` and `pheasant scan` could be edited apart
# and quietly disagree about how big the same corpus is — which is the whole
# reason :mod:`pheasant.capacity` exists (Phase 35.7).
__all__ = [
    "GRAPH_SHARE_OF_RSS",
    "NODES_PER_FILE",
    "RSS_BYTES_PER_NODE",
    "Shard",
    "SourceSize",
    "plan_shards",
    "render_plan",
]


@dataclass
class SourceSize:
    """What one source contributes. ``files`` is all the planner needs."""

    name: str
    files: int
    bytes_: int = 0

    @property
    def nodes(self) -> int:
        return int(self.files * NODES_PER_FILE)


@dataclass
class Shard:
    index: int
    sources: list[SourceSize] = field(default_factory=list)

    @property
    def files(self) -> int:
        return sum(source.files for source in self.sources)

    @property
    def nodes(self) -> int:
        return sum(source.nodes for source in self.sources)

    @property
    def rss_bytes(self) -> int:
        return int(self.nodes * RSS_BYTES_PER_NODE / GRAPH_SHARE_OF_RSS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "shard": self.index,
            "region_name": f"shard-{self.index}",
            "sources": [source.name for source in self.sources],
            "files": self.files,
            "nodes": self.nodes,
            "projected_rss_bytes": self.rss_bytes,
            "projected_rss_gb": round(self.rss_bytes / 1e9, 2),
            "recommended_memory": _recommend_memory(self.rss_bytes),
        }


def _recommend_memory(rss_bytes: int) -> str:
    """A container limit with headroom, rounded to something a human types.

    1.5x the projection, because the estimate is derived from a file count and
    a corpus of unusually large documents will exceed it — and being 50% over
    on a request is cheap where being 10% under is an OOM kill mid-sync.
    """

    gib = rss_bytes * 1.5 / (1024**3)
    for candidate in (0.5, 1, 2, 4, 6, 8, 12, 16, 24, 32, 48, 64):
        if gib <= candidate:
            return f"{candidate:g}Gi"
    return f"{int(gib) + 1}Gi"


def plan_shards(
    sources: list[SourceSize],
    *,
    shards: int | None = None,
    max_nodes_per_shard: int = 1_500_000,
) -> dict[str, Any]:
    """Propose a split.

    With ``shards`` given, packs into exactly that many. Otherwise picks the
    smallest count that keeps every shard under ``max_nodes_per_shard`` —
    which defaults to ``graph.max_nodes``, so the planner and the runtime
    warning agree about what "too big" means.
    """

    usable = [source for source in sources if source.files > 0]
    if not usable:
        return {"shards": [], "warnings": ["no sources with any files to plan"]}

    total_nodes = sum(source.nodes for source in usable)
    if shards is None:
        needed = -(-total_nodes // max(1, max_nodes_per_shard))  # ceil
        shards = max(1, needed)
    shards = max(1, min(int(shards), len(usable)))

    # Largest-first (LPT). Placing big sources while every bin is still empty
    # is what keeps the biggest one from landing on an already-full shard,
    # which is the failure mode of first-fit in source order.
    ordered = sorted(usable, key=lambda source: (-source.files, source.name))
    bins = [Shard(index=position + 1) for position in range(shards)]
    for source in ordered:
        smallest = min(bins, key=lambda shard: (shard.nodes, shard.index))
        smallest.sources.append(source)

    warnings: list[str] = []
    for shard in bins:
        if shard.nodes > max_nodes_per_shard:
            # One source can exceed the budget on its own, and no arrangement
            # of whole sources fixes that. Say so rather than proposing a
            # split that cannot honour it.
            biggest = max(shard.sources, key=lambda source: source.files)
            warnings.append(
                f"shard {shard.index} holds {shard.nodes:,} nodes, over the "
                f"{max_nodes_per_shard:,} budget — '{biggest.name}' alone is "
                f"{biggest.nodes:,}. Give that region more memory, or narrow the "
                f"source with include/exclude or a depth cap."
            )
    empty = [shard.index for shard in bins if not shard.sources]
    if empty:
        warnings.append(
            f"shard(s) {empty} would be empty: there are only {len(usable)} sources "
            "to spread, so fewer shards is the honest answer."
        )

    return {
        "total_files": sum(source.files for source in usable),
        "total_nodes": total_nodes,
        "shard_count": shards,
        "max_nodes_per_shard": max_nodes_per_shard,
        "shards": [shard.as_dict() for shard in bins if shard.sources],
        "warnings": warnings,
    }


def render_plan(plan: dict[str, Any]) -> str:
    """The human-readable form printed by ``pheasant shard plan``."""

    lines: list[str] = []
    if not plan.get("shards"):
        return "Nothing to plan: no sources with any files.\n"
    lines.append(
        f"{plan['total_files']:,} files across {len(plan['shards'])} region(s) "
        f"(~{plan['total_nodes']:,} graph nodes)"
    )
    lines.append("")
    for shard in plan["shards"]:
        lines.append(
            f"  {shard['region_name']}: {shard['files']:,} files, "
            f"~{shard['nodes']:,} nodes, ~{shard['projected_rss_gb']:.1f} GB RSS "
            f"-> request {shard['recommended_memory']}"
        )
        for name in shard["sources"]:
            lines.append(f"      - {name}")
    if plan.get("warnings"):
        lines.append("")
        for warning in plan["warnings"]:
            lines.append(f"  ! {warning}")
    lines.append("")
    lines.append(
        "Each region is an ordinary pheasant container with its own /state. "
        "Point them all at one router (synapse.router_url) and it fans out "
        "across them. See docs/how-to/capacity-planning.md."
    )
    return "\n".join(lines) + "\n"
