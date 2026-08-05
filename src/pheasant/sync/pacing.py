"""Keeping the server responsive while it indexes.

Indexing is pure Python and CPU-bound — parsing, chunking, graph building,
enrichment — so under the GIL it starves the threads serving HTTP and MCP.
Measured on a live index of a 2,000-file repository: a single model call took
0.57s with the server idle and **107s** while a sync ran, and search latency
swung by 4x. The work itself was not slow; it could not get scheduled.

The fix is cooperative: sleep briefly between units of work. A sleep releases
the GIL, so a request thread waiting to run gets its turn. At the default of
1ms per artifact this adds around two seconds to a 2,000-file sync — far
inside the noise — and takes the API from unusable to responsive.

Set ``PHEASANT_SYNC_YIELD_MS=0`` to turn it off, for a batch index with nobody
watching.
"""

from __future__ import annotations

import os
import time

SYNC_YIELD_S = max(0.0, float(os.environ.get("PHEASANT_SYNC_YIELD_MS", "1") or 0)) / 1000.0


def serve_yield() -> None:
    """Give request threads a turn. Called between units of indexing work."""

    if SYNC_YIELD_S:
        time.sleep(SYNC_YIELD_S)
