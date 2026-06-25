"""Synapse Step 20.1 — vendored semantic-contract parity gate.

The contract schema is canonical in the subjective-retrieval repository;
this repo only vendors the exported JSON Schema + golden fixture under
``contracts/``. These tests fail loudly when the vendored copies drift
from the recorded parity hashes (or from the sibling repo's bytes when it
is checked out alongside). Never fix a failure by editing the vendored
files — re-vendor from subjective-retrieval via its contract-author skill.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONTRACTS = REPO / "contracts"
SCHEMA = CONTRACTS / "semantic_contract.v1.schema.json"
FIXTURE = CONTRACTS / "fixtures" / "demo-region.v1.contract.json"
SIGNED_FIXTURE = CONTRACTS / "fixtures" / "signed-demo-region.v1.contract.json"
PARITY = CONTRACTS / "PARITY.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_vendored_files_exist() -> None:
    assert SCHEMA.is_file()
    assert FIXTURE.is_file()
    assert SIGNED_FIXTURE.is_file()
    assert PARITY.is_file()


def test_vendored_hashes_match_parity_manifest() -> None:
    parity = json.loads(PARITY.read_text(encoding="utf-8"))
    assert _sha256(SCHEMA) == parity["semantic_contract.v1.schema.json"]
    assert _sha256(FIXTURE) == parity["fixtures/demo-region.v1.contract.json"]
    assert _sha256(SIGNED_FIXTURE) == parity["fixtures/signed-demo-region.v1.contract.json"]


def test_schema_shape() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["title"] == "SemanticContract"
    props = schema["properties"]
    for key in (
        "schema_version",
        "kind",
        "kb_id",
        "generated_at",
        "watermark",
        "capabilities",
        "embedding_space",
        "signature",
        "vocabulary",
        "axiom_projections",
        "integrity",
    ):
        assert key in props, f"schema missing top-level property {key!r}"


def test_fixture_is_a_valid_v1_contract() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload["kind"] == "semantic_contract"
    assert payload["schema_version"] == 1
    assert payload["kb_id"]
    assert payload["embedding_space"]["dim"] > 0
    assert payload["signature"]["centroid"]
    assert payload["integrity"]["content_hash"].startswith("sha256:")
    assert len(payload["signature"]["cluster_weights"]) == len(
        payload["signature"]["cluster_centroids"]
    )


def test_parity_with_sibling_subjective_retrieval_when_present() -> None:
    sibling = REPO.parent / "subjective-retrieval"
    if not sibling.is_dir():
        pytest.skip("subjective-retrieval repo not checked out alongside")
    assert (
        sibling / "contracts/semantic_contract.v1.schema.json"
    ).read_bytes() == SCHEMA.read_bytes()
    assert (
        sibling / "tests/fixtures/contracts/demo-region.v1.contract.json"
    ).read_bytes() == FIXTURE.read_bytes()
    assert (
        sibling / "tests/fixtures/contracts/signed-demo-region.v1.contract.json"
    ).read_bytes() == SIGNED_FIXTURE.read_bytes()
    assert (sibling / "contracts/PARITY.json").read_bytes() == PARITY.read_bytes()
