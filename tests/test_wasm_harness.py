"""Step 34.1 acceptance: WASM host harness (empty sandbox).

A minimal "hello wasm" guest runs; a fuel-exhaustion case and a
memory-cap-exceeded case both fail closed. Guests here are hand-authored
WAT (``tests/fixtures/wasm/*.wat``) — ``wasmtime`` compiles WAT directly,
so no Rust/TinyGo toolchain is needed to build or run them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("wasmtime", reason="wasmtime not installed (pip install 'syncsage[wasm]')")

from syncsage.sandbox.wasm_runtime import (  # noqa: E402
    HostCapabilities,
    SandboxFuelExhausted,
    SandboxLimits,
    SandboxMemoryLimitExceeded,
    WasmSandbox,
)

FIXTURES = Path(__file__).parent / "fixtures" / "wasm"


def _wat(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_hello_wasm_runs() -> None:
    sandbox = WasmSandbox(_wat("hello.wat"))
    assert sandbox.call("add", 2, 3) == 5


def test_fuel_exhaustion_fails_closed() -> None:
    sandbox = WasmSandbox(_wat("fuel_spin.wat"), limits=SandboxLimits(fuel=10_000))
    with pytest.raises(SandboxFuelExhausted):
        sandbox.call("spin")


def test_memory_cap_exceeded_fails_closed() -> None:
    sandbox = WasmSandbox(
        _wat("memory_hog.wat"),
        limits=SandboxLimits(max_memory_bytes=2 * 65536),
    )
    with pytest.raises(SandboxMemoryLimitExceeded):
        sandbox.call("blow_up")


def test_host_fetch_round_trip_for_an_allowed_host() -> None:
    capabilities = HostCapabilities(
        allowed_hosts=frozenset({"allowed.example"}),
        fetcher=lambda url: b"hello from host",
    )
    sandbox = WasmSandbox(_wat("fetch_echo.wat"), capabilities=capabilities)
    n = sandbox.call("fetch_test")
    assert n == len(b"hello from host")
    assert sandbox.read_memory(100, n) == b"hello from host"


def test_host_fetch_denied_for_a_host_outside_the_allowlist() -> None:
    capabilities = HostCapabilities(allowed_hosts=frozenset(), fetcher=lambda url: b"unreachable")
    sandbox = WasmSandbox(_wat("fetch_echo.wat"), capabilities=capabilities)
    assert sandbox.call("fetch_test") < 0


def test_sandbox_requires_the_wasm_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    from syncsage.sandbox import wasm_runtime

    monkeypatch.setattr(wasm_runtime, "wasmtime", None)
    with pytest.raises(wasm_runtime.WasmRuntimeUnavailable):
        wasm_runtime.WasmSandbox(_wat("hello.wat"))
