"""Step 34.5 acceptance: the AOT module cache that makes acceleration fast.

The 34.4 benchmark spike's most important finding: a fresh JIT compile per
call is 5-20x *slower* than pure Python. `sandbox.accel.loader` solves this
with wasmtime's AOT module serialization, verified here to actually:
(1) get used (a cache file appears after first load), (2) actually be used
on the next load (not silently recompiling every time), and (3) fail
closed — never load a corrupt/mismatched cache silently — falling back to
a fresh compile instead.
"""

from __future__ import annotations

import pytest

pytest.importorskip("wasmtime", reason="wasmtime not installed (pip install 'syncsage[wasm]')")

from syncsage.sandbox.accel import loader  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_loader_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Each test gets its own cache dir and a fresh process-wide singleton,
    # so tests can't see each other's cached artifacts or engine instances.
    monkeypatch.setattr(loader, "_cache_dir", lambda: tmp_path / "wasm-cache")
    loader.reset_for_tests()
    yield
    loader.reset_for_tests()


def test_first_load_writes_an_aot_cache_file() -> None:
    cache_dir = loader._cache_dir()
    assert not cache_dir.exists() or not any(cache_dir.iterdir())

    store, instance = loader.new_instance()
    assert instance.exports(store)["resolve_cross_source"] is not None

    assert cache_dir.exists()
    cached_files = list(cache_dir.glob("*.cwasm"))
    assert len(cached_files) == 1


def test_second_process_wide_load_reuses_the_singleton_not_the_cache_file() -> None:
    # Within one process, _engine_and_module() is a singleton — the second
    # call must not even touch the cache file, just reuse the compiled
    # Module in memory.
    loader.new_instance()
    cache_path = next(loader._cache_dir().glob("*.cwasm"))
    written_at = cache_path.stat().st_mtime

    loader.new_instance()
    assert cache_path.stat().st_mtime == written_at, (
        "second call must not recompile or rewrite the cache"
    )


def test_a_fresh_process_would_deserialize_the_cache_not_recompile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Prime the cache, then simulate "a new process" by resetting the
    # singleton and asserting the module loads from the cache file (not a
    # fresh compile) — patch Module() to fail loudly if it's ever called
    # again, so a silent fall-through to recompiling would fail the test.
    loader.new_instance()
    loader.reset_for_tests()

    import wasmtime

    original_module_init = wasmtime.Module.__init__

    def _fail_if_compiled_from_scratch(self, engine, wasm):
        raise AssertionError("recompiled from raw .wasm bytes instead of using the AOT cache")

    monkeypatch.setattr(wasmtime.Module, "__init__", _fail_if_compiled_from_scratch)
    try:
        store, instance = loader.new_instance()
        assert instance.exports(store)["scan_edges"] is not None
    finally:
        monkeypatch.setattr(wasmtime.Module, "__init__", original_module_init)


def test_a_corrupt_cache_file_fails_closed_and_recompiles() -> None:
    loader.new_instance()
    cache_path = next(loader._cache_dir().glob("*.cwasm"))
    cache_path.write_bytes(b"not a valid precompiled wasmtime module")
    loader.reset_for_tests()

    # Must not raise, and must still produce a working instance — the
    # corrupt entry is detected and transparently recompiled.
    store, instance = loader.new_instance()
    assert instance.exports(store)["resolve_cross_source"] is not None
