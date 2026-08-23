"""Hardening for the wasmtime AOT-module disk cache (security audit C5, 2026-08-23).

Both :mod:`pheasant.sandbox.accel.loader` (the trusted hot-loop accelerator)
and :mod:`pheasant.ingestion.extractor_sandbox` (the untrusted-document
sandbox) cache a precompiled WASM module on disk and load it with
``wasmtime.Module.deserialize_file`` — which trusts the file's bytes as
already-validated native machine code, with none of the sandbox's own
fuel/memory ceremony applied to it. The cache directory defaults to the OS
temp directory, which on a shared host is writable by every local user, so
without this module another uid could plant a hostile artifact at the exact
filename this process is about to trust and get it deserialized as machine
code inside the pheasant process.

This module's whole job is to make that impossible without changing either
caller's cache *location* (still keyed by a hash of the vendored ``.wasm``
bytes, still under ``tempfile.gettempdir()``): a directory this process did
not itself create with private permissions is never trusted, and neither is
a file inside it that isn't a regular file this process's own uid owns. The
failure direction only ever goes the safe way — an untrusted cache is
treated as *absent* (falls back to an in-memory compile, a performance
regression) rather than raising, since raising here would turn "someone
else can write to /tmp" into a denial of service on top of the vulnerability
this closes.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def secure_dir(path: Path) -> Path | None:
    """Create/verify ``path`` as a private (mode 0700, owned by us) directory.

    Returns ``path`` when it can be trusted, ``None`` otherwise. Checks the
    *unresolved* entry (``lstat``, not ``stat``) so a symlink planted at
    ``path`` — pointing the cache directory somewhere this process does not
    control — is refused rather than followed.

    ``exist_ok=True`` on an existing directory does not retroactively change
    its mode or owner, which is exactly why the check below runs
    unconditionally after ``mkdir`` rather than only on the fresh-create
    path: a directory an attacker created first, before this process ever
    ran, is exactly the case this function exists to catch.
    """
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        logger.warning("could not prepare WASM cache dir %s: %s", path, exc)
        return None
    if stat.S_ISLNK(info.st_mode):
        logger.warning("WASM cache dir %s is a symlink; refusing to use it", path)
        return None
    if not stat.S_ISDIR(info.st_mode):
        logger.warning("WASM cache dir %s is not a directory; refusing to use it", path)
        return None
    if info.st_uid != os.getuid():
        logger.warning(
            "WASM cache dir %s is owned by uid %d, not this process's uid %d "
            "(a shared temp dir may have been planted by another user); refusing to use it",
            path,
            info.st_uid,
            os.getuid(),
        )
        return None
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning(
            "WASM cache dir %s is group/other-accessible (mode %o); refusing to use it",
            path,
            stat.S_IMODE(info.st_mode),
        )
        return None
    return path


def secure_cache_file(path: Path) -> Path | None:
    """Verify a cache *file* candidate is a regular file this uid alone controls.

    Call only after :func:`secure_dir` has accepted the parent directory —
    this additionally guards against a symlink placed *inside* an
    otherwise-trusted directory pointing the cache filename at a file this
    process does not actually own. A file that does not exist yet is not
    distrusted (there is nothing to distrust); every other failure mode
    returns ``None``.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path
    except OSError as exc:
        logger.warning("could not stat WASM cache file %s: %s", path, exc)
        return None
    if stat.S_ISLNK(info.st_mode):
        logger.warning("WASM cache file %s is a symlink; refusing to use it", path)
        return None
    if not stat.S_ISREG(info.st_mode):
        logger.warning("WASM cache file %s is not a regular file; refusing to use it", path)
        return None
    if info.st_uid != os.getuid():
        logger.warning(
            "WASM cache file %s is owned by uid %d, not this process's uid %d; refusing to use it",
            path,
            info.st_uid,
            os.getuid(),
        )
        return None
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        logger.warning("WASM cache file %s is group/other-writable; refusing to use it", path)
        return None
    return path


def load_or_compile(engine: Any, wasm_bytes: bytes, cache_path: Path) -> Any:
    """AOT-cache-then-compile a WASM module, hardened per this module's docstring.

    Tries the cache first — only when both the cache directory and the cache
    file pass their respective checks above — compiles fresh on any miss,
    architecture/engine mismatch, or untrusted cache, and best-effort writes
    the freshly compiled result back (via a private temp file + atomic
    rename, so a reader never observes a partially-written artifact). One
    function shared by both callers so the hardening logic cannot drift
    between them the way the two cache implementations already had before
    this module existed.
    """
    import wasmtime  # local: this module must stay importable without the [wasm] extra

    if secure_dir(cache_path.parent) is not None:
        safe_path = secure_cache_file(cache_path)
        if safe_path is not None and safe_path.exists():
            try:
                return wasmtime.Module.deserialize_file(engine, str(safe_path))
            except wasmtime.WasmtimeError as exc:
                # A wasmtime/engine-config/architecture mismatch — or a
                # tampered/corrupt entry — fails closed here (never silently
                # loads a bad artifact): fall through and recompile.
                logger.info("WASM AOT cache entry at %s unusable, recompiling: %s", cache_path, exc)

    module = wasmtime.Module(engine, wasm_bytes)
    if secure_dir(cache_path.parent) is not None:
        try:
            fd, tmp_name = tempfile.mkstemp(dir=cache_path.parent, prefix=".tmp-", suffix=".cwasm")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(module.serialize())
                os.replace(tmp_name, cache_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        except OSError as exc:
            # Fail-soft: an unwritable/untrusted cache dir must never break a
            # sync/query, it just means every process in this run pays the
            # JIT cost.
            logger.warning("could not write WASM AOT cache at %s: %s", cache_path, exc)
    return module
