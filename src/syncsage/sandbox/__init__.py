"""WASM sandbox for untrusted execution (Synapse Phase 34).

See :mod:`syncsage.sandbox.wasm_runtime` for the host harness and
``docs/SYNAPSE_INTEGRATION.md`` §5 for the step contracts.
"""

from __future__ import annotations

from syncsage.sandbox.connector import SandboxedConnector
from syncsage.sandbox.wasm_runtime import (
    HostCapabilities,
    SandboxCapabilityDenied,
    SandboxError,
    SandboxFuelExhausted,
    SandboxLimits,
    SandboxMemoryLimitExceeded,
    SandboxTrapped,
    WasmRuntimeUnavailable,
    WasmSandbox,
    wasm_available,
)

__all__ = [
    "HostCapabilities",
    "SandboxedConnector",
    "SandboxCapabilityDenied",
    "SandboxError",
    "SandboxFuelExhausted",
    "SandboxLimits",
    "SandboxMemoryLimitExceeded",
    "SandboxTrapped",
    "WasmRuntimeUnavailable",
    "WasmSandbox",
    "wasm_available",
]
