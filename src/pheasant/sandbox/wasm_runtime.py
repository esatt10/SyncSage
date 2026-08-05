"""WASM host harness (Synapse Step 34.1).

Wraps ``wasmtime`` behind a small, capability-scoped surface so guest code
(a sandboxed connector, later an accelerated hot loop) never gets ambient
env/filesystem/network access — only what the host explicitly grants via
:class:`HostCapabilities`. Two independent limits fail a guest call closed
instead of hanging or exhausting host memory:

- **Fuel** (``Config.consume_fuel`` + ``Store.set_fuel``): a deterministic,
  budget-based CPU limit. Preferred over a wall-clock timeout because a
  fuel budget is reproducible across machines/runs, matching the
  determinism guarantee the rest of the indexing path holds to (rule 1).
- **Linear memory cap** (``Store.set_limits``): bounds how much memory one
  guest instance may grow into. A guest that ignores a denied
  ``memory.grow`` and keeps writing past its actual (unchanged) memory
  size hits a host-observable ``MEMORY_OUT_OF_BOUNDS``/
  ``ALLOCATION_TOO_LARGE`` trap — there is no silent overrun.

Gated import: importing this module never requires ``wasmtime`` to be
installed; only *using* :class:`WasmSandbox` does
(``pip install 'pheasant-kb[wasm]'``). A region that never sets
``connector.runtime: sandboxed`` is byte-identical to pre-34.1.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from pheasant.sync.connectors import ConnectorUnavailable, require_fetchable_url

logger = logging.getLogger(__name__)

try:
    import wasmtime
except ImportError:  # pragma: no cover - exercised only without the [wasm] extra
    wasmtime = None  # type: ignore[assignment]


def wasm_available() -> bool:
    """Whether the optional ``wasmtime`` dependency is importable."""
    return wasmtime is not None


class WasmRuntimeUnavailable(RuntimeError):
    """Raised when a WASM sandbox is used without the ``[wasm]`` extra installed."""


class SandboxError(RuntimeError):
    """Base class for guest-execution failures. The host always fails closed."""


class SandboxFuelExhausted(SandboxError):
    """The guest consumed its entire CPU budget without finishing."""


class SandboxMemoryLimitExceeded(SandboxError):
    """The guest hit its per-instance linear-memory cap."""


class SandboxCapabilityDenied(SandboxError):
    """The guest asked the host for something outside its capability grant."""


class SandboxTrapped(SandboxError):
    """Any other WASM trap (e.g. bad guest code) — still fails closed."""


def _require_wasmtime() -> None:
    if wasmtime is None:
        raise WasmRuntimeUnavailable(
            "the WASM sandbox needs the optional 'wasmtime' dependency. "
            "Install with: pip install 'pheasant-kb[wasm]'"
        )


@dataclass(frozen=True)
class SandboxLimits:
    """Per-instance resource budget.

    Defaults are generous enough for a small deterministic guest (a
    connector's list/read call, or one hot-loop invocation) while still
    bounding a runaway or malicious one. Callers doing real work should size
    ``fuel`` to their guest's expected instruction count, not rely on the
    default.
    """

    fuel: int = 10_000_000
    max_memory_bytes: int = 64 * 1024 * 1024


HostFetcher = Callable[[str], bytes]


@dataclass
class HostCapabilities:
    """The capability-scoped host surface a guest may call.

    No ambient WASI env/filesystem/network: a guest reaches the outside
    world only through ``host_fetch``, and only for a URL whose hostname is
    in ``allowed_hosts`` (case-insensitive exact match; an empty allowlist
    denies every fetch). Secrets (API tokens, etc.) are resolved by
    ``fetcher`` on the host side — the guest only ever sees a URL string and
    the fetched bytes, never the credential used to get them.
    """

    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    fetcher: HostFetcher | None = None

    def check_and_fetch(self, url: str) -> bytes:
        try:
            require_fetchable_url(url)
        except ConnectorUnavailable as exc:
            raise SandboxCapabilityDenied(str(exc)) from exc
        host = (urlsplit(url).hostname or "").lower()
        if host not in self.allowed_hosts:
            raise SandboxCapabilityDenied(
                f"guest requested {url!r}; host {host!r} is not in connector.allowed_hosts"
            )
        if self.fetcher is None:
            raise SandboxCapabilityDenied("no host_fetch implementation configured for this source")
        return self.fetcher(url)


_TRAP_EXCEPTIONS: dict[str, type[SandboxError]] = {}


def _trap_exception_map() -> dict[str, type[SandboxError]]:
    # Built lazily: wasmtime.TrapCode only exists once the optional dep is installed.
    if not _TRAP_EXCEPTIONS and wasmtime is not None:
        _TRAP_EXCEPTIONS.update(
            {
                wasmtime.TrapCode.OUT_OF_FUEL.name: SandboxFuelExhausted,
                wasmtime.TrapCode.MEMORY_OUT_OF_BOUNDS.name: SandboxMemoryLimitExceeded,
                wasmtime.TrapCode.ALLOCATION_TOO_LARGE.name: SandboxMemoryLimitExceeded,
            }
        )
    return _TRAP_EXCEPTIONS


class WasmSandbox:
    """One guest module, instantiated under fuel + memory limits.

    ``wasm`` is either WAT text or a compiled wasm binary (``wasmtime``
    auto-detects the format) — reference and test guests in this repo are
    hand-authored WAT so no Rust/TinyGo toolchain is required to build or
    run them; third-party connector authors may still compile from any
    ``wasm32-wasip1``-targeting language.
    """

    def __init__(
        self,
        wasm: str | bytes,
        *,
        limits: SandboxLimits | None = None,
        capabilities: HostCapabilities | None = None,
    ) -> None:
        _require_wasmtime()
        assert wasmtime is not None  # narrows for type checkers after _require_wasmtime()
        self.limits = limits or SandboxLimits()
        self.capabilities = capabilities or HostCapabilities()
        self._pending_fetch: bytes | None = None

        config = wasmtime.Config()
        config.consume_fuel = True
        self._engine = wasmtime.Engine(config)
        self._module = wasmtime.Module(self._engine, wasm)
        self._store = wasmtime.Store(self._engine)
        self._store.set_fuel(self.limits.fuel)
        self._store.set_limits(memory_size=self.limits.max_memory_bytes)

        linker = wasmtime.Linker(self._engine)
        i32 = wasmtime.ValType.i32()
        linker.define_func(
            "env",
            "host_fetch_len",
            wasmtime.FuncType([i32, i32], [i32]),
            self._host_fetch_len,
            access_caller=True,
        )
        linker.define_func(
            "env",
            "host_fetch_read",
            wasmtime.FuncType([i32, i32], [i32]),
            self._host_fetch_read,
            access_caller=True,
        )
        try:
            self._instance = linker.instantiate(self._store, self._module)
        except wasmtime.WasmtimeError as exc:
            # No ambient WASI env/filesystem/network is ever wired into the
            # linker (rule: capability-scoped host surface only), so a guest
            # that imports e.g. wasi_snapshot_preview1.environ_get fails to
            # instantiate at all with wasmtime's own "unknown import"
            # message — proving there is no ambient capability to leak from,
            # not merely that a call to use one would be refused.
            raise SandboxCapabilityDenied(
                f"guest module requires a capability this sandbox does not grant: {exc}"
            ) from exc

    def _guest_memory(self, caller: Any) -> Any:
        memory = caller.get("memory")
        if memory is None:
            raise SandboxError("guest module does not export linear memory named 'memory'")
        return memory

    def _host_fetch_len(self, caller: Any, url_ptr: int, url_len: int) -> int:
        memory = self._guest_memory(caller)
        raw = bytes(memory.read(caller, url_ptr, url_ptr + url_len))
        url = raw.decode("utf-8", errors="replace")
        try:
            self._pending_fetch = self.capabilities.check_and_fetch(url)
        except SandboxCapabilityDenied as exc:
            logger.warning("wasm guest host_fetch denied: %s", exc)
            self._pending_fetch = None
            return -1
        return len(self._pending_fetch)

    def _host_fetch_read(self, caller: Any, out_ptr: int, out_len: int) -> int:
        data = self._pending_fetch
        if data is None or len(data) > out_len:
            return -1
        memory = self._guest_memory(caller)
        memory.write(caller, data, out_ptr)
        self._pending_fetch = None
        return len(data)

    def call(self, export_name: str, *args: int) -> Any:
        """Invoke an exported guest function, translating traps to :class:`SandboxError`."""
        exports = self._instance.exports(self._store)
        func = exports.get(export_name)
        if func is None:
            raise SandboxError(f"guest module has no export {export_name!r}")
        try:
            return func(self._store, *args)
        except wasmtime.Trap as trap:
            code = trap.trap_code
            name = code.name if code is not None else None
            exc_type = _trap_exception_map().get(name, SandboxTrapped)
            raise exc_type(f"{export_name!r} trapped ({name or 'unknown'}): {trap}") from trap

    def read_memory(self, offset: int, length: int) -> bytes:
        exports = self._instance.exports(self._store)
        memory = exports.get("memory")
        if memory is None:
            raise SandboxError("guest module does not export linear memory named 'memory'")
        return bytes(memory.read(self._store, offset, offset + length))

    def write_memory(self, offset: int, data: bytes) -> None:
        exports = self._instance.exports(self._store)
        memory = exports.get("memory")
        if memory is None:
            raise SandboxError("guest module does not export linear memory named 'memory'")
        memory.write(self._store, data, offset)

    def fuel_consumed(self) -> int:
        """Fuel spent so far — useful for the 34.4 benchmark spike."""
        return self.limits.fuel - self._store.get_fuel()
