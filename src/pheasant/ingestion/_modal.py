"""Shared helpers for multi-modal ingestion (Synapse step 25.4).

The image captioner (session A) and the audio transcriber (session B) share
the same two primitives: an authored ``<file>.<kind>.txt`` sidecar always wins,
and otherwise a deterministic blake2b fingerprint of the raw bytes anchors a
template so the same file produces the same indexable text. Factoring them here
keeps both modality handlers in lock-step without either reimplementing the
other; it is purely additive — neither handler's observable behavior changes.
"""

from __future__ import annotations

import hashlib


def sidecar_text(sidecar: bytes | None) -> str | None:
    """Decode an authored sidecar's bytes to stripped text, or ``None``.

    Empty/whitespace-only sidecars decode to ``None`` so the caller falls back
    to the deterministic template.
    """

    if sidecar is None:
        return None
    text = sidecar.decode("utf-8", errors="ignore").strip()
    return text or None


def stub_fingerprint(content: bytes) -> str:
    """Short, stable blake2b digest of raw bytes for stub templates."""

    return hashlib.blake2b(content, digest_size=6).hexdigest()
