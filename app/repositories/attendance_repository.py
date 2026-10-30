"""Reads and writes for :class:`~app.domain.models.AttendanceEvent`."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.enums import AttendanceDecision, AttendanceDirection
from app.domain.models import AttendanceEvent


class AttendanceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, event: AttendanceEvent) -> None:
        self._session.add(event)

    async def get_last_accepted(self, person_id: int) -> AttendanceEvent | None:
        stmt = (
            select(AttendanceEvent)
            .where(
                AttendanceEvent.person_id == person_id,
                AttendanceEvent.decision == AttendanceDecision.Accepted,
            )
            .order_by(AttendanceEvent.created_at_utc.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def get_last_accepted_in_window(
        self,
        person_id: int,
        direction: AttendanceDirection | None,
        not_before: datetime,
    ) -> AttendanceEvent | None:
        """Most recent accepted event for a person since ``not_before``.

        A ``None`` direction matches any direction, which is what the double-tap guard needs:
        the second tap has not been assigned a direction yet.
        """
        stmt = (
            select(AttendanceEvent)
            .where(
                AttendanceEvent.person_id == person_id,
                AttendanceEvent.decision == AttendanceDecision.Accepted,
                AttendanceEvent.created_at_utc >= not_before,
            )
            .order_by(AttendanceEvent.created_at_utc.desc())
            .limit(1)
        )

        if direction is not None:
            stmt = stmt.where(AttendanceEvent.direction == direction)

        return (await self._session.execute(stmt)).scalars().first()

    async def query(
        self,
        tenant_id: str,
        person_id: int | None,
        from_utc: datetime | None,
        to_utc: datetime | None,
        decision: AttendanceDecision | None,
        page: int,
        page_size: int,
    ) -> tuple[list[AttendanceEvent], int]:
        filters = [AttendanceEvent.tenant_id == tenant_id]

        if person_id is not None:
            filters.append(AttendanceEvent.person_id == person_id)

        if from_utc is not None:
            filters.append(AttendanceEvent.created_at_utc >= from_utc)

        if to_utc is not None:
            filters.append(AttendanceEvent.created_at_utc < to_utc)

        if decision is not None:
            filters.append(AttendanceEvent.decision == decision)

        total = (
            await self._session.execute(
                select(func.count()).select_from(AttendanceEvent).where(*filters)
            )
        ).scalar_one()

        items = (
            (
                await self._session.execute(
                    select(AttendanceEvent)
                    .where(*filters)
                    .order_by(AttendanceEvent.created_at_utc.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                    .options(joinedload(AttendanceEvent.person))
                )
            )
            .unique()
            .scalars()
            .all()
        )

        return list(items), int(total)

    async def expired_probe_frames(
        self, cutoff: datetime, batch_size: int
    ) -> list[tuple[int, str]]:
        """Oldest-first batch of ``(id, object key)`` for frames past their retention."""
        stmt = (
            select(AttendanceEvent.id, AttendanceEvent.probe_image_object_key)
            .where(
                AttendanceEvent.created_at_utc < cutoff,
                AttendanceEvent.probe_image_object_key.is_not(None),
            )
            .order_by(AttendanceEvent.created_at_utc)
            .limit(batch_size)
        )

        return [(row[0], row[1]) for row in (await self._session.execute(stmt)).all()]

    async def clear_probe_keys(self, event_ids: list[int]) -> None:
        """Null the keys in bulk after the files are gone.

        Doing it in this order means a crash leaves an orphaned file, not a row pointing at a
        file that no longer exists.
        """
        if not event_ids:
            return

        await self._session.execute(
            update(AttendanceEvent)
            .where(AttendanceEvent.id.in_(event_ids))
            .values(probe_image_object_key=None)
        )
