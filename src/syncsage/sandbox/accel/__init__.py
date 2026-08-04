"""WASM-accelerated hot loops (Synapse Step 34.5).

Vendored, generic, knowledge-base-independent `.wasm` binary (see
``accel.wasm`` and ``rust/`` for source) plus Python wrappers that are
drop-in replacements for two pure-Python functions, gated behind config
flags with a pure-Python fallback on any failure. See
``docs/SYNAPSE_INTEGRATION.md`` §5 Step 34.5.
"""

from __future__ import annotations

from syncsage.sandbox.accel.cross_source import resolve_cross_source_edges_wasm
from syncsage.sandbox.accel.relationship_search import scan_edges_wasm

__all__ = ["resolve_cross_source_edges_wasm", "scan_edges_wasm"]
