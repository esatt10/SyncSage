from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


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
