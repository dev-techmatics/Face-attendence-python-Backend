"""The payroll workbook HR reconciles a quarter with.

A real ``.xlsx`` rather than a CSV, because what HR asked for cannot survive a CSV: a sheet
per month, each in its own colour, and a blank column to write reasons into by hand.

The shape is theirs, and the reasoning behind each column is worth keeping:

* **One row per employee per date**, employee id first, date second. The question being
  asked is "what happened on the 14th", not "how many days was everybody here".
* **A date nobody turned up is still a row**, with its times blank. That row is the thing a
  loss-of-pay check is hunting; a file that omitted it would make somebody read the gaps
  between dates to find it.
* **No status column.** HR did not want the service's opinion in a sheet they annotate.
  What survives is ``Missed punch`` - "in" or "out", naming which half is missing - because
  that is a fact about the record rather than a judgement about the person.
* **Reason is always empty.** It is HR's column, filled in by hand once they have worked out
  why. Writing anything into it would be guessing at the answer they are there to supply.
"""

from __future__ import annotations

import io
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.admin.queries import DailyRow

HEADERS = (
    ("Employee ID", 13),
    ("Name", 22),
    ("Department", 16),
    ("Date", 12),
    ("Day", 7),
    ("In", 9),
    ("Out", 16),
    ("Hours", 8),
    ("Missed punch", 14),
    ("Reason", 34),
)

#: One per month, cycled. Each is a (tab, header fill) pair in the same hue: the tab colour
#: is how somebody finds the month, the header fill is how they know which sheet they are
#: looking at once they have scrolled and the tab is out of sight. Light enough to read black
#: text on, and distinguishable in greyscale when the sheet is printed.
MONTH_COLOURS = (
    ("1F6FEB", "DCE9FB"),  # blue
    ("1A7F55", "DCF0E4"),  # green
    ("A15C00", "FBEBD7"),  # amber
    ("7A3DB8", "EBE2F8"),  # violet
    ("B02A37", "FADDE0"),  # red
    ("0E7490", "D8EEF3"),  # teal
)

THIN = Side(style="thin", color="D4D4D4")


def _month_key(on: date) -> tuple[int, int]:
    return (on.year, on.month)


def _clock(when: datetime | None) -> str:
    return "" if when is None else when.astimezone().strftime("%H:%M")


def _stamp_if_later(start: datetime | None, finish: datetime | None) -> str:
    """The check-out as a clock time, or a whole timestamp when it is the next morning.

    A night shift ends on the following date and a bare ``06:00`` in a spreadsheet cannot
    say so. Only those rows carry the date, so the column stays readable on an ordinary
    month of day shifts.
    """
    if finish is None:
        return ""

    local_finish = finish.astimezone()

    if start is not None and local_finish.date() != start.astimezone().date():
        return local_finish.strftime("%d %b %H:%M")

    return local_finish.strftime("%H:%M")


def _missing(row: DailyRow) -> str:
    """Which half of the pair never arrived - a fact, not a verdict."""
    if row.status == "No check-out" or row.status == "Still checked in":
        return "out"
    if row.status == "Check-out with no check-in":
        return "in"
    return ""


def months_between(first: date, last: date) -> list[tuple[int, int]]:
    """Every (year, month) from one to the other inclusive."""
    spans = []
    year, month = first.year, first.month

    while (year, month) <= (last.year, last.month):
        spans.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    return spans


def build_workbook(rows: list[DailyRow], first: date, last: date) -> bytes:
    """One sheet per calendar month in the range, in order, each in its own colour.

    **Every** month asked for gets a sheet, including ones with nothing in them. A workbook
    that quietly omits September when September was requested looks like a failed download,
    and the one thing worse than an empty sheet is a missing one somebody does not notice.
    """
    book = Workbook()
    book.remove(book.active)

    months: dict[tuple[int, int], list[DailyRow]] = {
        key: [] for key in months_between(first, last)
    }

    for row in rows:
        months.setdefault(_month_key(row.on), []).append(row)

    for index, key in enumerate(sorted(months)):
        year, month = key
        title = date(year, month, 1).strftime("%b %Y")
        tab, header_fill = MONTH_COLOURS[index % len(MONTH_COLOURS)]

        sheet = book.create_sheet(title)
        sheet.sheet_properties.tabColor = tab

        fill = PatternFill("solid", fgColor=header_fill)
        bold = Font(bold=True)

        for column, (label, width) in enumerate(HEADERS, start=1):
            cell = sheet.cell(row=1, column=column, value=label)
            cell.font = bold
            cell.fill = fill
            cell.border = Border(bottom=THIN)
            cell.alignment = Alignment(horizontal="left")
            sheet.column_dimensions[get_column_letter(column)].width = width

        for offset, row in enumerate(months[key], start=2):
            values = (
                row.external_id,
                row.full_name,
                row.department or "",
                row.on.strftime("%Y-%m-%d"),
                row.on.strftime("%a"),
                _clock(row.checked_in),
                _stamp_if_later(row.checked_in, row.checked_out),
                round(row.hours, 2) if row.hours else "",
                _missing(row),
                # Reason: HR's own column, deliberately left empty.
                "",
            )

            for column, value in enumerate(values, start=1):
                cell = sheet.cell(row=offset, column=column, value=value)
                cell.border = Border(bottom=THIN)

                if column == 8 and value != "":
                    cell.number_format = "0.00"

        if not months[key]:
            sheet.cell(row=2, column=1, value="No attendance recorded in this month.")

        # The header stays put while they scroll a quarter, and the whole block filters.
        sheet.freeze_panes = "A2"

        if months[key]:
            sheet.auto_filter.ref = (
                f"A1:{get_column_letter(len(HEADERS))}{len(months[key]) + 1}"
            )

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
