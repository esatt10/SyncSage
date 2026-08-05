from __future__ import annotations

import time


class Debouncer:
    def __init__(self, debounce_ms: int):
        self.debounce_ms = debounce_ms
        self._last = 0.0

    def ready(self) -> bool:
        now = time.monotonic()
        if (now - self._last) * 1000 >= self.debounce_ms:
            self._last = now
            return True
        return False
