from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SchedulerService:
    interval_seconds: int

    def start(self) -> None:
        return None
