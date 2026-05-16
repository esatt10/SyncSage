from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path


@contextmanager
def source_lock(lock_dir: str | Path, source_id: str):
    root = Path(lock_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / f"{source_id}.lock"
    if lock.exists():
        raise RuntimeError(f"Source is already locked: {source_id}")
    lock.write_text("locked", encoding="utf-8")
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)
