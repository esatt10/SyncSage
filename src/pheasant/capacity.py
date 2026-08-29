"""The measured capacity model (Phase 35.7).

Every sizing question — how much RAM, how much disk, how long will the first
index take, when do I shard — comes down to a handful of coefficients. This
module is the one place they live, so `pheasant scan`, `pheasant shard plan`
and the documentation cannot disagree with each other.

**These are measurements, not estimates.** Each constant records where it came
from, and `python -m pheasant.sync.benchmark --mode capacity` reproduces the
run that produced it. Re-run it on your own hardware: the *shape* holds (all
of this is linear in file count), the constants move.

A projection from a file count is necessarily coarse. It is still worth having
because the alternative is not a better number, it is no number — and the
failure it prevents is specific: pointing a source at something large,
watching a container get OOM-killed mid-sync, and reading the restart as a bug
rather than as a capacity limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Graph nodes per file. From the live 2,132-file demo corpus (13,503 nodes).
#:
#: The synthetic sweep measures **3.0** and is *not* used here, which is worth
#: saying out loud: its fixture is uniform markdown with no links, no headings
#: and no cross-references, so it produces a file node plus chunk nodes and
#: nothing else. A real corpus adds symbols, entities and headings. Taking the
#: synthetic number would have halved every memory projection on the strength
#: of a fixture that was never meant to be representative.
NODES_PER_FILE = 6.3

#: Process RSS per graph node, measured flat across four scales in
#: `pheasant.graph.capacity` (2k → 250k files).
RSS_BYTES_PER_NODE = 2400

#: The graph is roughly this share of process RSS; the rest is the
#: interpreter, the search stores and whatever is being served.
GRAPH_SHARE_OF_RSS = 0.6

#: State-directory bytes per *corpus* byte. **Measured 3.95–4.09** across the
#: 500→8,000-file sweep, converging on ~3.97; the value below is the round
#: number that covers it. Dominated by SQLite, which stores every chunk's text
#: a second time plus its FTS5 index — so this scales with *content*, not with
#: file count, which is why :func:`project` takes bytes and not just files.
#:
#: Vectors are excluded: a per-dimension cost that exists only when embeddings
#: are on, added separately below.
STATE_BYTES_PER_CORPUS_BYTE = 4.0

#: Vector-store bytes per chunk per dimension, at float32.
VECTOR_BYTES_PER_CHUNK_PER_DIM = 4

#: Corpus bytes per chunk. Measured at a dead-flat **2,728** across every
#: point in the sweep — but that is this fixture's file size (5.4 KB → two
#: chunks) as much as it is the chunker. It rises toward
#: ``chunking.max_chars`` for a corpus of larger documents, so treat it as a
#: floor. Bytes-per-chunk rather than chunks-per-file because the former is
#: the stable one: file size varies far more between corpora than chunk size
#: does.
BYTES_PER_CHUNK = 2728

#: Seconds per 1,000 files for a full index, one worker, embeddings off, on
#: the machine that ran the sweep. **Measured 2.81–3.28** after the O(N²) fix
#: below; the high end is used because it is the large-corpus figure.
#:
#: Before that fix this was not a constant at all — it went 3.85 → 20.58 over
#: 500 → 8,000 files, because ``chunks_fts.artifact_id`` is UNINDEXED and the
#: per-artifact DELETE scanned the whole growing table. Anyone quoting a
#: seconds-per-file figure from a small corpus was extrapolating a curve as
#: though it were a line.
SECONDS_PER_1K_FILES = 3.3

#: Multiplier on the above when embeddings are enabled with a real provider.
#: **Deliberately not measured**: a network embedder's throughput is the
#: provider's rate limit, not pheasant's, and a number derived from the stub
#: would describe nothing. Recorded as a documented unknown rather than a
#: fabricated constant — see `docs/how-to/capacity-planning.md`.
EMBEDDING_TIME_MULTIPLIER: float | None = None

#: Container sizes an operator would actually type.
_MEMORY_LADDER = (0.5, 1, 2, 4, 6, 8, 12, 16, 24, 32, 48, 64)


@dataclass(frozen=True)
class Projection:
    """What one corpus is expected to cost."""

    files: int
    corpus_bytes: int
    nodes: int
    chunks: int
    rss_bytes: int
    state_bytes: int
    index_seconds: float
    recommended_memory: str
    shards: int
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "corpus_bytes": self.corpus_bytes,
            "corpus_mb": round(self.corpus_bytes / (1024 * 1024), 1),
            "graph_nodes": self.nodes,
            "chunks": self.chunks,
            "projected_rss_bytes": self.rss_bytes,
            "projected_rss_gb": round(self.rss_bytes / 1e9, 2),
            "projected_state_bytes": self.state_bytes,
            "projected_state_gb": round(self.state_bytes / 1e9, 2),
            "projected_index_seconds": round(self.index_seconds, 1),
            "projected_index_minutes": round(self.index_seconds / 60, 1),
            "recommended_memory": self.recommended_memory,
            "recommended_shards": self.shards,
            "warnings": list(self.warnings),
        }


def recommend_memory(rss_bytes: int) -> str:
    """A container limit with headroom, rounded to something a human types.

    1.5x the projection, because the estimate comes from a file count and a
    corpus of unusually large documents will exceed it — and being 50% over on
    a request is cheap where being 10% under is an OOM kill mid-sync.
    """

    gib = rss_bytes * 1.5 / (1024**3)
    for candidate in _MEMORY_LADDER:
        if gib <= candidate:
            return f"{candidate:g}Gi"
    return f"{int(gib) + 1}Gi"


def project(
    files: int,
    corpus_bytes: int = 0,
    *,
    embedding_dimensions: int = 0,
    max_nodes_per_shard: int = 1_500_000,
    seconds_per_1k_files: float = SECONDS_PER_1K_FILES,
) -> Projection:
    """Project cost from a file count and (optionally) a byte count.

    ``corpus_bytes`` is what `pheasant scan` already reports, and it is what
    makes the disk number meaningful: state size tracks *content*, while RSS
    and node count track file count. Passing 0 skips the disk projection
    rather than inventing one.
    """

    files = max(0, int(files))
    corpus_bytes = max(0, int(corpus_bytes))
    nodes = int(files * NODES_PER_FILE)
    # From bytes where we have them: a corpus of 100 large files and one of
    # 100 small ones differ by an order of magnitude in chunks and not at all
    # in file count. Falling back to one chunk per file is the floor, since
    # even an empty document produces one.
    chunks = int(corpus_bytes / BYTES_PER_CHUNK) if corpus_bytes else files
    chunks = max(chunks, files)
    rss = int(nodes * RSS_BYTES_PER_NODE / GRAPH_SHARE_OF_RSS)

    state = int(corpus_bytes * STATE_BYTES_PER_CORPUS_BYTE) if corpus_bytes else 0
    if embedding_dimensions > 0:
        state += chunks * embedding_dimensions * VECTOR_BYTES_PER_CHUNK_PER_DIM

    seconds = files / 1000.0 * seconds_per_1k_files
    shards = max(1, -(-nodes // max(1, max_nodes_per_shard)))  # ceil

    warnings: list[str] = []
    if shards > 1:
        warnings.append(
            f"{nodes:,} nodes exceeds the {max_nodes_per_shard:,}-node budget for one "
            f"region; `pheasant shard plan` proposes a {shards}-way split."
        )
    if embedding_dimensions > 0 and EMBEDDING_TIME_MULTIPLIER is None:
        warnings.append(
            "index time excludes embedding: a network embedder's throughput is the "
            "provider's rate limit, not pheasant's, and is not projected here."
        )
    return Projection(
        files=files,
        corpus_bytes=corpus_bytes,
        nodes=nodes,
        chunks=chunks,
        rss_bytes=rss,
        state_bytes=state,
        index_seconds=seconds,
        recommended_memory=recommend_memory(rss),
        shards=shards,
        warnings=tuple(warnings),
    )


#: Bytes one interaction row costs in ``/state``, including its indexes.
#:
#: Measured on SQLite with a WAL checkpoint, over a realistic row: a query, a
#: criteria object, five result ids and five paths, and the usual attributes.
#: 892 B at 5k rows, 937 at 20k, 949 at 50k --- flat across a 10x range, so the
#: number is the row and not the page overhead of a small file. Rounded up.
BYTES_PER_INTERACTION_EVENT = 950

#: The same row when a chat answer rides along, at the 4,000-character default
#: cap. 2,124 B at 5k rows and 2,169 at 20k.
#:
#: The ratio is the point: **a chat turn costs about 2.3x a search**, which is
#: why `max_answer_chars` exists and why `0` turns answers off entirely. A
#: region whose traffic is mostly chat should size for this row, not the other
#: one.
BYTES_PER_CHAT_INTERACTION = 2200


def observation_state_bytes(
    events_per_day: int,
    retention_days: int,
    *,
    chat_share: float = 0.0,
) -> int:
    """How much ``/state`` an enabled observation plane will hold.

    Retention-bounded rather than corpus-bounded, which makes it the one part
    of ``/state`` that grows with *traffic* --- the reason the log tier scales
    on its own axis. ``chat_share`` is the fraction of observations that carry
    an answer.
    """

    share = min(1.0, max(0.0, float(chat_share)))
    per_event = BYTES_PER_INTERACTION_EVENT * (1.0 - share) + BYTES_PER_CHAT_INTERACTION * share
    return int(max(0, events_per_day) * max(0, retention_days) * per_event)


def fleet_for(files: int) -> dict[str, Any]:
    """Replica counts for a corpus of this size.

    Deliberately coarse, and derived rather than tabulated: api replicas track
    *request* traffic, which this model knows nothing about, so the number
    here is a floor for availability (two, so a rollout is not an outage) that
    grows only where the graph is large enough that one replica's memory is
    the constraint. Workers track indexing throughput, which is what the file
    count actually predicts.
    """

    projection = project(files)
    shards = projection.shards
    # One worker per ~25k files, bounded: past a point the coordinator's
    # commit path is the bottleneck, not preparation, and the 2026-08-13
    # benchmark measured 8 file workers buying 1.113x.
    workers = max(0, min(8, files // 25_000)) * shards
    return {
        "shards": shards,
        "indexers": shards,
        "api_replicas": 2 if files < 250_000 else 3,
        "workers": workers,
        # The log tier scales on request traffic, which a file count cannot
        # predict at all -- so this is a floor for a region that has enabled
        # observation, not a projection. One replica handles a great deal: a
        # batch is 500 observations and the work per batch is a multi-row
        # insert. Scale it on `pheasant_log_queue_depth`, never on this.
        "loggers": 1,
        "memory_per_region": projection.recommended_memory,
        "single_container_is_enough": shards == 1 and files < 150_000,
    }
