"""The tuning plane: which retrieval stage is failing, and what to do about it.

A fourth plane, and the distinction is the same one the evaluation plane draws
about itself. Observations are **evidence**. Records are **memory**.
Measurements are **neither**. And a tuning experiment is none of the three: it
is a *proposal about configuration*, and nothing it holds is a file, is
chunked, is indexed, or is returned by a search. A region must not retrieve its
own experiments as knowledge any more than it may retrieve its own report.

What it adds over evaluation is the question evaluation deliberately does not
answer. ``known_positive_recall_at_10 = 0.61`` is a number about the region;
it is not a number about a *stage*. Retrieval is a pipeline -- query analysis,
three independent candidate arms, three filters, a fusion, a truncation -- and
after the merge every one of those failures looks identical, because they all
produce the same thing: an absent result. Six causes, one symptom.

So the plane works in four movements, and each one is a separate durable
artifact rather than a step inside a function:

1. **Diagnose.** Replay a cohort with ``explain=True`` and attribute every
   miss to the *first* stage that lost the document (:mod:`.stages`). The
   output is a histogram over stages, not a score. A region whose misses are
   80% ``absent_from_corpus`` has an indexing problem, and no ranking
   parameter in this package will move it -- saying so is the most useful
   thing the diagnosis does.
2. **Propose.** Take only the parameters whose stage the diagnosis actually
   blames (:mod:`.space`). Tuning the fusion constant because recall is low,
   when the lexical arm never returned the target, is how a search over
   fourteen parameters spends a day proving nothing.
3. **Trial.** Evaluate each proposal. Most cost *no retrieval at all*:
   fusion-family parameters are recomputed from the cached per-arm lists
   (:mod:`.refusion`), so thousands of points are explored against one replay.
   Only the parameters that change candidate generation need a re-search, and
   those are budgeted separately and run few.
4. **Decide.** Gate the winner against a held-out cohort and a control
   (:mod:`.gates`), then package it as a **bundle** -- a configuration set with
   its digest, its provenance and every measurement behind it (:mod:`.bundle`).
   Applying a bundle is what makes this a tuning system rather than a report
   generator, and it is reversible by construction.

Four rules the rest of the package is built to keep.

**Nothing is promoted by its own evidence.** A parameter point that improved
the queries it was selected on has demonstrated selection, not improvement.
Promotion requires a held-out cohort the search never saw and a control cohort
that must not regress -- the same closed loop the evaluation plane's
``allow_originating_query_only_promotion`` keeps shut, for the same reason.

**Fleet configuration only.** A bundle sets region-wide retrieval parameters.
There is no per-request and no per-principal tuning, and the absence is
deliberate: parameters that varied by caller would make two agents disagree
about what the region contains, and would make every published metric a
measurement of whoever happened to ask.

**It must not become the region's workload.** The executor holds one slot,
takes its own lease, never takes ``sync_lock``, and stands down when the index
queue is backed up (:mod:`.executor`). The whole design of the re-fusion path
exists so that the *search* over a parameter space costs a constant number of
retrievals rather than one per trial.

**Cold by default.** ``/state`` holds small rows -- an experiment, a trial's
scores, a decision. The bulky per-query, per-trial rankings go to
``/exports/tuning/`` as compressed JSONL, because they are regenerable and
because an operational database is not a place to accumulate them
(:mod:`.store`).

See ``docs/retrieval-tuning.md``.
"""

from __future__ import annotations

from pheasant.tuning.contracts import (
    Decision,
    Diagnosis,
    Experiment,
    ParameterPoint,
    StageAttribution,
    Trial,
    TuningBundle,
)
from pheasant.tuning.stages import STAGES, attribute, stage_histogram

__all__ = [
    "STAGES",
    "Decision",
    "Diagnosis",
    "Experiment",
    "ParameterPoint",
    "StageAttribution",
    "Trial",
    "TuningBundle",
    "attribute",
    "stage_histogram",
]
