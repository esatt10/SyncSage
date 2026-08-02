from __future__ import annotations

import re

from syncsage.persistence.state_store import StateStore

# --- ranking -------------------------------------------------------------
#
# BM25 column weights, positionally matching the chunks_fts schema:
#   chunk_id, source_id, artifact_id (UNINDEXED), title, path, heading_path, text
#
# `title` holds the file's BASENAME (see StateStore._fts_title); `path` is the
# full relative path. Weighting them above `text` is what makes "readme" find
# the file *named* README rather than the file that happens to say "readme"
# most often in a short body — untuned, a filename match was worth exactly one
# body word, and BM25's length normalization then ranked by body brevity.
_BM25_WEIGHTS = "0.0, 0.0, 0.0, 8.0, 3.0, 2.0, 1.0"

# Structural priors, applied as a DIVISOR on the (negative) BM25 cost, so they
# scale a match rather than displacing it: a strong deep hit still beats a weak
# shallow one, but ties break toward the more central file. An additive penalty
# was tried first and measurably hurt legitimately-deep code (a checkpoint
# implementation went from rank 34 to 43) while fixing the same document
# queries, so proportional it is.
#
#   depth  — 412 files in the demo corpus are named README.md, so their
#            basenames are textually identical and BM25 *cannot* separate them.
#            The signal that the repository's own README is the one you meant
#            is structural, not lexical: it sits at the root.
#   tests  — "where is X implemented" should not return X's test suite first.
#            Both the lexical and the vector arm ranked tests/ above the
#            implementation for every code query measured.
#   samples— same, one notch softer: sample code is often a legitimate answer.
_DEPTH_PRIOR = 0.05
_TEST_PRIOR = 0.60
_SAMPLE_PRIOR = 0.30
_STRUCTURAL_PRIOR = f"""(
    1.0
    + {_DEPTH_PRIOR} * (length(artifacts.relative_path)
                        - length(replace(artifacts.relative_path, '/', '')))
    + CASE WHEN artifacts.relative_path LIKE '%/tests/%'
             OR artifacts.relative_path LIKE 'tests/%'
             OR artifacts.relative_path LIKE '%/test_%'
             OR artifacts.relative_path LIKE 'test_%'
             OR artifacts.relative_path LIKE '%_test.%'
           THEN {_TEST_PRIOR} ELSE 0.0 END
    + CASE WHEN artifacts.relative_path LIKE '%/samples/%'
             OR artifacts.relative_path LIKE 'samples/%'
             OR artifacts.relative_path LIKE '%/examples/%'
             OR artifacts.relative_path LIKE 'examples/%'
           THEN {_SAMPLE_PRIOR} ELSE 0.0 END
)"""


class SearchStore:
    def __init__(self, state: StateStore):
        self.state = state

    def search(
        self,
        query: str,
        source_name: str | None = None,
        max_results: int = 10,
    ) -> list[dict]:
        # Natural-language queries ("where does the X service run") must not
        # fall to FTS5's implicit-AND — a single unmatched stopword would
        # zero out the whole query (found by the Step-33.4 memory benchmark).
        # OR the sanitized tokens instead and let BM25 rank by token rarity;
        # quoting each token also neutralizes FTS5 query-syntax characters.
        tokens = _query_tokens(query)
        match_expr = " OR ".join(f'"{token}"' for token in tokens) if tokens else query
        params: list[object] = [match_expr]
        where = "chunks_fts MATCH ?"
        if source_name:
            where += " AND source_id = ?"
            params.append(source_name)
        params.append(max_results)
        sql = f"""
        SELECT chunks_fts.chunk_id, chunks_fts.source_id, chunks_fts.artifact_id,
               chunks_fts.path, chunks_fts.heading_path, chunks.text,
               chunks.start_line, chunks.end_line, artifacts.path AS absolute_path,
               artifacts.relative_path,
               bm25(chunks_fts, {_BM25_WEIGHTS}) / {_STRUCTURAL_PRIOR} AS rank_score
        FROM chunks_fts
        JOIN chunks ON chunks.id = chunks_fts.chunk_id
        JOIN artifacts ON artifacts.id = chunks_fts.artifact_id
        WHERE {where}
        ORDER BY rank_score LIMIT ?
        """
        try:
            rows = self.state.rows(sql, tuple(params))
        except Exception:
            rows = self.state.rows(
                """SELECT chunks.id AS chunk_id, chunks.source_id, chunks.artifact_id,
                artifacts.relative_path AS path, chunks.heading_path, chunks.text,
                chunks.start_line, chunks.end_line, artifacts.path AS absolute_path,
                artifacts.relative_path, 0.0 AS rank_score
                FROM chunks JOIN artifacts ON artifacts.id=chunks.artifact_id
                WHERE chunks.text LIKE ? OR artifacts.relative_path LIKE ? LIMIT ?""",
                (f"%{query}%", f"%{query}%", max_results),
            )
        # No concept-term expansion pass. It used to top up short result sets
        # from `artifact_terms`, and it was measured dead: it only ran when FTS
        # returned FEWER than max_results, and on a real corpus every query
        # matched hundreds to thousands of chunks, so it never fired once. What
        # it did cost was an extra query per search and a table that had grown
        # to 1.27M rows / 554k distinct concepts over 2,132 files. Concept
        # nodes are kept — they still back the graph-facts panel and
        # `similar_to` edges, which is what they are actually good at — but
        # they are no longer a retrieval path.
        results = []
        for i, row in enumerate(rows, start=1):
            # FTS5 bm25() is a cost: more negative = better. Map it to a
            # monotone [0, 1) relevance so downstream merges (hybrid mode)
            # keep FTS's own ordering — the old 1/(1+|bm25|) *inverted* it
            # (found by the Step-33.4 memory benchmark). The LIKE-fallback
            # rows carry rank_score 0.0 and keep their historical 1.0.
            raw_rank = float(row["rank_score"] or 0.0)
            score = (-raw_rank / (1.0 - raw_rank)) if raw_rank < 0 else 1.0
            results.append(_row_result(row, i, score, "SQLite FTS/path match"))
        return results


def _row_result(row, rank: int, score: float, reason: str) -> dict:
    return {
        "rank": rank,
        "node_id": row["artifact_id"],
        "chunk_id": row["chunk_id"],
        "type": "chunk",
        "title": row["relative_path"],
        "path": row["absolute_path"],
        "relative_path": row["relative_path"],
        "score": score,
        "reason": reason,
        "summary": (row["text"] or "")[:240],
        "chunks": [
            {
                "chunk_id": row["chunk_id"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "text_preview": (row["text"] or "")[:500],
            }
        ],
        "provenance": {
            "source_id": row["source_id"],
            "path": row["absolute_path"],
            "relative_path": row["relative_path"],
        },
    }


# Words that carry no retrieval signal but wreck BM25 when they survive into
# the query. The OR-expansion above means every token contributes, and BM25
# weights a token by how *rare* it is — so an uncommon framing verb outscores
# the noun the question is actually about. Measured on the agent-framework
# corpus: "locate" appears in 15 chunks and "readme" in 724, so adding
# "locate" to `locate readme` pushed the repository's own README.md from rank
# 121 to 135 and put an unrelated `_compaction.py` into the top five. These
# are dropped from ranking, never from the user's intent — the answer step
# still sees the original question.
_STOPWORDS = frozenset(
    """
    a an the this that these those there here it its
    i we you me my our your us they them their
    is are was were be been being am do does did doing done
    have has had having can could should would will shall may might must
    of to in on at by for from with without into onto about across over under
    and or not but if then than as so such via per
    what which who whom whose when where why how
    find finds locate locating search searching show shows tell tells give gives
    get gets list lists explain explaining describe describing
    please help need needs want wants use uses using used
    me about above below more most some any all each every
    file files document documents thing things stuff
    """.split()
)

#: Applied when every token is a stopword ("what is it about") — a query with
#: no content words still has to return something, so the stopword filter
#: yields rather than emptying the query.
_MIN_CONTENT_TOKENS = 1


def _query_tokens(query: str, drop_stopwords: bool = True) -> list[str]:
    """Searchable tokens for a query: raw, underscore-split and camel-split.

    ``drop_stopwords`` filters framing words that only add BM25 noise. It
    yields when filtering would leave nothing to search for, so a question
    made entirely of function words degrades to the old behavior instead of
    matching nothing.
    """
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", query):
        tokens.add(raw.lower())
        for part in re.split(r"[_\W]+", raw):
            if len(part) > 1:
                tokens.add(part.lower())
        camel = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw)
        for part in camel.split():
            if len(part) > 1:
                tokens.add(part.lower())
    if drop_stopwords:
        content = {token for token in tokens if token not in _STOPWORDS}
        if len(content) >= _MIN_CONTENT_TOKENS:
            return sorted(content)
    return sorted(tokens)


#: Terms too generic to describe a corpus, on top of the query stopwords.
_VOCAB_NOISE = frozenset(
    """
    self none true false null return def class import from print str int float bool
    list dict set tuple value values key keys name names type types data item items
    args kwargs param params result results test tests example examples new old
    http https www com org net html span div href src id class style
    """.split()
)


def corpus_vocabulary(state: StateStore, limit: int = 64) -> list[tuple[str, int]]:
    """The corpus's most widely-used content terms, as ``(term, doc_count)``.

    Read from ``chunks_vocab``, an ``fts5vocab`` view over the FTS index, so
    it costs no storage and cannot drift from what is actually searchable.

    This is what replaced concept extraction. "What is this corpus about" was
    being answered by materializing 141,529 concept nodes and 1.27M
    ``artifact_terms`` rows, when SQLite was already maintaining a term →
    document-frequency table for the index. Ordering by document frequency
    (not raw count) is deliberate: a term repeated 400 times in one file
    describes that file, while a term appearing once in 400 files describes
    the corpus.
    """
    try:
        rows = state.rows(
            "SELECT term, doc FROM chunks_vocab WHERE length(term) > 3 "
            "AND term NOT GLOB '*[0-9]*' ORDER BY doc DESC, term ASC LIMIT ?",
            (limit * 4,),
        )
    except Exception:  # fts5vocab unavailable (older SQLite) — degrade quietly
        return []
    out: list[tuple[str, int]] = []
    for row in rows:
        term = str(row["term"])
        if term in _STOPWORDS or term in _VOCAB_NOISE:
            continue
        out.append((term, int(row["doc"])))
        if len(out) >= limit:
            break
    return out
