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


# ---------------------------------------------------------------------------
# Principal-assertion verification (security audit finding C3, 2026-08-23)
# ---------------------------------------------------------------------------
#
# A second, independent use of the same Ed25519 primitive above: where
# `sign_body`/`content_hash` let *this region* sign the contract it
# publishes, the functions below let this region *verify* a signed
# assertion of caller identity presented to it — the primitive
# `security.principal_source: "signed"` needs to make `security.acl_enforced`
# mean something for a caller reached through the Synapse router, which is
# in a position to authenticate the original caller and assert who they
# are to each region it fans out to. Same key-handling convention as
# `resolve_signing_key_ref`: a reference, resolved through env-var
# indirection, never the key itself in config or on disk — except this side
# resolves a *public* key, since this region only ever verifies, never signs,
# a principal assertion.


class PrincipalSignatureError(ValueError):
    """A signed principal assertion failed verification."""


def resolve_verifying_key_ref(ref: str) -> bytes:
    """Resolve ``security.principal_signing_public_key_ref`` to a 32-byte
    raw Ed25519 public key. Mirrors :func:`resolve_signing_key_ref` exactly,
    but for a public rather than a private key."""
    name = ref[len("env://") :] if ref.startswith("env://") else ref
    secret = os.environ.get(name)
    if not secret:
        raise PrincipalSignatureError(
            f"principal_signing_public_key_ref {ref!r} resolves to env var {name!r}, which is unset"
        )
    try:
        raw = base64.b64decode(secret.strip().encode("ascii"), validate=True)
    except (ValueError, TypeError) as exc:
        raise PrincipalSignatureError(
            f"principal_signing_public_key_ref {ref!r} is not valid base64"
        ) from exc
    if len(raw) != 32:
        raise PrincipalSignatureError(
            f"public key from {ref!r} must decode to 32 bytes, got {len(raw)}"
        )
    return raw


def verify_signed_principal(
    assertion_b64: str, signature_b64: str, public_key_ref: str
) -> dict[str, Any]:
    """Verify a base64 JSON principal assertion signed with Ed25519.

    ``assertion_b64`` is the base64 of a canonical JSON object
    ``{"principal": "...", "groups": [...], "exp": "<ISO instant>"}``
    (``groups`` and ``exp`` optional). ``signature_b64`` is the base64
    Ed25519 signature over those exact decoded bytes — the signer (the
    Synapse router) signs whatever byte string it base64-encoded into
    ``assertion_b64``; this function does not re-derive or re-canonicalize
    anything, it verifies the signature over the bytes as given and only
    then parses them as JSON, so there is exactly one byte string in play
    on both sides.

    Raises :class:`PrincipalSignatureError` on any failure — bad base64, a
    signature that does not verify, malformed or non-dict JSON, no
    ``principal`` claim, or an expired ``exp`` — so callers have exactly one
    exception to catch rather than needing to know which of several things
    can go wrong. ``cryptography``'s Ed25519 verification is constant-time
    internally; no additional care is needed here the way the raw bearer
    tokens elsewhere in this codebase need ``hmac.compare_digest``.
    """
    _require_crypto()
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        public_key_bytes = resolve_verifying_key_ref(public_key_ref)
        signature = base64.b64decode(signature_b64.strip().encode("ascii"), validate=True)
        assertion_bytes = base64.b64decode(assertion_b64.strip().encode("ascii"), validate=True)
    except (ValueError, TypeError) as exc:
        raise PrincipalSignatureError(f"malformed principal assertion or signature: {exc}") from exc
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature, assertion_bytes)
    except InvalidSignature as exc:
        raise PrincipalSignatureError("principal assertion signature did not verify") from exc
    try:
        claims = json.loads(assertion_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PrincipalSignatureError(f"principal assertion is not valid JSON: {exc}") from exc
    if not isinstance(claims, dict) or not claims.get("principal"):
        raise PrincipalSignatureError("principal assertion carries no principal claim")
    expiry = claims.get("exp")
    if expiry:
        from datetime import UTC, datetime

        try:
            expires_at = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
        except ValueError:
            raise PrincipalSignatureError(
                f"unparseable exp in principal assertion: {expiry!r}"
            ) from None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if datetime.now(UTC) > expires_at:
            raise PrincipalSignatureError("principal assertion has expired")
    return claims


__all__ = [
    "PrincipalSignatureError",
    "content_hash",
    "public_key_b64",
    "resolve_signing_key_ref",
    "resolve_verifying_key_ref",
    "sign_body",
    "signing_bytes",
    "verify_signed_principal",
]
