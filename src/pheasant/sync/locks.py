from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_is_active(lock: Path) -> bool:
    try:
        payload = json.loads(lock.read_text(encoding="utf-8"))
    except Exception:
        return False
    # A PID only means something inside the namespace that recorded it. A lock
    # left behind by a killed container names a PID — usually 1 — that is very
    # much alive in the *next* container, so a purely PID-based check declared
    # the source locked forever and bricked every restart. A lock written by
    # anything other than this host/container is stale by definition; live
    # cross-process writers are excluded by ``EngineLease`` (heartbeat-based),
    # not by this file. Legacy locks carry no hostname and are treated the same.
    if payload.get("hostname") != socket.gethostname():
        logger.warning(
            "Clearing stale source lock %s from another host (hostname=%s, pid=%s)",
            lock,
            payload.get("hostname"),
            payload.get("pid"),
        )
        return False
    pid = payload.get("pid")
    return isinstance(pid, int) and _pid_is_running(pid)


def _unlink_lock(lock: Path, *, retry_seconds: float = 0.5) -> None:
    """Remove a lock, riding out a concurrent Windows reader briefly."""

    deadline = time.monotonic() + max(0.0, retry_seconds)
    while True:
        try:
            lock.unlink(missing_ok=True)
            return
        except PermissionError:
            # On Windows, Path.read_text() in a waiting contender can deny the
            # owner's unlink for the few microseconds its handle is open.
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


@contextmanager
def source_lock(
    lock_dir: str | Path,
    source_id: str,
    *,
    timeout_s: float = 0.0,
    poll_interval_s: float = 0.1,
):
    root = Path(lock_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / f"{source_id}.lock"
    payload = {
        "source_id": source_id,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        if lock.exists() and not _lock_is_active(lock):
            _unlink_lock(lock)
        try:
            with lock.open("x", encoding="utf-8") as fh:
                json.dump(payload, fh)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Source is already locked after waiting {max(0.0, timeout_s):.1f}s: "
                    f"{source_id}"
                ) from None
            time.sleep(max(0.01, float(poll_interval_s)))
    try:
        yield
    finally:
        _unlink_lock(lock)


class EngineLeaseError(RuntimeError):
    """A different live engine process already holds the writer lease."""


class EngineLease:
    """Single-writer lease for one state directory (Synapse step 21.2).

    The lease is a JSON file ``<state>/engine.lease`` recording the owning
    PID, hostname, and an ISO heartbeat timestamp that a daemon thread
    refreshes every ``heartbeat_interval_s`` seconds while held. A second
    *process* attempting to acquire against the same state directory fails
    fast with :class:`EngineLeaseError`; a stale lease (heartbeat older than
    ``stale_after_s``, or the owning PID provably dead on this host) is taken
    over with a logged warning. Engines within the same process share the
    lease (PID match), so in-process concurrency is serialized elsewhere
    (``SyncEngine._sync_mutex``), not here.
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        heartbeat_interval_s: float = 5.0,
        stale_after_s: float = 30.0,
        wait_timeout_s: float = 0.0,
        poll_interval_s: float = 0.25,
    ):
        self.path = Path(state_dir) / "engine.lease"
        self.heartbeat_interval_s = heartbeat_interval_s
        self.stale_after_s = stale_after_s
        self.wait_timeout_s = wait_timeout_s
        self.poll_interval_s = poll_interval_s
        self._held = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> None:
        """Take the lease, optionally waiting for the current writer.

        Interactive/embedded engines remain fail-fast by default. Server sync
        workers set ``wait_timeout_s`` because two legitimate background
        triggers (most commonly startup indexing and a source just added in
        the UI) should queue, not make the first user-visible sync fail.
        """
        if self._held:
            return
        deadline = time.monotonic() + self.wait_timeout_s
        logged_wait = False
        while True:
            existing = self._read()
            if existing is None:
                break
            pid = existing.get("pid")
            same_host = existing.get("hostname") in (None, socket.gethostname())
            if isinstance(pid, int) and pid == os.getpid() and same_host:
                break  # another engine in this process holds it; share ownership
            elif self._is_stale(existing, same_host):
                logger.warning(
                    "Taking over stale engine lease %s (pid=%s, heartbeat_at=%s)",
                    self.path,
                    pid,
                    existing.get("heartbeat_at"),
                )
                break
            elif time.monotonic() >= deadline:
                raise EngineLeaseError(
                    f"Another pheasant engine (pid={pid}, "
                    f"host={existing.get('hostname')}) holds the writer lease at "
                    f"{self.path} (heartbeat_at={existing.get('heartbeat_at')}). "
                    f"Stop that process before syncing this state directory, or "
                    f"wait ~{self.stale_after_s:.0f}s for the lease to go stale "
                    f"if the process crashed."
                )
            else:
                if not logged_wait:
                    logger.info(
                        "Waiting for pheasant writer lease %s (pid=%s, host=%s)",
                        self.path,
                        pid,
                        existing.get("hostname"),
                    )
                    logged_wait = True
                time.sleep(self.poll_interval_s)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write()
        self._held = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="pheasant-engine-lease",
            daemon=True,
        )
        self._thread.start()

    def release(self) -> None:
        """Stop heartbeating and remove the lease if this process owns it."""
        if not self._held:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.heartbeat_interval_s * 2))
            self._thread = None
        self._held = False
        existing = self._read()
        if existing is not None and existing.get("pid") == os.getpid():
            self.path.unlink(missing_ok=True)

    def _is_stale(self, payload: dict[str, Any], same_host: bool) -> bool:
        heartbeat_raw = payload.get("heartbeat_at")
        try:
            heartbeat = datetime.fromisoformat(str(heartbeat_raw))
        except (TypeError, ValueError):
            return True
        age_s = (datetime.now(UTC) - heartbeat).total_seconds()
        if age_s > self.stale_after_s:
            return True
        pid = payload.get("pid")
        return same_host and isinstance(pid, int) and not _pid_is_running(pid)

    def _read(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write(self) -> None:
        payload = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "heartbeat_at": datetime.now(UTC).isoformat(),
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval_s):
            try:
                self._write()
            except OSError as exc:  # pragma: no cover - disk-failure path
                logger.warning("Failed to refresh engine lease %s: %s", self.path, exc)
