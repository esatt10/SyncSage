"""Region-side Synapse integration (Synapse step 21.5).

This package is the pheasant *region* half of the Synapse boundary. It is
deliberately self-contained:

- It never imports ``pheasant_flock`` (the router lives in the sibling
  repo; the boundary is contract JSON + HTTP — see ``CLAUDE.md`` §1.1).
- The semantic contract is built by hand to match the vendored JSON Schema
  ``contracts/semantic_contract.v1.schema.json`` exactly, including the
  fp16-base64 vector codec, the u64-base64 + blake2b MinHash scheme, and the
  ``sha256:``-prefixed canonical-JSON content hash the sibling's
  ``SemanticContract.seal()`` produces.
- Everything is a no-op when ``synapse.publish`` is off / ``synapse.router_url``
  is unset, *except* the local ``<state>/contract.latest.json`` write and the
  NDJSON event log, which are harmless and useful standalone.
"""

from __future__ import annotations

from pheasant.synapse.events import EventStream, RouterWebhook
from pheasant.synapse.publisher import (
    CONTRACT_FILENAME,
    ContractPublisher,
    contract_path,
    load_contract_text,
)

__all__ = [
    "CONTRACT_FILENAME",
    "ContractPublisher",
    "EventStream",
    "RouterWebhook",
    "contract_path",
    "load_contract_text",
]
