"""LongMemEval-style memory-recall benchmark (Product Framework Step 33.4).

Deterministic and fully offline: a seeded generator writes synthetic memory
histories through the **real** write path (``memory_write``) and scores
recall through the **real** search path (``search_context``) — no mocked
retrieval anywhere. Categories follow the LongMemEval taxonomy where it
maps onto region memory:

- **single-hop recall** — a fact written once must surface in the top-k
  for its natural-language question;
- **multi-session interference** — recall holds against a large body of
  distractor memories written across other sessions/subjects;
- **knowledge updates** — a superseding write followed by consolidation
  must make the *current* fact retrievable and the stale one absent;
- **abstention** — questions about never-stored entities must not surface
  confident hits.

A true LongMemEval run (LLM-judged answers over the published dataset,
scored against Mem0/Zep) needs network + an LLM and is tracked as manual
release-channel work; this harness is the runnable, regression-gated
proxy. Reproduce the recorded numbers with::

    python -m pheasant.memory.benchmark
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

WORDS = (
    "amber quartz falcon delta harbor juniper krypton lumen meadow nova "
    "onyx prism quill rustic saffron timber umber vertex willow zephyr "
    "basalt cedar dune ember flint garnet"
).split()

REGIONS = ["us-east-2", "eu-west-1", "ap-south-1", "us-west-2", "sa-east-1"]
OWNERS = ["ada", "bhaskara", "curie", "darwin", "euler", "fermi", "gauss"]


@dataclass(frozen=True)
class MemoryBenchSpec:
    n_facts: int = 30
    n_distractors: int = 120
    n_updates: int = 10
    n_abstain: int = 10
    k: int = 5
    seed: int = 1337
    mode: str = "hybrid"


@dataclass
class MemoryBenchReport:
    recall_at_k: float
    update_accuracy: float
    stale_leak_rate: float
    abstention_accuracy: float
    spec: MemoryBenchSpec
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "recall_at_k": self.recall_at_k,
            "update_accuracy": self.update_accuracy,
            "stale_leak_rate": self.stale_leak_rate,
            "abstention_accuracy": self.abstention_accuracy,
            "k": self.spec.k,
            "n_facts": self.spec.n_facts,
            "n_distractors": self.spec.n_distractors,
            "n_updates": self.spec.n_updates,
            "n_abstain": self.spec.n_abstain,
            "seed": self.spec.seed,
            "mode": self.spec.mode,
        }


def _codename(rng: random.Random) -> str:
    # One atomic token (no hyphens/shared parts): distinct entities stay
    # distinct in FTS *and* in graph entity nodes, mirroring real entity
    # names; interference pressure comes from the shared template
    # vocabulary and the distractor corpus, not from entity-name collisions.
    return f"{rng.choice(WORDS)}{rng.choice(WORDS)}{rng.randrange(100, 999)}"


def generate_cases(spec: MemoryBenchSpec) -> dict[str, list[dict[str, str]]]:
    """Seeded, deterministic case set (same spec → identical cases)."""
    rng = random.Random(spec.seed)
    seen: set[str] = set()

    def fresh_codename() -> str:
        while True:
            name = _codename(rng)
            if name not in seen:
                seen.add(name)
                return name

    facts = []
    for _ in range(spec.n_facts):
        service = fresh_codename()
        region, owner = rng.choice(REGIONS), rng.choice(OWNERS)
        facts.append(
            {
                "service": service,
                "text": f"The {service} service runs in {region} and is owned by {owner}.",
                "query": f"where does the {service} service run",
                "region": region,
            }
        )
    distractors = [
        {
            "text": (
                f"Note {i}: the {fresh_codename()} experiment logged "
                f"{rng.randrange(10, 500)} samples during review."
            )
        }
        for i in range(spec.n_distractors)
    ]
    updates = []
    for case in facts[: spec.n_updates]:
        new_region = rng.choice([r for r in REGIONS if r != case["region"]])
        updates.append(
            {
                "service": case["service"],
                "old_text": case["text"],
                "new_text": (
                    f"The {case['service']} service now runs in {new_region} after the migration."
                ),
                "query": case["query"],
            }
        )
    # Abstention queries share NO vocabulary with facts/distractors beyond
    # the unheard codename — exact-match abstention: FTS must return nothing.
    abstain = [{"query": f"{fresh_codename()} deployment status"} for _ in range(spec.n_abstain)]
    return {"facts": facts, "distractors": distractors, "updates": updates, "abstain": abstain}


def _hit_paths(tools: Any, kb: str, query: str, k: int, mode: str) -> list[str]:
    payload = tools.search_context(kb, query, mode=mode, max_results=k)
    return [str(item.get("relative_path") or "") for item in payload["results"]]


def run_memory_recall_benchmark(
    tools: Any, spec: MemoryBenchSpec | None = None, *, knowledge_base: str | None = None
) -> MemoryBenchReport:
    """Run the four categories against a live ``PheasantTools`` facade.

    The facade's config must include a ``type: memory`` source. Writes go
    through ``memory_write`` (sync deferred to one batch index), updates
    through ``supersedes`` + ``memory_consolidate`` — the exact surfaces an
    agent uses.
    """
    from pheasant.memory.maintenance import run_memory_maintenance
    from pheasant.memory.store import memory_source

    spec = spec or MemoryBenchSpec()
    kb = knowledge_base or tools.config.pheasant.name
    source = memory_source(tools.config)
    if source is None:
        raise ValueError("benchmark needs a `type: memory` source in the config")
    cases = generate_cases(spec)

    fact_ids: dict[str, str] = {}
    for case in cases["facts"]:
        record = tools.memory_write(kb, case["text"], scope="org", sync=False)["record"]
        fact_ids[case["text"]] = record["record_id"]
    for idx, distractor in enumerate(cases["distractors"]):
        scope = ("session", "user", "org")[idx % 3]
        tools.memory_write(kb, distractor["text"], scope=scope, sync=False)
    tools.sync_source(kb, source.name, "incremental")

    recalled = sum(
        1
        for case in cases["facts"]
        if any(
            fact_ids[case["text"]] in path
            for path in _hit_paths(tools, kb, case["query"], spec.k, spec.mode)
        )
    )

    update_ids: dict[str, str] = {}
    for case in cases["updates"]:
        new = tools.memory_write(
            kb,
            case["new_text"],
            scope="org",
            supersedes=fact_ids[case["old_text"]],
            sync=False,
        )["record"]
        update_ids[case["new_text"]] = new["record_id"]
    tools.sync_source(kb, source.name, "incremental")
    run_memory_maintenance(tools.engine)

    updated_ok = 0
    stale_leaks = 0
    for case in cases["updates"]:
        paths = _hit_paths(tools, kb, case["query"], spec.k, spec.mode)
        if any(update_ids[case["new_text"]] in p for p in paths):
            updated_ok += 1
        if any(fact_ids[case["old_text"]] in p for p in paths):
            stale_leaks += 1

    abstained = sum(
        1
        for case in cases["abstain"]
        if not _hit_paths(tools, kb, case["query"], spec.k, spec.mode)
    )

    return MemoryBenchReport(
        recall_at_k=recalled / spec.n_facts,
        update_accuracy=updated_ok / max(spec.n_updates, 1),
        stale_leak_rate=stale_leaks / max(spec.n_updates, 1),
        abstention_accuracy=abstained / max(spec.n_abstain, 1),
        spec=spec,
        details={"recalled": recalled, "updated_ok": updated_ok, "stale_leaks": stale_leaks},
    )


def main() -> None:  # pragma: no cover - manual reproduction entry point
    import tempfile
    from pathlib import Path

    from pheasant.config.loader import load_config
    from pheasant.mcp_server.tools import PheasantTools

    with tempfile.TemporaryDirectory(prefix="pheasant-membench-") as tmp:
        root = Path(tmp)
        (root / "memory").mkdir()
        config_path = root / "pheasant.yaml"
        config_path.write_text(
            f"""pheasant:
  name: membench
  state_path: {root / "state"}
  vault_path: {root / "vault"}
  exports_path: {root / "exports"}
  workspace_root: {root}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources:
  - name: agent-memory
    type: memory
    path: memory
""",
            encoding="utf-8",
        )
        tools = PheasantTools(load_config(config_path))
        try:
            report = run_memory_recall_benchmark(tools)
        finally:
            tools.engine.close()
        for key, value in report.as_dict().items():
            print(f"{key}: {value}")


if __name__ == "__main__":  # pragma: no cover
    main()
