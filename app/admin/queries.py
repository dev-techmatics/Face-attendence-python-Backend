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
    AttendanceDecision.TooSoon: "Too soon to check out",
    AttendanceDecision.AlreadyCheckedOut: "Already checked out",
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
    AttendanceDecision.TooSoon: (
        "People trying to check out before the minimum shift length. A steady trickle means "
        "the minimum is set longer than the shifts people actually work."
    ),
    AttendanceDecision.AlreadyCheckedOut: (
        "Faces passing the camera again after finishing for the day. A few is normal. A lot "
        "means the kiosk is somewhere people walk back through, and moving it will do more "
        "than any setting."
    ),
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
    #: When their most recent accepted punch happened. NOT a presence feed - this service
    #: knows nothing about anybody between punches, and a column called "last seen" implied
    #: it did.
    last_punch: datetime | None
    punches: int


async def today_board(session: AsyncSession, tenant: str, window: DayWindow) -> list[PersonToday]:
    """Everyone who can mark attendance, and where today left them.

    One row per person including those who have not arrived - "who is missing" is half the
    question a reception screen answers.
    """
    accepted = (
        select(
            AttendanceEvent.person_id.label("person_id"),
            func.min(AttendanceEvent.created_at_utc).label("first_punch"),
            func.max(AttendanceEvent.created_at_utc).label("last_punch"),
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
                accepted.c.first_punch,
                accepted.c.last_punch,
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
                last_punch=last,
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
                AttendanceEvent.edited_at_utc,
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
    session: AsyncSession, tenant: str, window: DayWindow, expiry_hours: float | None = None
) -> list[tuple]:
    """Per person: days present, first check-in, last check-out, total hours, unpaired.

    Built on :func:`_pair_into_sessions`, the same pairing every other screen uses. It used
    to carry its own copy of the arithmetic, and the two drifted: on a 22:00-06:00 shift this
    page said eight hours while the day views said nought, and its "last out" column was the
    latest punch of ANY direction, so somebody who never checked out had their check-in
    printed under it.

    ``first`` and ``last`` are now a real first check-in and a real last check-out. ``last``
    is ``None`` when the final session is still open or was never closed, because there is no
    leaving time to report and inventing one is what the old column effectively did.
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

    by_person: dict[str, dict] = {}

    for external_id, name, department, when, direction in rows:
        entry = by_person.setdefault(
            external_id,
            {"name": name, "department": department, "events": []},
        )
        entry["events"].append((when, direction))

    totals = []

    for external_id, entry in by_person.items():
        sessions = _pair_into_sessions(entry["events"], expiry_hours)

        starts = [s.started for s in sessions if s.started is not None]
        ends = [s.ended for s in sessions if s.ended is not None]

        # Unpaired: a check-in that never got its check-out, plus a check-out that never had
        # a check-in. Both are somebody's missing punch and both need a human.
        unpaired = sum(1 for s in sessions if s.missed_out or s.open_now or s.orphan_out)

        totals.append(
            (
                external_id,
                entry["name"],
                entry["department"],
                len({s.day for s in sessions if not s.orphan_out}),
                min(starts) if starts else None,
                max(ends) if ends else None,
                round(sum(s.hours for s in sessions), 1),
                unpaired,
            )
        )

    totals.sort(key=lambda row: row[1])
    return totals


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
class WorkSession:
    """One check-in and the check-out that closed it. The unit everything is counted in.

    Keyed on the clock, never on the calendar. The previous model bucketed punches by local
    date first and paired them inside each bucket, which meant a check-in could not find a
    check-out on the other side of midnight: a 22:00-06:00 shift came out as two broken rows
    worth **nothing**, while the monthly page - which paired across the whole month - said
    eight hours. Both screens, one night, two different answers.

    So a session simply runs from an In to the Out that follows it, however long and across
    however many midnights. ``day`` is the date of the check-in, which is the day the person
    came to work and the date they would name if asked. It is for reading and grouping, and
    never takes part in the arithmetic: hours are ``ended - started`` and nothing else.
    """

    #: Local time. ``None`` only for an orphan check-out - see ``orphan_out``.
    started: datetime | None
    #: Local time. ``None`` while they are still inside, or when the check-out never came.
    ended: datetime | None
    hours: float
    #: Checked in, no check-out yet, and recent enough that one could still arrive.
    open_now: bool
    #: Checked in and never checked out, past the point where a check-out is coming. The same
    #: cutoff the server uses to abandon a stale shift, so this turns true at exactly the
    #: moment the next punch would start a fresh check-in rather than close this one.
    missed_out: bool
    #: A check-out with no check-in before it. Cannot be paired and is worth no hours, but it
    #: is shown rather than dropped: a punch that silently disappears is how somebody loses
    #: faith in the whole register.
    orphan_out: bool = False

    @property
    def day(self) -> date:
        """The date the person marked attendance - the check-in's date."""
        anchor = self.started or self.ended
        return anchor.date()

    @property
    def ends_next_day(self) -> bool:
        """True when the check-out landed on a later date than the check-in."""
        return (
            self.started is not None
            and self.ended is not None
            and self.ended.date() != self.started.date()
        )

    @property
    def nights(self) -> int:
        """How many midnights the session crossed. 1 for a normal night shift."""
        if not self.ends_next_day:
            return 0
        return (self.ended.date() - self.started.date()).days


def _pair_into_sessions(
    events: list[tuple[datetime, int]], expiry_hours: float | None = None
) -> list[WorkSession]:
    """Turn a time-ordered stream of In/Out punches into sessions, newest first.

    ``events`` must be **ascending**: an Out is paired with the In before it, so handed the
    rows newest-first this pairs nothing.

    A second In while one is already open closes nothing - the first becomes a session with
    no end, judged by ``expiry_hours`` like any other. ``expiry_hours`` of ``None`` means no
    session is ever called missed, only open.
    """
    now = datetime.now().astimezone()
    sessions: list[WorkSession] = []
    open_at: datetime | None = None

    def close(started: datetime, ended: datetime | None) -> None:
        if ended is not None:
            sessions.append(
                WorkSession(
                    started=started,
                    ended=ended,
                    hours=round((ended - started).total_seconds() / 3600.0, 2),
                    open_now=False,
                    missed_out=False,
                )
            )
            return

        # Still open. Whether that means "at work" or "forgot to leave" is the only thing
        # the clock is asked here, and it is the same question the server asks before it
        # abandons a shift.
        stale = expiry_hours is not None and (now - started) >= timedelta(hours=expiry_hours)
        sessions.append(
            WorkSession(
                started=started,
                ended=None,
                hours=0.0,
                open_now=not stale,
                missed_out=stale,
            )
        )

    for when, direction in events:
        local = when.astimezone()

        if AttendanceDirection(direction) == AttendanceDirection.In:
            if open_at is not None:
                # Two check-ins with no check-out between them: the first never ended.
                close(open_at, None)
            open_at = local
        elif open_at is None:
            sessions.append(
                WorkSession(
                    started=None,
                    ended=local,
                    hours=0.0,
                    open_now=False,
                    missed_out=False,
                    orphan_out=True,
                )
            )
        else:
            close(open_at, local)
            open_at = None

    if open_at is not None:
        close(open_at, None)

    # Newest first, which is how every screen reads them.
    sessions.sort(key=lambda s: s.started or s.ended, reverse=True)
    return sessions


@dataclass(frozen=True)
class MyAttendance:
    external_id: str
    full_name: str
    department: str | None
    state: str | None
    since: datetime | None
    days: list[WorkSession]
    week_hours: float
    week_days: int
    month_hours: float
    month_days: int
    late_days: int
    #: Days in the period with no punch at all, excluding weekends and today.
    absent_days: int


async def my_attendance(
    session: AsyncSession,
    tenant: str,
    person_id: int,
    *,
    history_days: int = 14,
    late_after: time = time(9, 30),
    expiry_hours: float | None = None,
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

    worked = _pair_into_sessions(events, expiry_hours)

    # A date can hold several sessions now - a lunch break makes two - so the day-shaped
    # questions below (how many days, was I late, was I absent) group them rather than
    # assuming one each.
    dates_worked = {s.day for s in worked if not s.orphan_out}

    def total(frm: date) -> tuple[float, int]:
        chosen = [s for s in worked if s.day >= frm]
        return (
            round(sum(s.hours for s in chosen), 1),
            len({s.day for s in chosen if not s.orphan_out}),
        )

    week_hours, week_days = total(week_start)
    month_hours, month_days = total(month_start)

    # Late is judged on the FIRST check-in of a date, not on every session: coming back from
    # lunch at 14:00 is not turning up late.
    first_in_on: dict[date, datetime] = {}

    for s in worked:
        if s.started is None:
            continue
        if s.day not in first_in_on or s.started < first_in_on[s.day]:
            first_in_on[s.day] = s.started

    late = sum(
        1
        for day, started in first_in_on.items()
        if day >= month_start and started.time() > late_after
    )

    # Absent: a weekday in the month, before today, with nothing recorded. Today is excluded
    # because the day is not over, and weekends because nobody is expected.
    absent = 0
    cursor = month_start
    while cursor < today:
        if cursor.weekday() < 5 and cursor not in dates_worked:
            absent += 1
        cursor += timedelta(days=1)

    # The state is the latest session that started today, which is the one they are in or
    # the one they last finished.
    todays = next((s for s in worked if s.day == today and not s.orphan_out), None)
    state = None
    since = None

    if todays is not None:
        state = "In" if todays.open_now else "Out"
        since = todays.started if todays.open_now else todays.ended

    return MyAttendance(
        external_id=person[0],
        full_name=person[1],
        department=person[2],
        state=state,
        since=since,
        days=[s for s in worked if s.day > today - timedelta(days=history_days + 1)],
        week_hours=week_hours,
        week_days=week_days,
        month_hours=month_hours,
        month_days=month_days,
        late_days=late,
        absent_days=absent,
    )


@dataclass(frozen=True)
class EventPhoto:
    """One attendance attempt, enough of it to caption the photograph it stored."""

    event_id: int
    external_id: str | None
    full_name: str | None
    when: datetime
    direction: str
    result: str
    accepted: bool
    device: str | None
    has_photo: bool


async def one_event(
    session: AsyncSession, tenant: str, event_id: int
) -> EventPhoto | None:
    """The single attempt behind a photo page. Scoped to the tenant, like every other query."""
    row = (
        await session.execute(
            select(
                AttendanceEvent.id,
                Person.external_id,
                Person.full_name,
                AttendanceEvent.created_at_utc,
                AttendanceEvent.direction,
                AttendanceEvent.decision,
                AttendanceEvent.device_id,
                AttendanceEvent.probe_image_object_key,
            )
            .outerjoin(Person, Person.id == AttendanceEvent.person_id)
            .where(AttendanceEvent.id == event_id, AttendanceEvent.tenant_id == tenant)
        )
    ).first()

    if row is None:
        return None

    decision = AttendanceDecision(row[5])

    return EventPhoto(
        event_id=row[0],
        external_id=row[1],
        full_name=row[2],
        when=row[3],
        direction=AttendanceDirection(row[4]).name if row[4] is not None else "",
        result=OUTCOME.get(decision, decision.name),
        accepted=decision == AttendanceDecision.Accepted,
        device=row[6],
        has_photo=bool(row[7]),
    )


@dataclass(frozen=True)
class Footprint:
    """Everything a permanent delete would destroy, counted before it happens.

    Shown on the confirmation screen. Somebody about to erase a person's record is entitled
    to see exactly what goes with them, in numbers rather than in a warning sentence.
    """

    person_id: int
    external_id: str
    full_name: str
    department: str | None
    status: PersonStatus
    enrollment_status: EnrollmentStatus
    photos: int
    punches: int
    first_punch: datetime | None
    last_punch: datetime | None


async def person_footprint(
    session: AsyncSession, tenant: str, external_id: str
) -> Footprint | None:
    row = (
        await session.execute(
            select(
                Person.id,
                Person.external_id,
                Person.full_name,
                Person.department,
                Person.status,
                Person.enrollment_status,
            ).where(Person.tenant_id == tenant, Person.external_id == external_id)
        )
    ).first()

    if row is None:
        return None

    person_id = row[0]

    photos = (
        await session.execute(
            select(func.count())
            .select_from(FaceTemplate)
            .where(FaceTemplate.person_id == person_id)
        )
    ).scalar_one()

    # Every event, not just the accepted ones: a rejection is part of the record too, and it
    # is just as gone afterwards.
    punches, first_punch, last_punch = (
        await session.execute(
            select(
                func.count(),
                func.min(AttendanceEvent.created_at_utc),
                func.max(AttendanceEvent.created_at_utc),
            ).where(
                AttendanceEvent.tenant_id == tenant,
                AttendanceEvent.person_id == person_id,
            )
        )
    ).one()

    return Footprint(
        person_id=person_id,
        external_id=row[1],
        full_name=row[2],
        department=row[3],
        status=PersonStatus(row[4]),
        enrollment_status=EnrollmentStatus(row[5]),
        photos=int(photos),
        punches=int(punches),
        first_punch=first_punch,
        last_punch=last_punch,
    )


@dataclass(frozen=True)
class PersonDay:
    """One person, one day, on one line: in, out and the hours between them."""

    external_id: str
    full_name: str
    worked: WorkSession


async def worked_days(
    session: AsyncSession,
    tenant: str,
    window: DayWindow,
    search: str | None = None,
    expiry_hours: float | None = None,
) -> list[PersonDay]:
    """The register collapsed to one row per person per day.

    The same punches the log shows, paired up. Only accepted ones: a failed attempt has no
    other half to pair with and would silently become an arrival that never happened.

    Pairing is :func:`_pair_into_sessions`, the same code behind the monthly figures and the
    staff page, so an hour counted here is the hour counted everywhere else.
    """
    filters = [
        AttendanceEvent.tenant_id == tenant,
        AttendanceEvent.decision == AttendanceDecision.Accepted,
        AttendanceEvent.created_at_utc >= window.start_utc,
        AttendanceEvent.created_at_utc < window.end_utc,
    ]

    if search and search.strip():
        term = f"%{search.strip()}%"
        filters.append(Person.full_name.like(term) | Person.external_id.like(term))

    rows = (
        await session.execute(
            select(
                Person.external_id,
                Person.full_name,
                AttendanceEvent.created_at_utc,
                AttendanceEvent.direction,
            )
            .join(Person, Person.id == AttendanceEvent.person_id)
            .where(*filters)
            .order_by(AttendanceEvent.created_at_utc)
        )
    ).all()

    by_person: dict[tuple[str, str], list[tuple[datetime, int]]] = {}

    for external_id, full_name, when, direction in rows:
        by_person.setdefault((external_id, full_name), []).append((when, direction))

    days = [
        PersonDay(external_id=external_id, full_name=full_name, worked=worked)
        for (external_id, full_name), events in by_person.items()
        for worked in _pair_into_sessions(events, expiry_hours)
    ]

    # Newest first, and within one date by name - so reverse the date only. Sorting the
    # tuple would run the names backwards too.
    days.sort(key=lambda d: d.full_name)
    days.sort(key=lambda d: d.worked.day, reverse=True)
    return days


@dataclass(frozen=True)
class PersonHistory:
    """One person's attendance, for the page reached from People.

    Days only. The attempt log that used to sit under this was removed on the user's
    instruction: the page answers "when did they come and go", and the evidence behind those
    figures, failures included, is one click away on the Attendance page filtered to their
    id. Two views of the same punches on one screen made the answer harder to find, not
    easier.
    """

    person: Footprint
    sessions: list[WorkSession]
    total_hours: float
    #: Distinct dates somebody checked in on, so a night shift counts once rather than
    #: twice for the two dates it touches.
    days_present: int
    #: Days they checked in and never checked out, past the point where one could still
    #: arrive. These count no hours, so they are the days somebody has to rule on.
    missed_outs: int


async def person_history(
    session: AsyncSession,
    tenant: str,
    external_id: str,
    expiry_hours: float | None = None,
) -> PersonHistory | None:
    """One person's whole attendance record, paired into days.

    Every accepted punch on record, with no cap: this is where somebody checks a figure they
    are being paid on, and a total that quietly left out the oldest punches would be worse
    than no total at all.
    """
    person = await person_footprint(session, tenant, external_id)

    if person is None:
        return None

    # Ascending: _pair_into_sessions walks the stream in order, pairing each Out with the In
    # before it. Handed the rows newest-first it would pair nothing.
    accepted = [
        (row[0], row[1])
        for row in (
            await session.execute(
                select(AttendanceEvent.created_at_utc, AttendanceEvent.direction)
                .where(
                    AttendanceEvent.tenant_id == tenant,
                    AttendanceEvent.person_id == person.person_id,
                    AttendanceEvent.decision == AttendanceDecision.Accepted,
                )
                .order_by(AttendanceEvent.created_at_utc)
            )
        ).all()
    ]

    sessions = _pair_into_sessions(accepted, expiry_hours)

    return PersonHistory(
        person=person,
        sessions=sessions,
        total_hours=round(sum(s.hours for s in sessions), 2),
        days_present=len({s.day for s in sessions if not s.orphan_out}),
        missed_outs=sum(1 for s in sessions if s.missed_out),
    )


@dataclass(frozen=True)
class DailyRow:
    """One line of the payroll sheet: a single check-in and the check-out that closed it.

    A **session**, not a day. One row per date looked tidier and lied on any day with more
    than one session: Mighty Mike's 10 November showed "in 15:24, out 15:46" and a flag
    saying a check-out was missing, because those were the first check-in and the last
    check-out of the date while a third check-in at 16:45 was never closed. Every half of
    that row was true and the row as a whole was nonsense.

    A row is now one pair, so ``hours`` is always ``out - in`` and nothing in it can
    disagree with anything else in it. A date with no punches at all still gets exactly one
    row - that row is what a loss-of-pay check is hunting.
    """

    external_id: str
    full_name: str
    department: str | None
    on: date
    checked_in: datetime | None
    checked_out: datetime | None
    hours: float
    #: "out" when the check-out never came, "in" for a check-out with no check-in before it,
    #: empty when the pair is complete. A fact about the record, not a verdict on the person.
    missing: str
    #: True for a date nobody punched at all.
    absent: bool


async def daily_breakdown(
    session: AsyncSession, tenant: str, window: DayWindow, expiry_hours: float | None = None
) -> list[DailyRow]:
    """Every employee, every date in the window, in employee-id order.

    One row per check-in/check-out pair, so a day somebody went out and came back is two
    rows rather than one row that has to summarise them and cannot do it honestly. Built on
    :func:`_pair_into_sessions` like every other screen, so a night shift is one row counted
    in full against the date it started.

    Dates before somebody was registered are left out - reporting absence for days they were
    not an employee invents a liability - but attendance always overrides that guard, since
    re-registration moves the enrolment timestamp forward and a punch earlier than it is
    real.
    """
    people = (
        await session.execute(
            select(
                Person.id,
                Person.external_id,
                Person.full_name,
                Person.department,
                Person.enrolled_at_utc,
                Person.created_at_utc,
            ).where(Person.tenant_id == tenant)
        )
    ).all()

    punches = (
        await session.execute(
            select(
                AttendanceEvent.person_id,
                AttendanceEvent.created_at_utc,
                AttendanceEvent.direction,
            )
            .where(
                AttendanceEvent.tenant_id == tenant,
                AttendanceEvent.decision == AttendanceDecision.Accepted,
                AttendanceEvent.created_at_utc >= window.start_utc,
                AttendanceEvent.created_at_utc < window.end_utc,
            )
            .order_by(AttendanceEvent.created_at_utc)
        )
    ).all()

    by_person: dict[int, list[tuple[datetime, int]]] = {}

    for person_id, when, direction in punches:
        by_person.setdefault(person_id, []).append((when, direction))

    local_zone = datetime.now().astimezone().tzinfo
    first_date = window.start_utc.astimezone(local_zone).date()
    last_date = (window.end_utc.astimezone(local_zone) - timedelta(seconds=1)).date()

    rows: list[DailyRow] = []

    for person_id, external_id, full_name, department, enrolled_at, created_at in people:
        sessions = _pair_into_sessions(by_person.get(person_id, []), expiry_hours)

        on_date: dict[date, list[WorkSession]] = {}

        for s in sessions:
            on_date.setdefault(s.day, []).append(s)

        joined = enrolled_at or created_at
        start = first_date

        if joined is not None:
            boundary = joined.astimezone(local_zone).date()

            if on_date:
                boundary = min(boundary, min(on_date))

            start = max(first_date, boundary)

        cursor = start

        while cursor <= last_date:
            todays = sorted(on_date.get(cursor, []), key=lambda s: s.started or s.ended)

            if not todays:
                rows.append(
                    DailyRow(
                        external_id=external_id,
                        full_name=full_name,
                        department=department,
                        on=cursor,
                        checked_in=None,
                        checked_out=None,
                        hours=0.0,
                        missing="",
                        absent=True,
                    )
                )
                cursor += timedelta(days=1)
                continue

            for spell in todays:
                if spell.orphan_out:
                    missing = "in"
                elif spell.ended is None:
                    missing = "out"
                else:
                    missing = ""

                rows.append(
                    DailyRow(
                        external_id=external_id,
                        full_name=full_name,
                        department=department,
                        on=cursor,
                        checked_in=spell.started,
                        checked_out=spell.ended,
                        hours=spell.hours,
                        missing=missing,
                        absent=False,
                    )
                )

            cursor += timedelta(days=1)

    # Employee id ascending, as HR asked. Numeric ids sort as numbers so 2 comes before 10,
    # and ids that are not numbers sort after them, alphabetically - a plain string sort
    # would put 12345 before 1267.
    def by_id(row: DailyRow) -> tuple:
        raw = row.external_id
        return (0, int(raw), "") if raw.isdigit() else (1, 0, raw.upper())

    def when(row: DailyRow) -> float:
        """Seconds, not a datetime.

        Sorting on the timestamps themselves meant comparing an aware value against a naive
        sentinel the moment two people shared a date, which raises. One person in a test
        never triggers it; six people in the real database do, on the first row.
        """
        anchor = row.checked_in or row.checked_out
        return anchor.timestamp() if anchor is not None else 0.0

    rows.sort(key=lambda r: (r.on, when(r)))
    rows.sort(key=by_id)
    return rows
