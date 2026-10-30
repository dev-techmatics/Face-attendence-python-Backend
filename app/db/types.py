"""Portable column types."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp that is always stored and returned as timezone-aware UTC.

    The original service used ``DateTimeOffset``, which carries its offset through any
    round-trip. Most databases do not: SQLite has no date type at all and hands back a naive
    value, so a timestamp written as UTC would be read back with no zone and serialised
    without one. A client parsing ``2026-10-30T05:39:23`` then reads it as *local* time, which
    silently shifts every attendance record by the viewer's offset.

    So: convert to UTC on the way in, and re-attach UTC on the way out. A naive value arriving
    from application code is assumed to be UTC, since every clock in this codebase produces
    UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None

        if not isinstance(value, datetime):
            raise TypeError(f"Expected a datetime, got {type(value).__name__}.")

        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None

        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)
