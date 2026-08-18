"""Step 34.3 acceptance: adversarial limit enforcement.

Four adversarial shapes, each proven to fail closed — no hang, no secret
leak, no partial write:

1. Unbounded memory allocation.
2. An infinite loop (CPU exhaustion).
3. Reading the ambient process environment.
4. Calling a non-allowlisted host (including SSRF-shaped disguises: a
   ``file://`` local-path read, and a cloud-metadata-endpoint host).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("wasmtime", reason="wasmtime not installed (pip install 'pheasant-kb[wasm]')")

from pheasant.sandbox.wasm_runtime import (  # noqa: E402
    HostCapabilities,
    SandboxCapabilityDenied,
    SandboxFuelExhausted,
    SandboxLimits,
    SandboxMemoryLimitExceeded,
    WasmSandbox,
)

FIXTURES = Path(__file__).parent / "fixtures" / "wasm"

#: Generous wall-clock bound proving these cases fail fast rather than hang
#: — fuel/memory limits are what make this true, this just makes it observable.
NO_HANG_BUDGET_SECONDS = 5.0


def _wat(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_unbounded_memory_allocation_fails_closed() -> None:
    sandbox = WasmSandbox(
        _wat("memory_hog.wat"),
        limits=SandboxLimits(max_memory_bytes=2 * 65536),
    )
    started = time.perf_counter()
    with pytest.raises(SandboxMemoryLimitExceeded):
        sandbox.call("blow_up")
    assert time.perf_counter() - started < NO_HANG_BUDGET_SECONDS


def test_infinite_loop_fails_closed() -> None:
    sandbox = WasmSandbox(_wat("fuel_spin.wat"), limits=SandboxLimits(fuel=50_000))
    started = time.perf_counter()
    with pytest.raises(SandboxFuelExhausted):
        sandbox.call("spin")
    assert time.perf_counter() - started < NO_HANG_BUDGET_SECONDS


def test_ambient_env_read_fails_closed_at_instantiation() -> None:
    # No wasi_snapshot_preview1 import is ever wired into the linker, so a
    # guest declaring it needs one fails to load at all — there is no
    # ambient environment for a "call and see" attempt to leak from.
    with pytest.raises(SandboxCapabilityDenied):
        WasmSandbox(_wat("wasi_env_leak.wat"))


def test_non_allowlisted_host_calls_fail_closed_no_secret_leak() -> None:
    calls: list[str] = []

    def spy_fetcher(url: str) -> bytes:
        calls.append(url)
        return b"TOP-SECRET-SHOULD-NEVER-BE-SEEN"

    # Empty allowlist: nothing is reachable, including a same-scheme host
    # that simply isn't named.
    capabilities = HostCapabilities(allowed_hosts=frozenset(), fetcher=spy_fetcher)
    sandbox = WasmSandbox(_wat("host_fetch_ssrf.wat"), capabilities=capabilities)

    file_result = sandbox.call("try_file_scheme")
    metadata_result = sandbox.call("try_metadata_host")

    assert file_result < 0, "file:// scheme disguise must be denied"
    assert metadata_result < 0, "cloud metadata-endpoint host must be denied"
    assert calls == [], "the fetcher must never run for a denied request — no secret leak"


def test_denied_fetch_leaves_no_partial_data_in_guest_memory() -> None:
    # fetch_echo.wat is the fixture that actually completes the two-call
    # protocol (host_fetch_len, then host_fetch_read into offset 100) when
    # allowed — used here specifically to prove a denial never reaches the
    # copy step, unlike host_fetch_ssrf.wat's guest, which never calls
    # host_fetch_read at all and so can't demonstrate this property.
    capabilities = HostCapabilities(allowed_hosts=frozenset(), fetcher=lambda url: b"secret-bytes")
    sandbox = WasmSandbox(_wat("fetch_echo.wat"), capabilities=capabilities)
    result = sandbox.call("fetch_test")
    assert result < 0, "the guest never got a length to copy"
    # The guest checks the negative host_fetch_len result and returns early
    # (fetch_echo.wat's ABI), so no bytes were ever copied into its memory.
    assert sandbox.read_memory(100, len(b"secret-bytes")) == b"\x00" * len(b"secret-bytes")


def test_a_wasmtime_error_is_translated_like_a_trap() -> None:
    """The recorded "raw wasmtime type escapes" bug, closed and pinned.

    `wasmtime.Trap` and `wasmtime.WasmtimeError` are **siblings** —
    `issubclass(Trap, WasmtimeError)` is False — so a host that catches only
    `Trap` leaks a raw runtime type whenever wasmtime reports a failure as the
    other one. CLAUDE.md recorded exactly that on 2026-08-10 as a real
    pre-existing bug and it stayed open, because nothing exercised the case:
    the fixtures all happen to raise `Trap` on the installed version.

    So this test forces the other sibling. The point is not which exception
    wasmtime picks today — it is that the module's contract (*guest failures
    are SandboxError*) does not depend on that choice.
    """

    import wasmtime

    from pheasant.sandbox.wasm_runtime import SandboxError, guest_failures, translate_trap

    assert not issubclass(wasmtime.Trap, wasmtime.WasmtimeError), (
        "wasmtime changed its hierarchy; re-check that both are still caught"
    )
    assert set(guest_failures()) == {wasmtime.Trap, wasmtime.WasmtimeError}

    # A WasmtimeError has no trap_code, so it must degrade to SandboxTrapped
    # rather than raising AttributeError inside the translator.
    translated = translate_trap(wasmtime.WasmtimeError("guest exploded"), "some_export")
    assert isinstance(translated, SandboxError)
    assert "some_export" in str(translated)
    assert "guest exploded" in str(translated)


def test_a_guest_call_never_leaks_a_raw_wasmtime_exception() -> None:
    """End to end: whatever the export raises, the caller sees SandboxError."""

    import wasmtime

    from pheasant.sandbox.wasm_runtime import SandboxError

    sandbox = WasmSandbox(_wat("hello.wat"))

    for raised in (wasmtime.WasmtimeError("boom"), wasmtime.Trap("boom")):

        class Exploding:
            def __init__(self, exc: BaseException) -> None:
                self.exc = exc

            def get(self, _name: str):
                def call(*_args, **_kwargs):
                    raise self.exc

                return call

        sandbox._instance = type(
            "Stub", (), {"exports": lambda _self, _store, box=Exploding(raised): box}
        )()
        with pytest.raises(SandboxError):
            sandbox.call("hello")
