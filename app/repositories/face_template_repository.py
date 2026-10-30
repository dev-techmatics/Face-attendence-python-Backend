"""Reads and writes for :class:`~app.domain.models.FaceTemplate`.

``is_active == True`` rather than ``.is_(True)`` throughout: SQLAlchemy renders ``.is_(True)``
as ``IS 1``, which SQL Server rejects outright - T-SQL has no ``IS <value>``, only ``IS NULL``.
SQLite accepts it, so the difference only shows against a real SQL Server.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import FacePose, PersonStatus
from app.core.faces import FaceCandidate
from app.domain.models import FaceTemplate, Person


class FaceTemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_for_person(self, person_id: int) -> list[FaceTemplate]:
        stmt = (
            select(FaceTemplate)
            .where(FaceTemplate.person_id == person_id, FaceTemplate.is_active == True)
            .order_by(FaceTemplate.pose)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_active_for_pose(self, person_id: int, pose: FacePose) -> FaceTemplate | None:
        stmt = select(FaceTemplate).where(
            FaceTemplate.person_id == person_id,
            FaceTemplate.pose == pose,
            FaceTemplate.is_active == True,
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def get_all_for_person(self, person_id: int) -> list[FaceTemplate]:
        stmt = select(FaceTemplate).where(FaceTemplate.person_id == person_id)
        return list((await self._session.execute(stmt)).scalars().all())

    def add(self, template: FaceTemplate) -> None:
        self._session.add(template)

    async def remove(self, template: FaceTemplate) -> None:
        await self._session.delete(template)

    async def get_candidates(
        self,
        tenant_id: str,
        provider_id: str,
        exclude_person_id: int | None = None,
        include_inactive: bool = False,
    ) -> list[FaceCandidate]:
        """The 1:N candidate set.

        Projected straight to :class:`FaceCandidate` rather than to entities: the identity map
        has no business holding thousands of embeddings.

        ``include_inactive`` is the difference between two questions that look alike. Matching
        somebody at a kiosk asks "who here may clock in", so it leaves out the archived and the
        suspended. Checking a new registration asks "is this face already on file", and that
        one must see everybody - otherwise archiving a person quietly licenses them to come
        back under a second identity, with their hours split across the two.
        """
        stmt = (
            select(
                FaceTemplate.person_id,
                FaceTemplate.id,
                FaceTemplate.pose,
                FaceTemplate.template_data,
                FaceTemplate.image_object_key,
            )
            .join(Person, Person.id == FaceTemplate.person_id)
            .where(
                FaceTemplate.is_active == True,
                FaceTemplate.provider_id == provider_id,
                Person.tenant_id == tenant_id,
            )
        )

        if not include_inactive:
            stmt = stmt.where(Person.status == PersonStatus.Active)

        if exclude_person_id is not None:
            stmt = stmt.where(FaceTemplate.person_id != exclude_person_id)

        rows = (await self._session.execute(stmt)).all()

        return [
            FaceCandidate(
                person_id=row[0],
                template_id=row[1],
                pose=FacePose(row[2]),
                template_data=row[3],
                image_object_key=row[4],
            )
            for row in rows
        ]

    async def get_candidates_for_person(
        self, person_id: int, provider_id: str
    ) -> list[FaceCandidate]:
        stmt = select(
            FaceTemplate.person_id,
            FaceTemplate.id,
            FaceTemplate.pose,
            FaceTemplate.template_data,
            FaceTemplate.image_object_key,
        ).where(
            FaceTemplate.person_id == person_id,
            FaceTemplate.is_active == True,
            FaceTemplate.provider_id == provider_id,
        )

        rows = (await self._session.execute(stmt)).all()

        return [
            FaceCandidate(
                person_id=row[0],
                template_id=row[1],
                pose=FacePose(row[2]),
                template_data=row[3],
                image_object_key=row[4],
            )
            for row in rows
        ]

    async def get_max_version(self, person_id: int, pose: FacePose) -> int:
        """Counts retired rows too, so versions never repeat for a person/pose pair."""
        stmt = select(func.max(FaceTemplate.version)).where(
            FaceTemplate.person_id == person_id, FaceTemplate.pose == pose
        )
        return int((await self._session.execute(stmt)).scalar() or 0)

    async def image_hash_exists_for_person(
        self, person_id: int, sha256: str, exclude_pose: FacePose
    ) -> bool:
        stmt = (
            select(func.count())
            .select_from(FaceTemplate)
            .where(
                FaceTemplate.person_id == person_id,
                FaceTemplate.is_active == True,
                FaceTemplate.pose != exclude_pose,
                FaceTemplate.image_sha256 == sha256,
            )
        )
        return bool((await self._session.execute(stmt)).scalar_one())
