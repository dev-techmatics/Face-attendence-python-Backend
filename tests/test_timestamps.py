"""Timestamps must survive a database round-trip as timezone-aware UTC.

A naive value serialises without an offset, and a client parsing ``2026-10-30T05:39:23``
reads it as *local* time - silently shifting every attendance record by the viewer's offset.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.clock import SystemClock
from app.core.enums import (
    AttendanceDecision,
    AttendanceDirection,
    EnrollmentStatus,
    PersonStatus,
)
from app.domain.models import AttendanceEvent, Person
from app.repositories.attendance_repository import AttendanceRepository
from app.repositories.person_repository import PersonRepository

pytestmark = pytest.mark.asyncio


async def test_person_timestamps_come_back_utc_aware(session):
    now = SystemClock().utc_now()

    person = Person(
        tenant_id="default",
        external_id="TZ-1",
        full_name="Timezone Test",
        status=PersonStatus.Active,
        enrollment_status=EnrollmentStatus.NotEnrolled,
        created_at_utc=now,
        updated_at_utc=now,
    )
    session.add(person)
    await session.commit()
    session.expunge_all()

    loaded = await PersonRepository(session).get_by_id(person.id)

    assert loaded is not None
    assert loaded.created_at_utc.tzinfo is not None
    assert loaded.created_at_utc.utcoffset() == timedelta(0)
    # Round-tripped to the same instant, allowing for sub-second storage precision.
    assert abs((loaded.created_at_utc - now).total_seconds()) < 1


async def test_a_non_utc_input_is_normalised_not_truncated(session):
    # A caller in Kolkata hands us +05:30; the stored instant must be the same moment.
    kolkata = timezone(timedelta(hours=5, minutes=30))
    local = datetime(2026, 10, 30, 11, 0, 0, tzinfo=kolkata)

    person = Person(
        tenant_id="default",
        external_id="TZ-2",
        full_name="Offset Test",
        status=PersonStatus.Active,
        enrollment_status=EnrollmentStatus.NotEnrolled,
        created_at_utc=local,
        updated_at_utc=local,
    )
    session.add(person)
    await session.commit()
    session.expunge_all()

    loaded = await PersonRepository(session).get_by_id(person.id)

    assert loaded is not None
    assert loaded.created_at_utc == local
    # Same instant, expressed as UTC: 11:00 +05:30 is 05:30 UTC.
    assert loaded.created_at_utc.hour == 5
    assert loaded.created_at_utc.minute == 30
    assert loaded.created_at_utc.utcoffset() == timedelta(0)


async def test_attendance_event_timestamps_come_back_utc_aware(session):
    now = SystemClock().utc_now()

    event = AttendanceEvent(
        tenant_id="default",
        person_id=None,
        direction=AttendanceDirection.In,
        decision=AttendanceDecision.NoFaceDetected,
        match_score=0.0,
        threshold=0.52,
        provider_id="test",
        created_at_utc=now,
        captured_at_utc=now,
        processing_ms=5,
    )
    session.add(event)
    await session.commit()
    session.expunge_all()

    loaded = await AttendanceRepository(session).get_last_accepted_in_window(
        person_id=None, direction=None, not_before=now - timedelta(minutes=1)
    )

    # That query filters on Accepted, so it finds nothing - which is itself correct. Read the
    # row back directly instead.
    assert loaded is None

    from sqlalchemy import select

    stored = (
        (await session.execute(select(AttendanceEvent).where(AttendanceEvent.id == event.id)))
        .scalars()
        .one()
    )

    assert stored.created_at_utc.utcoffset() == timedelta(0)
    assert stored.captured_at_utc is not None
    assert stored.captured_at_utc.utcoffset() == timedelta(0)


async def test_the_duplicate_punch_window_compares_across_offsets(session):
    """The window arithmetic must not depend on whose clock wrote the row."""
    kolkata = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 10, 30, 11, 0, 0, tzinfo=kolkata)

    event = AttendanceEvent(
        tenant_id="default",
        person_id=None,
        direction=AttendanceDirection.In,
        decision=AttendanceDecision.Accepted,
        match_score=0.9,
        threshold=0.42,
        provider_id="test",
        created_at_utc=now,
        processing_ms=5,
    )
    session.add(event)
    await session.commit()

    repository = AttendanceRepository(session)

    # One minute before the punch, expressed in UTC: the punch is inside the window.
    inside = await repository.get_last_accepted_in_window(
        person_id=None, direction=None, not_before=now.astimezone(timezone.utc) - timedelta(minutes=1)
    )
    assert inside is not None

    # One minute after: outside.
    outside = await repository.get_last_accepted_in_window(
        person_id=None, direction=None, not_before=now.astimezone(timezone.utc) + timedelta(minutes=1)
    )
    assert outside is None
