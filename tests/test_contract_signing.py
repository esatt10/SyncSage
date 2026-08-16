"""Synapse 24.4 — region-side Ed25519 contract signing.

Offline + deterministic test keypair. Two halves:

1. The publisher signs ``integrity.signature`` when ``synapse.signing_key_ref``
   is set, and is unsigned (standalone-safe) when it is not.
2. The vendored ``signed-demo-region`` fixture — produced by *this* signing
   path — verifies under a plain ``cryptography`` check over the same canonical
   body bytes the router uses, and a tampered copy fails. This is the pheasant
   side of the cross-repo signing-parity guarantee (the Flock side asserts the
   sibling router's ``verify_signature`` accepts the same fixture).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.synapse import signing
from pheasant.synapse.publisher import ContractPublisher, contract_path
from pheasant.sync.engine import SyncEngine
from tests.conftest import run_sync

pytest.importorskip("cryptography")
from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PublicKey,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SIGNED_FIXTURE = REPO_ROOT / "contracts" / "fixtures" / "signed-demo-region.v1.contract.json"

TEST_SEED = bytes(range(32))
TEST_KEY_B64 = base64.b64encode(TEST_SEED).decode("ascii")
# Public key matching TEST_SEED (used by the Flock router trust store too).
TEST_PUBKEY_B64 = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="


def _verify(body_with_integrity: dict[str, Any], pubkey_b64: str) -> bool:
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
    sig = base64.b64decode(body_with_integrity["integrity"]["signature"])
    try:
        pub.verify(sig, signing.signing_bytes(body_with_integrity))
    except InvalidSignature:
        return False
    return True


def _no_embed_engine(tmp_path: Path, **synapse: Any) -> SyncEngine:
    notes = tmp_path / "ws" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "a.md").write_text("# A\n\nThe automobile fleet needs inspection.\n", "utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "signing-region",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "ws"),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [{"name": "notes", "type": "markdown_folder", "path": str(notes)}],
        }
    )
    engine = SyncEngine(config)
    engine.config.synapse.publish = True
    for key, value in synapse.items():
        setattr(engine.config.synapse, key, value)
    return engine


# --------------------------------------------------------------------------- publisher


def test_publisher_signs_when_key_ref_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PHEASANT_SIGNING_KEY", TEST_KEY_B64)
    engine = _no_embed_engine(tmp_path, signing_key_ref="env://PHEASANT_SIGNING_KEY")
    run_sync(engine, source_name="notes", mode="full")
    payload = json.loads(contract_path(engine.config.pheasant.state_path).read_text("utf-8"))
    assert payload["integrity"]["signature"] is not None
    assert _verify(payload, TEST_PUBKEY_B64)


def test_publisher_unsigned_by_default(tmp_path: Path) -> None:
    engine = _no_embed_engine(tmp_path)  # no signing_key_ref
    run_sync(engine, source_name="notes", mode="full")
    payload = json.loads(contract_path(engine.config.pheasant.state_path).read_text("utf-8"))
    assert payload["integrity"]["signature"] is None


def test_signing_does_not_change_content_hash(tmp_path: Path, monkeypatch) -> None:
    """The signature lives outside the hashed body, so signing leaves the hash."""
    monkeypatch.setenv("PHEASANT_SIGNING_KEY", TEST_KEY_B64)
    engine = _no_embed_engine(tmp_path, signing_key_ref="env://PHEASANT_SIGNING_KEY")
    run_sync(engine, source_name="notes", mode="full")
    payload = json.loads(contract_path(engine.config.pheasant.state_path).read_text("utf-8"))
    body = {k: v for k, v in payload.items() if k != "integrity"}
    assert payload["integrity"]["content_hash"] == signing.content_hash(body)


def test_missing_env_key_raises(tmp_path: Path) -> None:
    # The publisher surfaces a clear error; the sync engine treats publication
    # as fail-soft, so exercise the publisher build directly.
    engine = _no_embed_engine(tmp_path, signing_key_ref="env://NOT_SET_ANYWHERE")
    run_sync(engine, source_name="notes", mode="full")
    publisher = ContractPublisher(engine.config, engine.state)
    with pytest.raises(ValueError, match="unset"):
        publisher.build()


# --------------------------------------------------------------------- vendored fixture


def test_vendored_signed_fixture_verifies() -> None:
    payload = json.loads(SIGNED_FIXTURE.read_text(encoding="utf-8"))
    assert payload["integrity"]["signature"]
    assert _verify(payload, TEST_PUBKEY_B64)


def test_vendored_signed_fixture_tamper_fails() -> None:
    payload = json.loads(SIGNED_FIXTURE.read_text(encoding="utf-8"))
    payload["watermark"]["chunk_count"] = 999
    assert not _verify(payload, TEST_PUBKEY_B64)


def test_public_key_b64_matches_known_key() -> None:
    assert signing.public_key_b64(TEST_SEED) == TEST_PUBKEY_B64
