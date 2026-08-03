from __future__ import annotations

import json
import logging
import os
import socket
import threading
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


@contextmanager
def source_lock(lock_dir: str | Path, source_id: str):
    root = Path(lock_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / f"{source_id}.lock"
    if lock.exists():
        if _lock_is_active(lock):
            raise RuntimeError(f"Source is already locked: {source_id}")
        lock.unlink(missing_ok=True)

    payload = {
        "source_id": source_id,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    try:
        with lock.open("x", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except FileExistsError:
        raise RuntimeError(f"Source is already locked: {source_id}") from None
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


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
    ):
        self.path = Path(state_dir) / "engine.lease"
        self.heartbeat_interval_s = heartbeat_interval_s
        self.stale_after_s = stale_after_s
        self._held = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> None:
        """Take the lease, or raise :class:`EngineLeaseError`. Idempotent."""
        if self._held:
            return
        existing = self._read()
        if existing is not None:
            pid = existing.get("pid")
            same_host = existing.get("hostname") in (None, socket.gethostname())
            if isinstance(pid, int) and pid == os.getpid() and same_host:
                pass  # another engine in this process holds it; share ownership
            elif self._is_stale(existing, same_host):
                logger.warning(
                    "Taking over stale engine lease %s (pid=%s, heartbeat_at=%s)",
                    self.path,
                    pid,
                    existing.get("heartbeat_at"),
                )
            else:
                raise EngineLeaseError(
                    f"Another SyncSage engine (pid={pid}, "
                    f"host={existing.get('hostname')}) holds the writer lease at "
                    f"{self.path} (heartbeat_at={existing.get('heartbeat_at')}). "
                    f"Stop that process before syncing this state directory, or "
                    f"wait ~{self.stale_after_s:.0f}s for the lease to go stale "
                    f"if the process crashed."
                )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write()
        self._held = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="syncsage-engine-lease",
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
