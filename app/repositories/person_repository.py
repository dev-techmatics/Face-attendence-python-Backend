"""Reads and writes for :class:`~app.domain.models.Person`.

``is_active == True`` rather than ``.is_(True)`` throughout: SQLAlchemy renders ``.is_(True)``
as ``IS 1``, which SQL Server rejects outright - T-SQL has no ``IS <value>``, only ``IS NULL``.
SQLite accepts it, so the difference only shows against a real SQL Server.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import EnrollmentStatus
from app.domain.models import FaceTemplate, Person


class PersonRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, person_id: int, include_templates: bool = False) -> Person | None:
        stmt = select(Person).where(Person.id == person_id)

        if include_templates:
            stmt = stmt.options(
                selectinload(Person.face_templates.and_(FaceTemplate.is_active == True))
            )

        return (await self._session.execute(stmt)).scalars().first()

    async def get_by_external_id(
        self,
        tenant_id: str,
        external_id: str,
        include_templates: bool = False,
    ) -> Person | None:
        stmt = select(Person).where(
            Person.tenant_id == tenant_id, Person.external_id == external_id
        )

        if include_templates:
            stmt = stmt.options(
                selectinload(Person.face_templates.and_(FaceTemplate.is_active == True))
            )

        return (await self._session.execute(stmt)).scalars().first()

    async def external_id_exists(self, tenant_id: str, external_id: str) -> bool:
        stmt = select(func.count()).select_from(Person).where(
            Person.tenant_id == tenant_id, Person.external_id == external_id
        )
        return bool((await self._session.execute(stmt)).scalar_one())

    def add(self, person: Person) -> None:
        self._session.add(person)

    async def remove(self, person: Person) -> None:
        await self._session.delete(person)

    async def search(
        self,
        tenant_id: str,
        query: str | None,
        enrollment_status: EnrollmentStatus | None,
        page: int,
        page_size: int,
    ) -> tuple[list[Person], int]:
        filters = [Person.tenant_id == tenant_id]

        if query and query.strip():
            term = f"%{query.strip()}%"
            filters.append(Person.full_name.like(term) | Person.external_id.like(term))

        if enrollment_status is not None:
            filters.append(Person.enrollment_status == enrollment_status)

        total = (
            await self._session.execute(
                select(func.count()).select_from(Person).where(*filters)
            )
        ).scalar_one()

        items = (
            (
                await self._session.execute(
                    select(Person)
                    .where(*filters)
                    .order_by(Person.full_name)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )

        return list(items), int(total)
