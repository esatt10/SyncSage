"""Drive one evaluation batch inside the CI container, seeding it first.

Split out of the compose file rather than inlined as a `-c` one-liner because
it does three distinguishable things, and a failure in any of them should say
which. It is also the piece the smoke script *kills*: `PHEASANT_EVAL_STALL`
makes the replay loop sleep so there is a window in which to stop the
container, which is the only honest way to test "the process went away".
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

CONFIG = Path(os.environ.get("PHEASANT_CONFIG", "/config/pheasant.yaml"))
QUERIES = [
    "where is invoice retry configured",
    "which module owns the retry policy",
    "filewatch daemon restart schedule",
    "how does the runbook describe rollouts",
    "what coordinates canaries",
    "invoice retry handler location",
]


def main() -> int:
    from pheasant.cli import _engine

    import pheasant.evaluation as evaluation
    from pheasant.sync.log_queue import write_events
    from pheasant.telemetry.interactions import InteractionEvent

    engine = _engine(CONFIG)
    config = engine.config
    try:
        if os.environ.get("PHEASANT_EVAL_SEED") == "1":
            rows = engine.state.rows("SELECT id, relative_path FROM artifacts ORDER BY id")
            artifacts = {str(r["relative_path"]): str(r["id"]) for r in rows}
            write_events(
                engine.state,
                [
                    InteractionEvent(
                        kb_id=config.knowledge_base_id,
                        operation="/search",
                        modality="ui",
                        principal="user:ci",
                        session_id=f"ci-{index % 3}",
                        trace_id=f"{index:032x}",
                        span_id=f"{index:016x}",
                        started_at=f"2026-01-01T00:00:{index:02d}.000000Z",
                        status="ok",
                        duration_ms=9.0,
                        query_text=query,
                        result_paths=["invoice.md"],
                        result_ids=[artifacts.get("invoice.md", "")],
                        result_count=1,
                        top_score=0.8,
                    )
                    for index, query in enumerate(QUERIES)
                ],
            )
            for index, (query, path, event) in enumerate(
                [
                    (QUERIES[0], "invoice.md", "explicit_accept"),
                    (QUERIES[0], "legacy.md", "explicit_reject"),
                    (QUERIES[1], "invoice.md", "selected"),
                    (QUERIES[2], "runbook.md", "downstream_success"),
                ]
            ):
                evaluation.record_evidence(
                    engine.state,
                    config,
                    query=query,
                    target_id=artifacts[path],
                    event_type=event,
                    principal="user:ci",
                    session_id=f"ci-{index % 3}",
                    interaction_id=f"ci-call-{index}",
                )
            print("seeded", len(QUERIES), "queries and 4 proofs", flush=True)

        stall = float(os.environ.get("PHEASANT_EVAL_STALL", "0") or 0)
        if stall > 0:
            # Slow the replay loop so the smoke script has a window in which to
            # stop this container mid-batch. Patched here rather than in the
            # product: a sleep the shipped code could reach is a sleep that
            # eventually fires in production.
            from pheasant.evaluation.replay import ReplayEngine

            original = ReplayEngine.replay_variant

            def slow(self, cohort, variant):  # type: ignore[no-untyped-def]
                time.sleep(stall)
                return original(self, cohort, variant)

            ReplayEngine.replay_variant = slow  # type: ignore[method-assign]
            print(f"replay slowed by {stall}s per pair", flush=True)

        outcome = evaluation.run(
            engine,
            on_progress=lambda phase, detail: print(f"  {phase}: {detail}", flush=True),
        )
        print(
            json.dumps(
                {
                    "run_id": outcome.run_id,
                    "status": outcome.status,
                    "attempts": outcome.attempts,
                    "resumed_replays": outcome.resumed_replays,
                    "gates_passed": outcome.gates_passed,
                }
            ),
            flush=True,
        )
        return 0
    finally:
        engine.close()


if __name__ == "__main__":
    sys.exit(main())
