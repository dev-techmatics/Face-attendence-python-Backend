"""Shared template setup, so the staff page and the HR pages format a time the same way."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "admin" / "templates"


def clock(value: datetime | None) -> str:
    """Just the time, in this machine's timezone. Stored values are UTC."""
    return value.astimezone().strftime("%H:%M") if value else ""


def stamp(value: datetime | None) -> str:
    return value.astimezone().strftime("%d %b %H:%M") if value else ""


def day(value) -> str:
    return value.strftime("%a %d %b") if value else ""


def hours(value: float | None) -> str:
    """Hours as ``7h 30m``: nobody reads 7.5 as seven and a half hours at a glance."""
    if value is None:
        return ""

    whole = int(value)
    minutes = int(round((value - whole) * 60))

    if minutes == 60:
        whole, minutes = whole + 1, 0

    if whole and minutes:
        return f"{whole}h {minutes}m"
    if whole:
        return f"{whole}h"
    return f"{minutes}m"


def form_datetime(value: datetime | None) -> str:
    """Local wall-clock time in the shape ``<input type="datetime-local">`` wants.

    Seconds and any offset have to go: the control silently ignores a value it cannot parse,
    so an edit form would open blank and an admin would retype a time that was already
    correct.
    """
    return value.astimezone().strftime("%Y-%m-%dT%H:%M") if value else ""


def register_filters(templates: Jinja2Templates) -> None:
    templates.env.filters["clock"] = clock
    templates.env.filters["stamp"] = stamp
    templates.env.filters["day"] = day
    templates.env.filters["hours"] = hours
    templates.env.filters["form_datetime"] = form_datetime
