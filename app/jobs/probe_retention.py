"""Deletes attendance probe frames once they age past the configured retention.

Probe frames are useful for a few weeks - long enough to settle a disputed punch - and a
liability after that. Keeping face images of every employee indefinitely is the kind of detail
a data-protection audit asks about, and storage grows by one image per punch per day.

Only the image is removed; the attendance row itself is kept, so the register stays complete
and ``ProbeImageObjectKey`` simply becomes null.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from app.core.clock import Clock
from app.core.config import Settings
from app.db.session import get_session_factory
from app.repositories.attendance_repository import AttendanceRepository
from app.storage.base import FaceImageStore

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 6 * 60 * 60
BATCH_SIZE = 500

#: A short initial delay keeps startup fast and avoids competing with migrations.
STARTUP_DELAY_SECONDS = 60


class ProbeImageRetentionJob:
    def __init__(self, settings: Settings, store: FaceImageStore, clock: Clock) -> None:
        self._settings = settings
        self._store = store
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        retention_days = self._settings.face.probe_image_retention_days

        if retention_days <= 0:
            logger.info(
                "Probe image retention is disabled; frames will be kept indefinitely."
            )
            return

        self._task = asyncio.create_task(self._run(retention_days), name="probe-retention")

    async def stop(self) -> None:
        if self._task is None:
            return

        self._task.cancel()

        try:
            await self._task
        except asyncio.CancelledError:
            pass

        self._task = None

    async def _run(self, retention_days: int) -> None:
        try:
            await asyncio.sleep(STARTUP_DELAY_SECONDS)

            while True:
                try:
                    await self.purge_once(retention_days)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A failed sweep must not kill the host; the next tick will try again.
                    logger.exception("Probe image retention sweep failed")

                await asyncio.sleep(INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return

    async def purge_once(self, retention_days: int) -> int:
        """One sweep. Returns how many frames were deleted."""
        cutoff = self._clock.utc_now() - timedelta(days=retention_days)
        deleted = 0

        factory = get_session_factory()

        async with factory() as session:
            repository = AttendanceRepository(session)

            # Batched so a long-neglected deployment does not try to delete a year of frames
            # in one transaction.
            while True:
                expired = await repository.expired_probe_frames(cutoff, BATCH_SIZE)

                if not expired:
                    break

                for _, object_key in expired:
                    await self._store.delete(object_key)

                await repository.clear_probe_keys([event_id for event_id, _ in expired])
                await session.commit()

                deleted += len(expired)

        if deleted > 0:
            logger.info(
                "Deleted %d probe images captured before %s",
                deleted,
                cutoff.strftime("%Y-%m-%d"),
            )

        return deleted
