"""The questions the dashboard asks of the database.

Kept apart from the page code so each one can be tested on its own, and so the screens stay
thin: a screen picks a date range and renders rows, it does not know any SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import Integer, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    AttendanceDecision,
    AttendanceDirection,
    EnrollmentStatus,
    PersonStatus,
)
from app.domain.models import AttendanceEvent, FaceTemplate, Person

#: Plain words for the decision codes. The screens show these, never the code itself.
OUTCOME = {
    AttendanceDecision.Accepted: "Recorded",
    AttendanceDecision.Rejected: "Face did not match",
    AttendanceDecision.Duplicate: "Ignored, punched twice",
    AttendanceDecision.NotEnrolled: "Not registered",
    AttendanceDecision.NoFaceDetected: "No face in photo",
    AttendanceDecision.LowQuality: "Photo too dark or blurry",
    AttendanceDecision.SpoofSuspected: "Suspected photo of a photo",
    AttendanceDecision.ProviderError: "Face service error",
}

#: What a failure usually means in the real world, for the Problems screen. The point of that
#: screen is to turn a pile of rejections into "go and fix the light above the back door".
LIKELY_CAUSE = {
    AttendanceDecision.LowQuality: "Lighting or camera placement - the face is too dark, blurred or far away.",
    AttendanceDecision.NoFaceDetected: "Camera angle - the face is out of frame, or the person is too far back.",
    AttendanceDecision.Rejected: "Either the wrong person, or their registration photos no longer look like them.",
    AttendanceDecision.NotEnrolled: "These people were never finished registering.",
    AttendanceDecision.Duplicate: "People tapping twice. Harmless, but it can mean the screen gives no feedback.",
    AttendanceDecision.ProviderError: "The recognition engine failed. Check the service log.",
}

ENROLLMENT_LABEL = {
    EnrollmentStatus.NotEnrolled: "No photos yet",
    EnrollmentStatus.InProgress: "Part registered",
    EnrollmentStatus.Enrolled: "Registered",
    EnrollmentStatus.Stale: "Needs registering again",
}


@dataclass(frozen=True)
class DayWindow:
    """A calendar day in local time, expressed as the UTC range to query on.

    Timestamps are stored as UTC. "Today" to somebody standing in the office is a local day,
    so converting at the edge is what stops a 9am punch in Kolkata landing on yesterday's
    board.
    """

    start_utc: datetime
    end_utc: datetime
    label: str

    @staticmethod
    def for_date(day: date) -> "DayWindow":
        local_zone = datetime.now().astimezone().tzinfo
        start = datetime.combine(day, time.min, tzinfo=local_zone)

        return DayWindow(
            start_utc=start.astimezone(timezone.utc),
            end_utc=(start + timedelta(days=1)).astimezone(timezone.utc),
            label=day.strftime("%A %d %B %Y"),
        )

    @staticmethod
    def for_range(first: date, last: date) -> "DayWindow":
        local_zone = datetime.now().astimezone().tzinfo
        start = datetime.combine(first, time.min, tzinfo=local_zone)
        end = datetime.combine(last, time.min, tzinfo=local_zone) + timedelta(days=1)

        return DayWindow(
            start_utc=start.astimezone(timezone.utc),
            end_utc=end.astimezone(timezone.utc),
            label=(
                first.strftime("%d %b %Y")
                if first == last
                else f"{first.strftime('%d %b')} to {last.strftime('%d %b %Y')}"
            ),
        )


@dataclass(frozen=True)
class PersonToday:
    external_id: str
    full_name: str
    department: str | None
    #: "In", "Out" or None when they have not been seen today.
    state: str | None
    first_in: datetime | None
    last_seen: datetime | None
    punches: int


async def today_board(session: AsyncSession, tenant: str, window: DayWindow) -> list[PersonToday]:
    """Everyone who can mark attendance, and where today left them.

    One row per person including those who have not arrived - "who is missing" is half the
    question a reception screen answers.
    """
    accepted = (
        select(
            AttendanceEvent.person_id.label("person_id"),
            func.min(AttendanceEvent.created_at_utc).label("first_seen"),
            func.max(AttendanceEvent.created_at_utc).label("last_seen"),
            func.count().label("punches"),
        )
        .where(
            AttendanceEvent.tenant_id == tenant,
            AttendanceEvent.decision == AttendanceDecision.Accepted,
            AttendanceEvent.created_at_utc >= window.start_utc,
            AttendanceEvent.created_at_utc < window.end_utc,
        )
        .group_by(AttendanceEvent.person_id)
        .subquery()
    )

    rows = (
        await session.execute(
            select(
                Person.id,
                Person.external_id,
                Person.full_name,
                Person.department,
                accepted.c.first_seen,
                accepted.c.last_seen,
                accepted.c.punches,
            )
            .outerjoin(accepted, accepted.c.person_id == Person.id)
            .where(
                Person.tenant_id == tenant,
                Person.status == PersonStatus.Active,
                Person.enrollment_status == EnrollmentStatus.Enrolled,
            )
            .order_by(Person.full_name)
        )
    ).all()

    # The direction of the last accepted punch decides whether somebody is in or out, and that
    # is one row per person rather than something the grouping above can give us.
    last_directions = dict(
        (
            await session.execute(
                select(AttendanceEvent.person_id, AttendanceEvent.direction).where(
                    AttendanceEvent.id.in_(
                        select(func.max(AttendanceEvent.id))
                        .where(
                            AttendanceEvent.tenant_id == tenant,
                            AttendanceEvent.decision == AttendanceDecision.Accepted,
                            AttendanceEvent.created_at_utc >= window.start_utc,
                            AttendanceEvent.created_at_utc < window.end_utc,
                        )
                        .group_by(AttendanceEvent.person_id)
                    )
                )
            )
        ).all()
    )

    board: list[PersonToday] = []

    for person_id, external_id, name, department, first, last, punches in rows:
        direction = last_directions.get(person_id)

        board.append(
            PersonToday(
                external_id=external_id,
                full_name=name,
                department=department,
                state=AttendanceDirection(direction).name if direction is not None else None,
                first_in=first,
                last_seen=last,
                punches=int(punches or 0),
            )
        )

    return board


async def register_rows(
    session: AsyncSession,
    tenant: str,
    window: DayWindow,
    search: str | None = None,
    only_failures: bool = False,
    limit: int = 1000,
) -> list[tuple]:
    """The attendance log: every attempt, newest first, successes and failures together."""
    filters = [
        AttendanceEvent.tenant_id == tenant,
        AttendanceEvent.created_at_utc >= window.start_utc,
        AttendanceEvent.created_at_utc < window.end_utc,
    ]

    if only_failures:
        filters.append(AttendanceEvent.decision != AttendanceDecision.Accepted)

    if search and search.strip():
        term = f"%{search.strip()}%"
        filters.append(Person.full_name.like(term) | Person.external_id.like(term))

    return (
        await session.execute(
            select(
                AttendanceEvent.id,
                AttendanceEvent.created_at_utc,
                Person.external_id,
                Person.full_name,
                AttendanceEvent.direction,
                AttendanceEvent.decision,
                AttendanceEvent.device_id,
                AttendanceEvent.match_score,
                AttendanceEvent.probe_image_object_key,
            )
            .outerjoin(Person, Person.id == AttendanceEvent.person_id)
            .where(*filters)
            .order_by(AttendanceEvent.created_at_utc.desc())
            .limit(limit)
        )
    ).all()


async def people_rows(session: AsyncSession, tenant: str) -> list[tuple]:
    """Who is registered, and - more useful - who is not finished."""
    active_photos = (
        select(func.count())
        .select_from(FaceTemplate)
        .where(
            FaceTemplate.person_id == Person.id,
            FaceTemplate.is_active == True,  # noqa: E712
        )
        .scalar_subquery()
    )

    return (
        await session.execute(
            select(
                Person.external_id,
                Person.full_name,
                Person.department,
                Person.status,
                Person.enrollment_status,
                Person.enrolled_at_utc,
                Person.created_at_utc,
                active_photos.label("photos"),
            )
            .where(Person.tenant_id == tenant)
            # Unfinished registrations first: that is the list somebody has to act on.
            .order_by(
                case(
                    (Person.enrollment_status == EnrollmentStatus.Enrolled, 1),
                    else_=0,
                ),
                Person.full_name,
            )
        )
    ).all()


async def problem_summary(
    session: AsyncSession, tenant: str, window: DayWindow
) -> list[tuple]:
    """Failures grouped by reason and device, so a pattern becomes visible.

    A list of individual rejections tells you nothing; "eleven 'too dark' at the back door"
    tells you to go and look at the light.
    """
    return (
        await session.execute(
            select(
                AttendanceEvent.decision,
                AttendanceEvent.device_id,
                func.count().label("total"),
                func.min(AttendanceEvent.created_at_utc).label("first_at"),
                func.max(AttendanceEvent.created_at_utc).label("last_at"),
            )
            .where(
                AttendanceEvent.tenant_id == tenant,
                AttendanceEvent.decision != AttendanceDecision.Accepted,
                AttendanceEvent.created_at_utc >= window.start_utc,
                AttendanceEvent.created_at_utc < window.end_utc,
            )
            .group_by(AttendanceEvent.decision, AttendanceEvent.device_id)
            .order_by(func.count().desc())
        )
    ).all()


async def busiest_hours(
    session: AsyncSession, tenant: str, window: DayWindow
) -> list[tuple[int, int, int]]:
    """``(hour, accepted, failed)`` per hour of the day, in local time.

    Done in Python rather than SQL: hour-of-day in the viewer's timezone is not something the
    three supported databases agree on how to express, and the row count here is small.
    """
    rows = (
        await session.execute(
            select(AttendanceEvent.created_at_utc, AttendanceEvent.decision).where(
                AttendanceEvent.tenant_id == tenant,
                AttendanceEvent.created_at_utc >= window.start_utc,
                AttendanceEvent.created_at_utc < window.end_utc,
            )
        )
    ).all()

    buckets: dict[int, list[int]] = {}

    for when, decision in rows:
        hour = when.astimezone().hour
        slot = buckets.setdefault(hour, [0, 0])
        if AttendanceDecision(decision) == AttendanceDecision.Accepted:
            slot[0] += 1
        else:
            slot[1] += 1

    return [(hour, counts[0], counts[1]) for hour, counts in sorted(buckets.items())]


async def monthly_totals(
    session: AsyncSession, tenant: str, window: DayWindow
) -> list[tuple]:
    """Per person: days present, first in, last out, and total hours.

    Hours are computed from the paired In/Out punches rather than from first-to-last, so a
    lunch break that was punched out and back in is not counted as time at work.
    """
    rows = (
        await session.execute(
            select(
                Person.external_id,
                Person.full_name,
                Person.department,
                AttendanceEvent.created_at_utc,
                AttendanceEvent.direction,
            )
            .join(Person, Person.id == AttendanceEvent.person_id)
            .where(
                AttendanceEvent.tenant_id == tenant,
                AttendanceEvent.decision == AttendanceDecision.Accepted,
                AttendanceEvent.created_at_utc >= window.start_utc,
                AttendanceEvent.created_at_utc < window.end_utc,
            )
            .order_by(Person.full_name, AttendanceEvent.created_at_utc)
        )
    ).all()

    people: dict[str, dict] = {}

    for external_id, name, department, when, direction in rows:
        local = when.astimezone()
        entry = people.setdefault(
            external_id,
            {
                "name": name,
                "department": department,
                "days": set(),
                "seconds": 0.0,
                "open_in": None,
                "first": local,
                "last": local,
                "unpaired": 0,
            },
        )

        entry["days"].add(local.date())
        entry["first"] = min(entry["first"], local)
        entry["last"] = max(entry["last"], local)

        if AttendanceDirection(direction) == AttendanceDirection.In:
            if entry["open_in"] is not None:
                # Two Ins in a row: somebody forgot to punch out. Counted, not guessed at.
                entry["unpaired"] += 1
            entry["open_in"] = local
        else:
            if entry["open_in"] is None:
                entry["unpaired"] += 1
            else:
                entry["seconds"] += (local - entry["open_in"]).total_seconds()
                entry["open_in"] = None

    # Anyone still "in" at the end of the period has an unclosed shift.
    for entry in people.values():
        if entry["open_in"] is not None:
            entry["unpaired"] += 1

    return [
        (
            external_id,
            entry["name"],
            entry["department"],
            len(entry["days"]),
            entry["first"],
            entry["last"],
            round(entry["seconds"] / 3600.0, 1),
            entry["unpaired"],
        )
        for external_id, entry in sorted(people.items(), key=lambda kv: kv[1]["name"])
    ]


async def headline_counts(session: AsyncSession, tenant: str, window: DayWindow) -> dict:
    """The numbers across the top of the Today screen."""
    enrolled = (
        await session.execute(
            select(func.count())
            .select_from(Person)
            .where(
                Person.tenant_id == tenant,
                Person.status == PersonStatus.Active,
                Person.enrollment_status == EnrollmentStatus.Enrolled,
            )
        )
    ).scalar_one()

    unfinished = (
        await session.execute(
            select(func.count())
            .select_from(Person)
            .where(
                Person.tenant_id == tenant,
                Person.enrollment_status.in_(
                    [EnrollmentStatus.InProgress, EnrollmentStatus.Stale]
                ),
            )
        )
    ).scalar_one()

    attempts, accepted = (
        await session.execute(
            select(
                func.count(),
                func.sum(
                    case(
                        (AttendanceEvent.decision == AttendanceDecision.Accepted, 1),
                        else_=0,
                    ).cast(Integer)
                ),
            ).where(
                AttendanceEvent.tenant_id == tenant,
                AttendanceEvent.created_at_utc >= window.start_utc,
                AttendanceEvent.created_at_utc < window.end_utc,
            )
        )
    ).one()

    return {
        "enrolled": int(enrolled),
        "unfinished": int(unfinished),
        "attempts": int(attempts or 0),
        "accepted": int(accepted or 0),
        "failed": int(attempts or 0) - int(accepted or 0),
    }


# ---- one person's own record -------------------------------------------------------------
# What a staff member sees when they sign in. Deliberately computed from the same accepted
# events the register shows, so their figure and HR's figure can never disagree.


@dataclass(frozen=True)
class WorkedDay:
    day: date
    first_in: datetime | None
    last_out: datetime | None
    hours: float
    #: A punch with no matching other half - usually somebody forgetting to punch out.
    unpaired: bool
    #: Still inside the building as far as the system knows.
    open_now: bool


@dataclass(frozen=True)
class MyAttendance:
    external_id: str
    full_name: str
    department: str | None
    state: str | None
    since: datetime | None
    days: list[WorkedDay]
    week_hours: float
    week_days: int
    month_hours: float
    month_days: int
    late_days: int
    #: Days in the period with no punch at all, excluding weekends and today.
    absent_days: int


def _pair_into_days(events: list[tuple[datetime, int]]) -> list[WorkedDay]:
    """Turn a stream of In/Out punches into one row per local day, with hours.

    Pairs an Out with the preceding In rather than measuring first-to-last, so a lunch break
    that was punched out and back in is not counted as time at work. An unmatched punch is
    reported rather than guessed at: inventing an end time would quietly inflate somebody's
    pay.
    """
    by_day: dict[date, dict] = {}

    for when, direction in events:
        local = when.astimezone()
        entry = by_day.setdefault(
            local.date(),
            {"first_in": None, "last_out": None, "seconds": 0.0, "open": None, "unpaired": False},
        )

        if AttendanceDirection(direction) == AttendanceDirection.In:
            if entry["first_in"] is None:
                entry["first_in"] = local
            if entry["open"] is not None:
                entry["unpaired"] = True
            entry["open"] = local
        else:
            entry["last_out"] = local
            if entry["open"] is None:
                entry["unpaired"] = True
            else:
                entry["seconds"] += (local - entry["open"]).total_seconds()
                entry["open"] = None

    return [
        WorkedDay(
            day=day,
            first_in=entry["first_in"],
            last_out=entry["last_out"],
            hours=round(entry["seconds"] / 3600.0, 2),
            unpaired=entry["unpaired"],
            open_now=entry["open"] is not None,
        )
        for day, entry in sorted(by_day.items(), reverse=True)
    ]


async def my_attendance(
    session: AsyncSession,
    tenant: str,
    person_id: int,
    *,
    history_days: int = 14,
    late_after: time = time(9, 30),
) -> MyAttendance | None:
    """One person's own attendance: today, this week, this month, and recent days."""
    person = (
        await session.execute(
            select(Person.external_id, Person.full_name, Person.department).where(
                Person.id == person_id, Person.tenant_id == tenant
            )
        )
    ).first()

    if person is None:
        return None

    local_zone = datetime.now().astimezone().tzinfo
    today = datetime.now(local_zone).date()

    month_start = today.replace(day=1)
    week_start = today - timedelta(days=today.weekday())
    # Far enough back to cover the history table, the week and the month in one query.
    earliest = min(month_start, week_start, today - timedelta(days=history_days))

    since_utc = datetime.combine(earliest, time.min, tzinfo=local_zone).astimezone(timezone.utc)

    events = [
        (row[0], row[1])
        for row in (
            await session.execute(
                select(AttendanceEvent.created_at_utc, AttendanceEvent.direction)
                .where(
                    AttendanceEvent.person_id == person_id,
                    AttendanceEvent.tenant_id == tenant,
                    AttendanceEvent.decision == AttendanceDecision.Accepted,
                    AttendanceEvent.created_at_utc >= since_utc,
                )
                .order_by(AttendanceEvent.created_at_utc)
            )
        ).all()
    ]

    worked = _pair_into_days(events)
    by_day = {d.day: d for d in worked}

    def total(frm: date) -> tuple[float, int]:
        days = [d for d in worked if d.day >= frm]
        return round(sum(d.hours for d in days), 1), len(days)

    week_hours, week_days = total(week_start)
    month_hours, month_days = total(month_start)

    late = sum(
        1
        for d in worked
        if d.day >= month_start and d.first_in is not None and d.first_in.time() > late_after
    )

    # Absent: a weekday in the month, before today, with nothing recorded. Today is excluded
    # because the day is not over, and weekends because nobody is expected.
    absent = 0
    cursor = month_start
    while cursor < today:
        if cursor.weekday() < 5 and cursor not in by_day:
            absent += 1
        cursor += timedelta(days=1)

    todays = by_day.get(today)
    state = None
    since = None

    if todays is not None:
        state = "In" if todays.open_now else "Out"
        since = todays.first_in if todays.open_now else todays.last_out

    return MyAttendance(
        external_id=person[0],
        full_name=person[1],
        department=person[2],
        state=state,
        since=since,
        days=[d for d in worked if d.day > today - timedelta(days=history_days + 1)],
        week_hours=week_hours,
        week_days=week_days,
        month_hours=month_hours,
        month_days=month_days,
        late_days=late,
        absent_days=absent,
    )
