"""What every number on the tuning surface means, and what it does not.

This exists because of a specific failure mode: a dashboard full of rates that
people read confidently and wrongly. "Truncation 42%" looks like a problem and
usually is not. "Empty rate 3%" looks fine and may be the whole story. A
`generalization_gap` sounds bad and is *supposed* to be non-zero. Someone
reading those without their meaning will act on the ones that sound alarming
rather than the ones that are, and the plane will have made retrieval worse
while reporting numbers.

So every entry carries four fields, and the last two are the ones that are
usually missing from software like this:

``means``
    What the number is, in a sentence, with its denominator named.
``impact``
    What it tells you about *retrieval* — what a person should do differently
    if it moves. A metric with no action attached is decoration.
``does_not_mean``
    The misreading this number invites. Written as the wrong conclusion, not
    as a hedge, because "may not reflect quality" teaches nobody anything and
    "a high truncation rate is normal and not a fault" teaches them the thing.
``direction``
    ``higher``, ``lower``, or ``neutral`` — and ``neutral`` is common here on
    purpose. Several of these are diagnostics with no good direction, and
    presenting them with an arrow would imply an optimization target that does
    not exist.

Served over HTTP and MCP and rendered inline in the UI, rather than living in
the docs. Documentation a reader has to go and find is documentation that
arrives after the mistake.
"""

from __future__ import annotations

from typing import Any

#: How to read a movement. `neutral` is not a cop-out: a fused depth of 40 is
#: neither good nor bad, and drawing it with an arrow would invite somebody to
#: optimize it.
HIGHER = "higher"
LOWER = "lower"
NEUTRAL = "neutral"


def _entry(
    term: str,
    label: str,
    means: str,
    impact: str,
    does_not_mean: str,
    direction: str = NEUTRAL,
    kind: str = "metric",
) -> dict[str, Any]:
    return {
        "term": term,
        "label": label,
        "kind": kind,
        "means": means,
        "impact": impact,
        "does_not_mean": does_not_mean,
        "direction": direction,
    }


#: Metrics a batch computes over a cohort with known positives.
METRICS: list[dict[str, Any]] = [
    _entry(
        "known_positive_reciprocal_rank",
        "Reciprocal rank",
        "The average of 1/rank of the first known-good document, over the "
        "evidenced queries only. 1.0 means every query put a good result "
        "first; 0.5 means rank two on average.",
        "The metric to watch when callers read the top result. If it rises "
        "while recall holds, ranking genuinely improved. If it rises while "
        "recall falls, a document was pushed out of the list to sharpen the "
        "top — which is a trade, not a win.",
        "It is not accuracy. It says where a document somebody already "
        "judged good landed; it says nothing about the documents nobody "
        "judged, and nothing about whether the answer built from them was "
        "correct.",
        HIGHER,
    ),
    _entry(
        "known_positive_recall_at_5",
        "Recall at 5",
        "The share of known-good documents that appeared in the first five "
        "results, averaged over evidenced queries.",
        "Watch this when something downstream reads several results. A drop "
        "here alongside a rise in reciprocal rank is the classic "
        "over-sharpening trade.",
        "It is order-blind inside the top five. A parameter set that moves "
        "the best answer from rank 1 to rank 5 does not change this number "
        "at all.",
        HIGHER,
    ),
    _entry(
        "known_positive_recall_at_10",
        "Recall at 10",
        "The same as recall at 5 over a wider window.",
        "The right headline for agentic callers that fetch a page of context "
        "and let a model pick. Insensitive to ordering across all ten.",
        "A high value does not mean the results are well ordered. It means "
        "the documents are present.",
        HIGHER,
    ),
    _entry(
        "known_positive_hit_rate",
        "Hit rate",
        "The share of evidenced queries where a known-good document appeared "
        "anywhere in the results.",
        "The blunt instrument. Use it when the complaint is 'it finds "
        "nothing'; switch to reciprocal rank once it is off the floor.",
        "Position-blind entirely. Every answer moving from rank 1 to rank 10 "
        "leaves this unchanged.",
        HIGHER,
    ),
]

#: Live pipeline health, computed from sampled stage digests on real traffic.
#: These need no proof, and consequently can say nothing about correctness.
HEALTH: list[dict[str, Any]] = [
    _entry(
        "empty_rate",
        "Returned nothing",
        "The share of sampled searches that returned zero results, with the "
        "count and the sample size beside it.",
        "The single most actionable number here, because the stage breakdown "
        "says where to look: `no_candidates` means the corpus or the query "
        "analysis, `filters` means an ACL or memory policy is too narrow or "
        "the over-fetch window is too small, `fusion` means the merge.",
        "A low empty rate is not evidence that results are good. A region "
        "that returns ten irrelevant documents for every query has an empty "
        "rate of zero.",
        LOWER,
        kind="health",
    ),
    _entry(
        "arm_contribution_rate",
        "Arm contributed",
        "Of the searches where this arm ran, the share where it returned at least one candidate.",
        "An arm near zero is costing latency on every hybrid search and "
        "contributing nothing — usually a vector index that was never built, "
        "is stale, or is dimensioned wrong. Either fix it or set its fusion "
        "weight to 0 and prove the merge is better without it.",
        "A high rate does not mean the arm is *useful*. It means the arm "
        "returned something. Whether those candidates survive fusion is a "
        "different question, answered by the fusion contribution counter.",
        NEUTRAL,
        kind="health",
    ),
    _entry(
        "arm_failed",
        "Arm failures",
        "Searches where an arm raised rather than returning nothing.",
        "Counted separately from 'empty' deliberately: 'the vector index is "
        "down' and 'the vector index has nothing for this query' look "
        "identical downstream and call for opposite responses. Any non-zero "
        "value here is an outage, not a tuning problem.",
        "This is not a ranking signal and no parameter will move it. It is an "
        "infrastructure fault.",
        LOWER,
        kind="health",
    ),
    _entry(
        "truncation_rate",
        "Truncated",
        "The share of searches where the fused candidate list was longer than "
        "the number of results returned.",
        "Usually high and usually fine — it means the arms found more than the "
        "caller asked for, which is what over-fetching is for. Worth looking "
        "at only when it is near zero, which means the arms are barely "
        "filling the requested page and ranking has almost nothing to do.",
        "A high truncation rate is **not** a fault and not a sign of lost "
        "results. Reading it as one is the most common misreading on this "
        "page.",
        NEUTRAL,
        kind="health",
    ),
    _entry(
        "results_per_search",
        "Results per search",
        "Mean results returned across sampled searches.",
        "Read against the max_results callers ask for. Consistently below it "
        "means the pipeline cannot fill a page — check the empty-rate stage "
        "breakdown for which step is short.",
        "Not a quality measure in either direction. More results is not better.",
        NEUTRAL,
        kind="health",
    ),
]

#: The pipeline stages a miss is attributed to.
STAGES: list[dict[str, Any]] = [
    _entry(
        "absent_from_corpus",
        "Not indexed",
        "The document is not in the index, so no arm could have returned it.",
        "Look at sources, include patterns and extraction. **No retrieval "
        "parameter can move this**, and a batch whose misses are mostly here "
        "will correctly propose nothing.",
        "This is not a ranking failure and tuning will not help it.",
        LOWER,
        kind="stage",
    ),
    _entry(
        "candidates_missing",
        "No arm returned it",
        "No arm returned the target, and the corpus was not checked — so "
        "whether it is absent or merely unranked is unknown.",
        "Deliberately distinct from `absent_from_corpus`: this plane does not "
        "infer absence. Run a diagnosis with corpus lookup available to "
        "resolve which it is.",
        "It does not mean the document is missing from the index. It means nobody checked.",
        LOWER,
        kind="stage",
    ),
    _entry(
        "lexical_candidates",
        "Text arm missed it",
        "The document is indexed, and the BM25/tsvector arm did not return it "
        "within its fetch window.",
        "Reachable by the column weights (title, path, heading, text) and the "
        "structural priors. A document whose *filename* matches but body does "
        "not wants a higher title weight; one buried by path-depth wants a "
        "lower depth prior.",
        "Not necessarily a failure of the whole pipeline — another arm may "
        "have covered it, and attribution only blames this stage when no arm "
        "had the document.",
        LOWER,
        kind="stage",
    ),
    _entry(
        "vector_candidates",
        "Vector arm missed it",
        "The embedding arm did not return the target.",
        "Usually an embedding problem rather than a ranking one: a stale "
        "index, the wrong model, or a dimension mismatch. The arm weight can "
        "only change how much its candidates count, not whether it has any.",
        "Lowering the vector arm's weight does not fix this stage; it only "
        "stops the arm from diluting the merge.",
        LOWER,
        kind="stage",
    ),
    _entry(
        "graph_candidates",
        "Graph arm missed it",
        "The graph arm did not return the target.",
        "The graph arm returns symbols, entities and headings rather than "
        "passages, so it misses ordinary prose by design. Worth acting on "
        "only when the target *is* a structural node — otherwise this stage "
        "firing is the arm working correctly and the merge covering it.",
        "It is rarely the real fault. Attribution only blames an arm when no "
        "arm had the document, so this appearing at all means the text and "
        "vector arms missed it too.",
        LOWER,
        kind="stage",
    ),
    _entry(
        "filters",
        "A filter removed it",
        "An arm returned the target and a filter — ACL, memory policy, or a "
        "section constraint — dropped it before fusion.",
        "Two possible causes with opposite fixes. If the policy is correct, "
        "this is not a fault at all. If it is not, either the policy is too "
        "narrow or `filter_overfetch` is too small — the arms fetched a page, "
        "the filter ate most of it, and nothing was left to rank.",
        "A filter drop is not automatically a bug. An ACL doing its job "
        "produces this stage, and the right response is nothing.",
        LOWER,
        kind="stage",
    ),
    _entry(
        "fusion",
        "The merge ranked it below the cut",
        "The document survived every filter and reciprocal rank fusion placed "
        "it below the results returned.",
        "The stage the fusion parameters reach: `rrf_k` and the three arm "
        "weights. This is where an arm that agrees with another gets "
        "promoted, so it is also where a badly weighted arm buries good "
        "results.",
        "It does not mean the document scored badly in any arm. Fusion ranks "
        "on position, not score, and a document ranked well by one arm and "
        "not seen by the others can still land here.",
        LOWER,
        kind="stage",
    ),
    _entry(
        "truncation",
        "Deduplicated away",
        "The document was fused inside the cut but not returned, because the "
        "merge collapsed it behind another chunk of the same artifact.",
        "Rare. Usually means one artifact is dominating a page with several of its own chunks.",
        "Not the same as the caller asking for too few results — that case is "
        "attributed to `fusion`.",
        LOWER,
        kind="stage",
    ),
    _entry(
        "served",
        "Returned",
        "The known-good document was in the results. Nothing failed.",
        "The denominator's other half. A rising served count with a flat "
        "miss count usually means the cohort grew, not that ranking improved.",
        "Being served is not evidence the result was useful — only that it was shown.",
        HIGHER,
        kind="stage",
    ),
]

#: The gates that stand between a winning trial and the live configuration.
GATES: list[dict[str, Any]] = [
    _entry(
        "holdout_confirms",
        "Confirmed on unseen queries",
        "The winning parameters must also improve a cohort the search never "
        "selected on, by a margin above noise.",
        "The most important gate here. A point that improved the queries it "
        "was *chosen* on has demonstrated selection, not improvement — on the "
        "search cohort every winner looks like a winner, which is what "
        "selection means.",
        "Failing this does not mean the parameters are bad. It means they are "
        "unproven, and often that the region has no holdout cohort yet.",
        kind="gate",
    ),
    _entry(
        "control_does_not_regress",
        "Nothing else got worse",
        "A control cohort — queries the experiment is not trying to improve — "
        "must not fall by more than a small margin.",
        "Retrieval is a fixed number of slots, so almost any change that "
        "helps one class of query hurts another. This is what stops a batch "
        "from optimizing the evidenced fraction of traffic at everyone "
        "else's expense.",
        "Passing it does not mean nothing regressed. It means nothing "
        "regressed *on the control cohort*, which is a sample.",
        kind="gate",
    ),
    _entry(
        "no_stage_collapse",
        "No failure mode traded for another",
        "No single pipeline stage's miss count may grow by more than a set "
        "share of the evaluated queries.",
        "Catches the parameter set that lifts the headline while emptying an "
        "arm — setting a weight to zero moves that arm's exclusive hits into "
        "`candidates_missing`. Sometimes that is right; this makes sure it is "
        "never silent.",
        "It does not forbid removing an arm. It forbids doing so without the decision saying so.",
        kind="gate",
    ),
    _entry(
        "sufficient_evidence",
        "Enough paired queries",
        "A minimum number of queries scored under both the baseline and the "
        "candidate before a delta may decide anything.",
        "A denominator gate. Six paired queries can produce any delta you "
        "like, in either direction.",
        "Passing it does not make a small delta meaningful — it only makes it measurable.",
        kind="gate",
    ),
    _entry(
        "parameters_within_bounds",
        "Servable configuration",
        "Every parameter is a known ranking parameter inside its clamp range.",
        "Cheap, and it catches the case where a search bug produces a point "
        "the region would clamp on read — which would make the applied "
        "configuration quietly different from the measured one.",
        "Not a quality check. A configuration can be perfectly in-bounds and terrible.",
        kind="gate",
    ),
]


def catalog() -> dict[str, Any]:
    """Everything, grouped, for a surface that wants to explain itself."""

    from pheasant.search.ranking import BOUNDS, PARAMETER_STAGES
    from pheasant.tuning.space import DEFAULT_PARAMETERS

    parameters = [
        {
            "term": parameter.name,
            "label": parameter.name,
            "kind": "parameter",
            "stage": parameter.stage,
            "cost_class": parameter.cost_class,
            "bounds": list(BOUNDS.get(parameter.name, ())),
            "candidates": list(parameter.candidates),
            # The parameter's own rationale is already written where it is
            # defined; repeating it here would create two homes for one
            # explanation and they would drift.
            "means": parameter.rationale,
            "impact": (
                f"Acts on the {parameter.stage} stage. "
                + (
                    "Trialling it costs no retrieval — it is recomputed from cached candidates."
                    if parameter.cost_class == "refusion"
                    else "Trialling it needs a real search per query."
                )
            ),
            "does_not_mean": (
                "Changing it cannot fix a miss attributed to a different "
                "stage. Read the diagnosis first."
            ),
            "direction": NEUTRAL,
        }
        for parameter in DEFAULT_PARAMETERS
    ]
    unexplained = sorted(set(PARAMETER_STAGES) - {p["term"] for p in parameters})
    return {
        "metrics": METRICS,
        "health": HEALTH,
        "stages": STAGES,
        "gates": GATES,
        "parameters": parameters,
        # Named rather than hidden: a tunable parameter with no explanation is
        # a gap somebody should close, and the catalog is where it shows.
        "parameters_without_explanation": unexplained,
        "reading_notes": [
            "Every rate here carries its denominator. A rate without one is not a measurement.",
            "Live health says what the pipeline did. It never says whether an "
            "answer was correct — nobody judged those queries.",
            "A metric that could not be computed reports insufficient evidence, never 0.0.",
            "Attribution blames the first stage that lost a document, not "
            "every stage downstream of it.",
        ],
    }


def lookup(term: str) -> dict[str, Any] | None:
    """One entry by name, across every group."""

    for group in (METRICS, HEALTH, STAGES, GATES):
        for entry in group:
            if entry["term"] == term:
                return entry
    for entry in catalog()["parameters"]:
        if entry["term"] == term:
            return entry
    return None
