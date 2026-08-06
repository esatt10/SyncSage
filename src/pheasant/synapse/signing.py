"""Ed25519 contract signing for the region publisher (Synapse 24.4).

The publisher signs the contract's ``integrity.signature`` when
``synapse.signing_key_ref`` is configured. The signature covers the **exact
same canonical body bytes** that ``publisher._content_hash`` digests — the body
with ``integrity`` excluded, ``sort_keys=True``, compact separators,
``ensure_ascii=False`` — so the sibling router's
``SemanticContract.verify_signature`` (which signs ``signing_bytes()``, the
identical serialization) accepts it. This byte-level agreement is the
cross-repo crypto contract; it is guarded by ``tests/test_contract_parity.py``
on this side and the parity test in pheasant-flock.

Key handling
------------
``signing_key_ref`` is a secret *reference*, not the key. We resolve it through
a tiny env-var indirection (``env://NAME`` or a bare ``NAME``) so the plaintext
seed lives only in the process environment, never in the YAML config or on
disk. The resolved value is the base64 of a 32-byte raw Ed25519 seed.

The ``cryptography`` import is gated behind the optional ``[a2a]`` extra: a
region that never sets ``signing_key_ref`` never imports it, and a region that
*does* but lacks the extra gets an actionable error naming the install.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

_A2A_HINT = (
    "Ed25519 contract signing requires the cryptography package; "
    "install it with: pip install 'pheasant-kb[a2a]'"
)


def _require_crypto() -> None:
    try:
        import cryptography  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised only bare
        raise ModuleNotFoundError(_A2A_HINT) from exc


def signing_bytes(body: dict[str, Any]) -> bytes:
    """Canonical body bytes the signature (and content hash) cover.

    Byte-identical to ``publisher._content_hash``'s pre-image: the body with
    ``integrity`` dropped, ``sort_keys=True``, ``separators=(",", ":")``,
    ``ensure_ascii=False``.
    """
    payload = {k: v for k, v in body.items() if k != "integrity"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return canonical.encode("utf-8")


def content_hash(body: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(signing_bytes(body)).hexdigest()


def resolve_signing_key_ref(ref: str) -> bytes:
    """Resolve a ``signing_key_ref`` to a 32-byte raw Ed25519 seed.

    Accepts ``env://NAME`` or a bare ``NAME`` (read from the environment). The
    env value is the base64 of the 32-byte seed.
    """
    name = ref[len("env://") :] if ref.startswith("env://") else ref
    secret = os.environ.get(name)
    if not secret:
        raise ValueError(f"signing_key_ref {ref!r} resolves to env var {name!r}, which is unset")
    raw = base64.b64decode(secret.strip().encode("ascii"), validate=True)
    if len(raw) != 32:
        raise ValueError(f"signing key from {ref!r} must decode to 32 bytes, got {len(raw)}")
    return raw


def sign_body(body: dict[str, Any], seed: bytes) -> str:
    """Ed25519-sign the canonical body bytes; return a base64 signature."""
    _require_crypto()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    return base64.b64encode(private_key.sign(signing_bytes(body))).decode("ascii")


def public_key_b64(seed: bytes) -> str:
    """Base64 raw public key for a seed — used to populate the router trust store."""
    _require_crypto()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    pub = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(pub).decode("ascii")


__all__ = [
    "content_hash",
    "public_key_b64",
    "resolve_signing_key_ref",
    "sign_body",
    "signing_bytes",
]
