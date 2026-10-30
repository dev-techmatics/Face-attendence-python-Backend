"""Prints who is registered and what attendance has been recorded.

For an operator who wants to look at the data without a database tool, an API key or a
browser. Read-only: it opens the database, prints, and closes. It cannot change anything.

    python deploy\\report.py                 who is registered
    python deploy\\report.py --today         today's attendance as well
    python deploy\\report.py --days 7        the last seven days
    python deploy\\report.py --person TS4012 one person's history
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Run from anywhere: make the application importable and read the deployment's own settings.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FA_ENV_FILE", ".env.production")
os.chdir(ROOT)

from sqlalchemy import func, select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.enums import AttendanceDecision, AttendanceDirection, EnrollmentStatus  # noqa: E402
from app.db.session import dispose_engine, get_session_factory, init_engine  # noqa: E402
from app.domain.models import AttendanceEvent, FaceTemplate, Person  # noqa: E402

#: Plain words for the decision codes, so nobody has to look them up.
OUTCOME = {
    AttendanceDecision.Accepted: "recorded",
    AttendanceDecision.Rejected: "face did not match",
    AttendanceDecision.Duplicate: "ignored, punched twice",
    AttendanceDecision.NotEnrolled: "not registered",
    AttendanceDecision.NoFaceDetected: "no face in photo",
    AttendanceDecision.LowQuality: "photo too dark or blurry",
    AttendanceDecision.SpoofSuspected: "suspected photo of a photo",
    AttendanceDecision.ProviderError: "face service error",
}

STATE = {
    EnrollmentStatus.NotEnrolled: "no photos yet",
    EnrollmentStatus.InProgress: "part registered",
    EnrollmentStatus.Enrolled: "registered",
    EnrollmentStatus.Stale: "needs registering again",
}


def local(when: datetime) -> str:
    """Timestamps are stored as UTC; show them in this machine's own time."""
    return when.astimezone().strftime("%d %b %Y  %H:%M")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--today", action="store_true", help="also show today's attendance")
    parser.add_argument("--days", type=int, help="show attendance for the last N days")
    parser.add_argument("--person", help="show one person, by their employee ID")
    parser.add_argument("--tenant", default=None, help="site, if you run more than one")
    arguments = parser.parse_args()

    settings = get_settings()
    tenant = arguments.tenant or settings.default_tenant_id

    init_engine(settings)
    factory = get_session_factory()

    try:
        async with factory() as session:
            await show_people(session, tenant, arguments.person)

            days = arguments.days if arguments.days else (1 if arguments.today else 0)
            if days or arguments.person:
                await show_attendance(session, tenant, days or 30, arguments.person)
    finally:
        await dispose_engine()

    return 0


async def show_people(session, tenant: str, only: str | None) -> None:
    filters = [Person.tenant_id == tenant]

    if only:
        filters.append(Person.external_id == only.strip().upper())

    rows = (
        (
            await session.execute(
                select(
                    Person.external_id,
                    Person.full_name,
                    Person.enrollment_status,
                    Person.enrolled_at_utc,
                    Person.created_at_utc,
                    select(func.count())
                    .select_from(FaceTemplate)
                    .where(
                        FaceTemplate.person_id == Person.id,
                        FaceTemplate.is_active == True,  # noqa: E712
                    )
                    .scalar_subquery()
                    .label("photos"),
                )
                .where(*filters)
                .order_by(Person.full_name)
            )
        )
        .all()
    )

    print()
    print("REGISTERED PEOPLE" + (f"  (site: {tenant})" if tenant != "default" else ""))
    print("=" * 78)

    if not rows:
        print("  Nobody is registered yet.")
        print()
        return

    print(f"  {'ID':<12} {'Name':<26} {'State':<24} {'Photos':>6}")
    print(f"  {'-' * 12} {'-' * 26} {'-' * 24} {'-' * 6}")

    for external_id, name, state, enrolled_at, _created, photos in rows:
        label = STATE.get(EnrollmentStatus(state), str(state))
        print(f"  {external_id:<12} {name[:26]:<26} {label:<24} {photos:>6}")

    ready = sum(1 for r in rows if EnrollmentStatus(r[2]) == EnrollmentStatus.Enrolled)
    print()
    print(f"  {len(rows)} people, {ready} fully registered and able to mark attendance.")
    print()


async def show_attendance(session, tenant: str, days: int, only: str | None) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=days)

    filters = [AttendanceEvent.tenant_id == tenant, AttendanceEvent.created_at_utc >= since]

    if only:
        filters.append(Person.external_id == only.strip().upper())

    rows = (
        (
            await session.execute(
                select(
                    AttendanceEvent.created_at_utc,
                    Person.external_id,
                    Person.full_name,
                    AttendanceEvent.direction,
                    AttendanceEvent.decision,
                    AttendanceEvent.device_id,
                )
                .outerjoin(Person, Person.id == AttendanceEvent.person_id)
                .where(*filters)
                .order_by(AttendanceEvent.created_at_utc.desc())
                .limit(500)
            )
        )
        .all()
    )

    print(f"ATTENDANCE, LAST {days} DAY{'S' if days != 1 else ''}")
    print("=" * 78)

    if not rows:
        print("  Nothing recorded in this period.")
        print()
        return

    print(f"  {'When':<20} {'ID':<10} {'Name':<20} {'In/Out':<7} {'Result':<24}")
    print(f"  {'-' * 20} {'-' * 10} {'-' * 20} {'-' * 7} {'-' * 24}")

    for when, external_id, name, direction, decision, _device in rows:
        result = OUTCOME.get(AttendanceDecision(decision), str(decision))
        way = AttendanceDirection(direction).name
        print(
            f"  {local(when):<20} {(external_id or '-'):<10} {(name or 'unknown')[:20]:<20} "
            f"{way:<7} {result:<24}"
        )

    accepted = sum(1 for r in rows if AttendanceDecision(r[4]) == AttendanceDecision.Accepted)
    print()
    print(f"  {len(rows)} attempts, {accepted} recorded. Failed attempts are kept on purpose:")
    print("  they are the evidence when somebody says the machine did not recognise them.")
    print()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
