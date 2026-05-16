from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WatcherService:
    enabled: bool = True

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None
