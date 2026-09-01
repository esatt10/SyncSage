"""What is tunable, what it costs to try, and which stage it can move.

A parameter space is usually a list of ranges. This one is a list of ranges
plus two claims per parameter, and the two claims are what keep the search
honest and affordable.

**The stage claim.** Every parameter declares the pipeline stage it acts on.
The strategy proposes a parameter only when the diagnosis blames that stage, so
a region whose misses are all in the lexical arm never spends a trial on the
fusion constant. Without this the search is a fourteen-dimensional sweep over a
space where most dimensions provably cannot affect the observed failure, and it
will still find "improvements" -- noise, dressed as a result, on whatever
cohort it was fitted to.

**The cost claim.** A parameter is either ``refusion`` or ``requery``:

*``refusion``* parameters act *after* the arms have produced their candidates:
the fusion constant, the per-arm weights. Changing one cannot change what any
arm returned, so a trial can be evaluated by re-fusing the arm lists a single
baseline replay already captured. A thousand such trials cost one replay.

*``requery``* parameters act *inside* candidate generation: the BM25 column
weights and the structural priors are in the SQL, and the over-fetch multiplier
changes how many rows come back. Their trials need a real retrieval per query,
so they are budgeted separately, ordered by expected value, and run few.

That split is the whole reason a batch is affordable, and it is a property of
where each parameter sits in the pipeline rather than a heuristic -- which is
why :func:`validate_space` asserts it against
:data:`pheasant.search.ranking.PARAMETER_STAGES` instead of trusting this file.

**Candidate values are enumerated, not sampled.** Each parameter carries an
explicit ladder of values spanning its useful range. Random search over a
continuous range would make a batch non-reproducible, and reproducibility is
the property that lets a re-run of an unchanged experiment be recognised as the
same experiment rather than counted as a second data point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pheasant.evaluation.contracts import digest
from pheasant.search.ranking import BOUNDS, PARAMETER_STAGES, RankingParameters, clamp

#: Cost classes, in the order a budget spends them.
REFUSION = "refusion"
REQUERY = "requery"


@dataclass(frozen=True)
class Parameter:
    """One tunable dimension."""

    name: str
    stage: str
    cost_class: str
    #: The ladder the search walks. Includes the default, so "leave it alone"
    #: is always reachable and a coordinate descent can decline to move.
    candidates: tuple[float, ...]
    rationale: str

    def neighbours(self, current: float) -> list[float]:
        """The candidate values adjacent to ``current``, nearest first.

        Coordinate descent moves one step at a time rather than jumping to the
        best value on the ladder. A large jump evaluated against a cohort of
        fifty queries is how a search lands on an extreme that happens to suit
        those fifty and nothing else.
        """

        ordered = sorted(self.candidates, key=lambda value: (abs(value - current), value))
        return [value for value in ordered if value != current]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "stage": self.stage,
            "cost_class": self.cost_class,
            "candidates": list(self.candidates),
            "bounds": list(BOUNDS.get(self.name, ())),
            "rationale": self.rationale,
        }


#: The shipped space. Every ladder is centred on the current default and
#: bounded well inside `ranking.BOUNDS`, because the bounds are "past here it
#: stops being ranking" and a search should not spend trials proving that.
DEFAULT_PARAMETERS: tuple[Parameter, ...] = (
    Parameter(
        name="rrf_k",
        stage="fusion",
        cost_class=REFUSION,
        candidates=(10.0, 20.0, 30.0, 60.0, 90.0, 120.0, 200.0),
        rationale=(
            "How hard fusion damps the top ranks. Low k lets one arm's confident "
            "first place decide the merge; high k makes agreement between arms "
            "the dominant signal. 60 is the RRF paper's value and a default, not "
            "a measurement of this corpus."
        ),
    ),
    Parameter(
        name="text_arm_weight",
        stage="fusion",
        cost_class=REFUSION,
        candidates=(0.5, 0.75, 1.0, 1.5, 2.0, 3.0),
        rationale=(
            "What the lexical arm's ordering is worth in the merge. Worth moving "
            "when the diagnosis shows the text arm holding targets that fusion "
            "ranks below vector or graph hits."
        ),
    ),
    Parameter(
        name="vector_arm_weight",
        stage="fusion",
        cost_class=REFUSION,
        candidates=(0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0),
        rationale=(
            "Includes 0.0 deliberately: a stale or badly-dimensioned vector index "
            "actively costs ranking, and proving that the merge is better without "
            "it is a legitimate and useful outcome."
        ),
    ),
    Parameter(
        name="graph_arm_weight",
        stage="fusion",
        cost_class=REFUSION,
        candidates=(0.0, 0.25, 0.5, 0.75, 1.0, 1.5),
        rationale=(
            "The graph arm returns symbols and entities, which are the right "
            "answer to some queries and noise in others; the balance is a "
            "property of the corpus, not of the software."
        ),
    ),
    Parameter(
        name="title_weight",
        stage="lexical_candidates",
        cost_class=REQUERY,
        candidates=(2.0, 4.0, 6.0, 8.0, 12.0, 16.0, 24.0),
        rationale=(
            "`title` is the file's basename. It is 8 because untuned a filename "
            "match was worth one body word and BM25 then ranked by body brevity; "
            "how far above `text` it should sit depends on whether the corpus is "
            "code (filenames are meaningful) or prose (they are not)."
        ),
    ),
    Parameter(
        name="path_weight",
        stage="lexical_candidates",
        cost_class=REQUERY,
        candidates=(1.0, 2.0, 3.0, 4.0, 6.0, 8.0),
        rationale="How much the full relative path contributes next to the basename.",
    ),
    Parameter(
        name="heading_weight",
        stage="lexical_candidates",
        cost_class=REQUERY,
        candidates=(0.0, 1.0, 2.0, 3.0, 4.0, 6.0),
        rationale=(
            "Worth nothing on a corpus with no extracted taxonomy and worth a "
            "great deal on one where chunks are cut at section boundaries, which "
            "is exactly the kind of thing a region should measure rather than "
            "inherit."
        ),
    ),
    Parameter(
        name="text_weight",
        stage="lexical_candidates",
        cost_class=REQUERY,
        candidates=(0.5, 1.0, 1.5, 2.0, 3.0),
        rationale=(
            "The body. Moving this is equivalent to moving the other three the "
            "other way, so it is on the ladder mostly to let a descent express "
            "'the fields are too far apart' in one step instead of three."
        ),
    ),
    Parameter(
        name="depth_prior",
        stage="structural_prior",
        cost_class=REQUERY,
        candidates=(0.0, 0.025, 0.05, 0.1, 0.15, 0.25),
        rationale=(
            "Tie-breaks toward files nearer the root, which is the only signal "
            "separating 412 identically-named READMEs. Too high and it buries "
            "legitimately deep code; 0.0 turns it off, which is the right answer "
            "for a flat corpus."
        ),
    ),
    Parameter(
        name="test_prior",
        stage="structural_prior",
        cost_class=REQUERY,
        candidates=(0.0, 0.3, 0.6, 1.0, 1.5, 2.0),
        rationale=(
            "Pushes test files below implementations for 'where is X implemented'. "
            "A region whose users mostly ask about tests wants this near zero, and "
            "there is no way to know which kind of region this is without asking "
            "the queries."
        ),
    ),
    Parameter(
        name="sample_prior",
        stage="structural_prior",
        cost_class=REQUERY,
        candidates=(0.0, 0.15, 0.3, 0.6, 1.0),
        rationale="The same, one notch softer: sample code is often a legitimate answer.",
    ),
    Parameter(
        name="filter_overfetch",
        stage="filters",
        cost_class=REQUERY,
        candidates=(1.0, 2.0, 3.0, 4.0, 6.0),
        rationale=(
            "How far past `max_results` the arms fetch when ACL, memory-policy or "
            "section filtering will remove candidates afterwards. Under-fetching "
            "here shows up as a `filters` miss that no amount of ranking fixes: "
            "the document was never in the window the filter ran over."
        ),
    ),
)


@dataclass(frozen=True)
class ParameterSpace:
    """The parameters an experiment may move, and the ladders it may walk."""

    parameters: tuple[Parameter, ...] = DEFAULT_PARAMETERS
    #: Names an operator has pinned. A pinned parameter is never proposed --
    #: which is how a region that has measured its own title weight keeps a
    #: later sweep from quietly re-litigating it.
    pinned: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parameters",
            tuple(p for p in self.parameters if p.name not in self.pinned),
        )

    @property
    def digest(self) -> str:
        """Identity of the space, so an experiment over a changed space is a
        different experiment rather than a continuation of the old one."""

        return digest(
            [(p.name, p.stage, p.cost_class, list(p.candidates)) for p in self.parameters],
            sorted(self.pinned),
        )

    def by_name(self, name: str) -> Parameter | None:
        return next((p for p in self.parameters if p.name == name), None)

    def for_stage(self, stage: str) -> list[Parameter]:
        """Parameters that act on one stage.

        ``filters`` and ``truncation`` are handled by the same over-fetch
        parameter, and ``fusion`` covers ``truncation`` too: a target fused
        below the cut is moved by re-weighting the arms, not by anything the
        truncation itself exposes.
        """

        wanted = {stage}
        if stage == "truncation":
            wanted.add("fusion")
        if stage == "lexical_candidates":
            # A lexical miss is as often a structural-prior problem as a column
            # weight one: the prior is a divisor on the same score, so it can
            # push a matched document out of the fetch window on its own.
            wanted.add("structural_prior")
        return [p for p in self.parameters if p.stage in wanted]

    def cost_classes(self) -> dict[str, list[str]]:
        classes: dict[str, list[str]] = {REFUSION: [], REQUERY: []}
        for parameter in self.parameters:
            classes.setdefault(parameter.cost_class, []).append(parameter.name)
        return classes

    def as_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "pinned": sorted(self.pinned),
            "parameters": [p.as_dict() for p in self.parameters],
            "cost_classes": self.cost_classes(),
        }


def baseline_values(ranking: RankingParameters, space: ParameterSpace) -> dict[str, float]:
    """The current value of every parameter the space may move."""

    values = ranking.values()
    return {p.name: clamp(p.name, values[p.name]) for p in space.parameters if p.name in values}


def apply_point(ranking: RankingParameters, values: dict[str, float]) -> RankingParameters:
    """A parameter point as a real :class:`RankingParameters`.

    Goes through ``with_overlay`` -- the same clamping path a stored bundle
    takes -- so a trial can never measure a configuration the region would
    refuse to serve.
    """

    return ranking.with_overlay(values, provenance="trial")


def validate_space(space: ParameterSpace) -> list[str]:
    """Reasons this space is not usable, or an empty list.

    Checked mechanically rather than by review: a parameter whose declared
    stage disagrees with ``ranking.PARAMETER_STAGES`` would make the strategy
    propose it for the wrong diagnosis, which is a silent failure -- the search
    still runs, still reports numbers, and is simply looking in the wrong
    place.
    """

    problems: list[str] = []
    for parameter in space.parameters:
        if parameter.name not in PARAMETER_STAGES:
            problems.append(f"{parameter.name}: not a ranking parameter")
            continue
        declared = PARAMETER_STAGES[parameter.name]
        if declared != parameter.stage:
            problems.append(
                f"{parameter.name}: space says stage {parameter.stage!r}, ranking says {declared!r}"
            )
        if parameter.cost_class not in (REFUSION, REQUERY):
            problems.append(f"{parameter.name}: unknown cost class {parameter.cost_class!r}")
        # A fusion parameter that needed a re-query, or a candidate-generation
        # parameter claimed to be free, would break the budget's whole premise.
        if parameter.stage == "fusion" and parameter.cost_class != REFUSION:
            problems.append(f"{parameter.name}: a fusion parameter is always re-fusable")
        if parameter.stage != "fusion" and parameter.cost_class == REFUSION:
            problems.append(
                f"{parameter.name}: acts on {parameter.stage!r}, which changes the "
                "candidates, so it cannot be evaluated by re-fusion"
            )
        low, high = BOUNDS.get(parameter.name, (float("-inf"), float("inf")))
        outside = [value for value in parameter.candidates if not low <= value <= high]
        if outside:
            problems.append(f"{parameter.name}: candidates outside bounds: {outside}")
        if not parameter.candidates:
            problems.append(f"{parameter.name}: empty ladder")
    return problems
