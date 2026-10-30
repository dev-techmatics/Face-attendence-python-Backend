"""Injectable clock so time-window rules (duplicate punches) are testable."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def utc_now(self) -> datetime:
        """Timezone-aware UTC now."""
        ...


class SystemClock:
    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """A clock that does not move. Used by the tests."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def utc_now(self) -> datetime:
        return self.now
