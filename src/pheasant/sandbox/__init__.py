"""WASM sandbox for untrusted execution (Synapse Phase 34).

See :mod:`pheasant.sandbox.wasm_runtime` for the host harness and
``docs/SYNAPSE_INTEGRATION.md`` §5 for the step contracts.
"""

from __future__ import annotations

from pheasant.sandbox.connector import SandboxedConnector
from pheasant.sandbox.wasm_runtime import (
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
