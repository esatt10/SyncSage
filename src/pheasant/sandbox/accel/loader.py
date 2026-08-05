"""Loads the vendored WASM hot-loop accelerator (Synapse Step 34.5).

Two things this module exists to get right, both found empirically in the
34.4 benchmark spike before any production code was written:

1. **The compiled module must never be recompiled per call.** A fresh
   `wasmtime.Module(engine, wasm_bytes)` JIT-compiles from scratch — about
   100ms in the 34.4 measurements, 5-20x slower than the pure-Python
   functions it replaces. `_engine_and_module()` compiles once per
   process and caches it (a lock-guarded module-level singleton), so every
   caller within one process after the first pays only Store/Instance
   creation, not compilation.
2. **A one-shot subprocess (`sync/worker.py` spawns a fresh
   `python -m pheasant sync` per sync — there is no "second call" within
   that process to amortize a compile against) still needs to be fast.**
   wasmtime's AOT module serialization (`Module.serialize`/`deserialize`)
   solves this: compiling once and writing the serialized artifact to a
   machine-local cache file lets every subsequent process — even a brand
   new one — load it in under a millisecond (measured) instead of
   recompiling. The cache is keyed by a hash of the vendored `.wasm` bytes,
   so a package upgrade naturally invalidates it (a new hash, a new
   filename) rather than loading a stale artifact.

The cache lives under the OS temp directory, not any knowledge base's
`/state` — the accelerator is a generic, KB-independent binary (same
compiled code runs for every knowledge base; graph data is a call
argument, never baked into the module), so caching it per-KB would be
wasted redundant compilation across knowledge bases on the same machine
for zero benefit. Cache writes are best-effort: a failure to read or write
the cache file never breaks a caller, it just falls back to an in-memory
compile for that call.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
import threading
from pathlib import Path

from pheasant.sandbox.wasm_runtime import WasmRuntimeUnavailable

logger = logging.getLogger(__name__)

try:
    import wasmtime
except ImportError:  # pragma: no cover - exercised only without the [wasm] extra
    wasmtime = None  # type: ignore[assignment]

_ACCEL_WASM_PATH = Path(__file__).parent / "accel.wasm"

_lock = threading.Lock()
_engine: wasmtime.Engine | None = None
_module: wasmtime.Module | None = None


def _cache_dir() -> Path:
    return Path(tempfile.gettempdir()) / "pheasant-wasm-cache"


def _cache_path(wasm_bytes: bytes) -> Path:
    digest = hashlib.sha256(wasm_bytes).hexdigest()[:16]
    return _cache_dir() / f"accel-{digest}.cwasm"


def _compile_and_cache(engine: wasmtime.Engine, wasm_bytes: bytes) -> wasmtime.Module:
    module = wasmtime.Module(engine, wasm_bytes)
    try:
        cache_path = _cache_path(wasm_bytes)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(".tmp")
        tmp_path.write_bytes(module.serialize())
        tmp_path.replace(cache_path)
    except OSError as exc:
        # Fail-soft: an unwritable temp dir must never break a sync/query,
        # it just means every process in this run pays the JIT cost.
        logger.warning(
            "could not write WASM AOT cache (falling back to per-process compile): %s", exc
        )
    return module


def _load_module(engine: wasmtime.Engine, wasm_bytes: bytes) -> wasmtime.Module:
    cache_path = _cache_path(wasm_bytes)
    if cache_path.exists():
        try:
            return wasmtime.Module.deserialize_file(engine, str(cache_path))
        except wasmtime.WasmtimeError as exc:
            # A wasmtime/engine-config/architecture mismatch fails closed
            # here (never silently loads a wrong-architecture artifact) —
            # recompile and overwrite the stale cache entry.
            logger.info("WASM AOT cache entry unusable, recompiling: %s", exc)
    return _compile_and_cache(engine, wasm_bytes)


def _engine_and_module() -> tuple[wasmtime.Engine, wasmtime.Module]:
    global _engine, _module
    if wasmtime is None:
        raise WasmRuntimeUnavailable(
            "the WASM accelerator needs the optional 'wasmtime' dependency. "
            "Install with: pip install 'pheasant-kb[wasm]'"
        )
    with _lock:
        if _module is None:
            config = wasmtime.Config()
            config.consume_fuel = True
            _engine = wasmtime.Engine(config)
            wasm_bytes = _ACCEL_WASM_PATH.read_bytes()
            _module = _load_module(_engine, wasm_bytes)
        assert _engine is not None
        return _engine, _module


def new_instance() -> tuple[wasmtime.Store, wasmtime.Instance]:
    """A fresh Store+Instance over the (cached, compiled-once) accel module."""
    engine, module = _engine_and_module()
    store = wasmtime.Store(engine)
    # Generous fixed budget: this is trusted, vendored code operating on
    # already-validated in-memory graph data, not an untrusted guest — the
    # 34.1-34.3 fuel/memory ceremony is for sandboxing third-party plugins,
    # which this is not. A budget still exists so a pathological input
    # (e.g. a corrupted graph) fails with a clear error instead of hanging.
    store.set_fuel(50_000_000_000)
    store.set_limits(memory_size=512 * 1024 * 1024)
    linker = wasmtime.Linker(engine)
    instance = linker.instantiate(store, module)
    return store, instance


def reset_for_tests() -> None:
    """Drop the cached Engine/Module (tests only, e.g. after monkeypatching)."""
    global _engine, _module
    with _lock:
        _engine = None
        _module = None
