"""Verification side: match a live frame against the registered poses and record the outcome.

Two modes, and the choice matters more than any threshold:

* **1:1 verification** - the device sends who they claim to be (badge scan, PIN, dropdown)
  and the face only has to confirm it. Accuracy is roughly independent of headcount.
* **1:N identification** - the face alone has to pick one person out of everybody. Convenient,
  but the false-accept rate grows with the size of the population, so the threshold has to
  rise with headcount and a near-tie is treated as inconclusive.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import FaceRecognitionSettings
from app.core.enums import AttendanceDecision, AttendanceDirection, EnrollmentStatus, PersonStatus
from app.core.errors import DomainException, FaceError, FaceProcessingException
from app.core.faces import FaceImage, FaceMatchResult, FaceScore
from app.domain.models import AttendanceEvent, Person
from app.repositories.attendance_repository import AttendanceRepository
from app.repositories.face_template_repository import FaceTemplateRepository
from app.repositories.person_repository import PersonRepository
from app.services.commands import MarkAttendanceCommand, MarkAttendanceResult
from app.services.validator import FaceCaptureValidator
from app.storage.base import FaceImageStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ShiftDecision:
    """What the shift-length rules concluded about one punch."""

    direction: AttendanceDirection

    #: Set when the punch must be refused, carrying the reason the person will read.
    refusal: str | None = None

    #: Set when the server recorded something other than what the device asked for. It is
    #: shown to the person, because a kiosk that says "Checked out" while recording a
    #: check-in is worse than a kiosk that says nothing.
    note: str | None = None


def _spoken_duration(span: timedelta) -> str:
    """``"35 minutes"``, ``"2 hours"``, ``"3h 20m"`` - read off a kiosk screen, not parsed."""
    minutes = int(span.total_seconds() // 60)

    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"

    hours, rest = divmod(minutes, 60)

    if rest == 0:
        return f"{hours} hour{'' if hours == 1 else 's'}"

    return f"{hours}h {rest}m"


class AttendanceService:
    def __init__(
        self,
        session: AsyncSession,
        people: PersonRepository,
        templates: FaceTemplateRepository,
        attendance: AttendanceRepository,
        image_store: FaceImageStore,
        provider,  # FaceRecognitionProvider
        validator: FaceCaptureValidator,
        clock: Clock,
        settings: FaceRecognitionSettings,
    ) -> None:
        self._session = session
        self._people = people
        self._templates = templates
        self._attendance = attendance
        self._image_store = image_store
        self._provider = provider
        self._validator = validator
        self._clock = clock
        self._settings = settings

    async def mark(
        self, command: MarkAttendanceCommand, include_diagnostics: bool = False
    ) -> MarkAttendanceResult:
        started_at = time.perf_counter()
        now = self._clock.utc_now()

        self._validator.validate_upload(command.image)

        claimed = await self._resolve_claimed_person(command)

        # Candidate set: one person for a 1:1 check, the whole tenant for a 1:N search.
        one_to_one = claimed is not None
        threshold = (
            self._settings.verification_threshold
            if one_to_one
            else self._settings.identification_threshold
        )

        if one_to_one and not claimed.can_mark_attendance:
            return await self._record(
                command,
                now,
                started_at,
                claimed,
                AttendanceDecision.Rejected
                if claimed.enrollment_status == EnrollmentStatus.Enrolled
                else AttendanceDecision.NotEnrolled,
                score=0.0,
                threshold=threshold,
                matched_pose=None,
                matched_template_id=None,
                failure_reason=(
                    f"Person is {PersonStatus(claimed.status).name} / "
                    f"{EnrollmentStatus(claimed.enrollment_status).name}."
                ),
                top_scores=[],
            )

        candidates = (
            await self._templates.get_candidates_for_person(
                claimed.id, self._provider.provider_id
            )
            if one_to_one
            else await self._templates.get_candidates(
                command.tenant_id, self._provider.provider_id, None
            )
        )

        if not candidates:
            return await self._record(
                command,
                now,
                started_at,
                claimed,
                AttendanceDecision.NotEnrolled,
                score=0.0,
                threshold=threshold,
                matched_pose=None,
                matched_template_id=None,
                failure_reason=(
                    "No usable templates for this person under the active provider."
                    if one_to_one
                    else "No enrolled templates in this tenant under the active provider."
                ),
                top_scores=[],
            )

        try:
            match: FaceMatchResult = await self._provider.match(
                command.image, candidates, threshold, self._settings.top_n
            )
        except FaceProcessingException as exc:
            if exc.error == FaceError.NoFaceDetected:
                decision = AttendanceDecision.NoFaceDetected
            elif exc.error in (FaceError.LowQuality, FaceError.MultipleFacesDetected):
                decision = AttendanceDecision.LowQuality
            else:
                logger.error("Provider failed during attendance match", exc_info=exc)
                decision = AttendanceDecision.ProviderError

            return await self._record(
                command,
                now,
                started_at,
                claimed,
                decision,
                score=0.0,
                threshold=threshold,
                matched_pose=None,
                matched_template_id=None,
                failure_reason=exc.message,
                top_scores=[],
            )

        if not match.is_match:
            return await self._record(
                command,
                now,
                started_at,
                claimed,
                AttendanceDecision.Rejected,
                score=match.score,
                threshold=threshold,
                matched_pose=None,
                matched_template_id=None,
                failure_reason=(
                    f"Best score {match.score:.3f} did not reach the {threshold:.3f} threshold."
                ),
                top_scores=match.top_scores,
                include_diagnostics=include_diagnostics,
            )

        # A 1:N win that barely beats the runner-up is not a decision, it is a coin toss.
        if (
            not one_to_one
            and match.margin is not None
            and match.margin < self._settings.identification_margin
        ):
            return await self._record(
                command,
                now,
                started_at,
                claimed,
                AttendanceDecision.Rejected,
                score=match.score,
                threshold=threshold,
                matched_pose=match.pose,
                matched_template_id=match.template_id,
                failure_reason=(
                    f"Top two candidates were only {match.margin:.3f} apart; below the "
                    f"required {self._settings.identification_margin:.3f} margin. Ask the "
                    "user to identify themselves first."
                ),
                top_scores=match.top_scores,
                include_diagnostics=include_diagnostics,
            )

        matched = claimed or await self._people.get_by_id(match.person_id, False)

        if matched is None:
            raise DomainException.not_found(
                "person_not_found", "The matched person no longer exists."
            )

        # Collapse accidental double taps BEFORE deciding a direction. Checking afterwards
        # would compare against the inferred direction, which is by definition the opposite of
        # the punch just made - so a second tap two seconds later would sail through as an Out
        # and silently end the person's shift.
        if self._settings.duplicate_punch_window_minutes > 0:
            not_before = now - timedelta(
                minutes=self._settings.duplicate_punch_window_minutes
            )
            recent = await self._attendance.get_last_accepted_in_window(
                matched.id, None, not_before
            )

            if recent is not None:
                return await self._record(
                    command,
                    now,
                    started_at,
                    matched,
                    AttendanceDecision.Duplicate,
                    score=match.score,
                    threshold=threshold,
                    matched_pose=match.pose,
                    matched_template_id=match.template_id,
                    failure_reason=(
                        f"Already marked {AttendanceDirection(recent.direction).name} at "
                        f"{recent.created_at_utc.isoformat()}, inside the "
                        f"{self._settings.duplicate_punch_window_minutes}-minute window."
                    ),
                    top_scores=match.top_scores,
                    include_diagnostics=include_diagnostics,
                    direction=AttendanceDirection(recent.direction),
                )

        shift = await self._decide_shift(command, matched, now)

        if shift.refusal is not None:
            return await self._record(
                command,
                now,
                started_at,
                matched,
                AttendanceDecision.TooSoon,
                score=match.score,
                threshold=threshold,
                matched_pose=match.pose,
                matched_template_id=match.template_id,
                failure_reason=shift.refusal,
                top_scores=match.top_scores,
                include_diagnostics=include_diagnostics,
                direction=shift.direction,
            )

        return await self._record(
            command,
            now,
            started_at,
            matched,
            AttendanceDecision.Accepted,
            score=match.score,
            threshold=threshold,
            matched_pose=match.pose,
            matched_template_id=match.template_id,
            failure_reason=None,
            top_scores=match.top_scores,
            include_diagnostics=include_diagnostics,
            direction=shift.direction,
            note=shift.note,
        )

    async def preview_match(
        self, tenant_id: str, person_id: int | None, image: FaceImage
    ) -> FaceMatchResult:
        """Score a frame without recording anything.

        Useful for a "is the camera seeing me?" indicator on the capture screen, and for
        tuning thresholds against real traffic.
        """
        self._validator.validate_upload(image)

        one_to_one = person_id is not None
        threshold = (
            self._settings.verification_threshold
            if one_to_one
            else self._settings.identification_threshold
        )

        candidates = (
            await self._templates.get_candidates_for_person(
                person_id, self._provider.provider_id
            )
            if one_to_one
            else await self._templates.get_candidates(
                tenant_id, self._provider.provider_id, None
            )
        )

        if not candidates:
            return FaceMatchResult.no_match(threshold)

        return await self._provider.match(image, candidates, threshold, self._settings.top_n)

    # ---- internals -----------------------------------------------------------------------

    async def _resolve_claimed_person(self, command: MarkAttendanceCommand) -> Person | None:
        if command.person_id is not None:
            person = await self._people.get_by_id(command.person_id, False)

            if person is None or person.tenant_id != command.tenant_id:
                raise DomainException.not_found(
                    "person_not_found", f"Person {command.person_id} was not found."
                )

            return person

        if command.external_id and command.external_id.strip():
            person = await self._people.get_by_external_id(
                command.tenant_id, command.external_id.strip(), False
            )

            if person is None:
                raise DomainException.not_found(
                    "person_not_found",
                    f"No person with ExternalId {command.external_id} in this tenant.",
                )

            return person

        return None

    async def _resolve_direction(
        self, command: MarkAttendanceCommand, person: Person
    ) -> AttendanceDirection:
        """Decide In vs Out.

        Trusting the client is fine for a kiosk you control, but inferring from the previous
        event means a mis-tapped button cannot corrupt the day.
        """
        if not self._settings.infer_direction and command.direction is not None:
            return command.direction

        last = await self._attendance.get_last_accepted(person.id)

        if last is None:
            return command.direction or AttendanceDirection.In

        # A new day always starts with an In, even if yesterday ended without an Out.
        same_day = last.created_at_utc.date() == self._clock.utc_now().date()

        if not same_day:
            return AttendanceDirection.In

        return (
            AttendanceDirection.Out
            if AttendanceDirection(last.direction) == AttendanceDirection.In
            else AttendanceDirection.In
        )

    async def _decide_shift(
        self, command: MarkAttendanceCommand, person: Person, now
    ) -> _ShiftDecision:
        """Apply the shift-length rules on top of the In/Out choice.

        Two rules, and the order between them is the whole design:

        1. A check-in left open longer than ``open_shift_expiry_hours`` is **abandoned**.
           Nobody worked sixteen hours and forgot to go home; they forgot to check out. The
           stale In is left unpaired - the register already shows that as "Punch missing" -
           and this punch starts a new one, whichever direction the device asked for. This is
           the only place the server overrules the app's choice.
        2. A check-out sooner than ``minimum_shift_minutes`` after its check-in is
           **refused**, and the person stays checked in so the shift can still be closed
           properly later.

        Rule 1 has to come before rule 2. Measured against a stale check-in, the next
        morning's punch is simultaneously "sixteen hours late" and "too soon", and deciding
        it the other way round would refuse it - leaving somebody unable to start work, with
        no way to clear it from the kiosk.

        Nothing here invents a punch. The alternative design - writing a closing Out at
        check-in plus sixteen hours - would read better on the register and would put hours
        nobody worked into payroll.

        A check-out with no check-in open is deliberately left alone. It pairs with nothing,
        so it credits no hours and the register already flags it; overruling it would mean
        this service second-guessing the app's own in/out state, which
        ``infer_direction=false`` exists to avoid.
        """
        wanted = await self._resolve_direction(command, person)
        last = await self._attendance.get_last_accepted(person.id)

        open_in = (
            last
            if last is not None
            and AttendanceDirection(last.direction) == AttendanceDirection.In
            else None
        )

        # Rule 1.
        if open_in is not None and self._settings.open_shift_expiry_hours > 0:
            open_for = now - open_in.created_at_utc

            if open_for >= timedelta(hours=self._settings.open_shift_expiry_hours):
                logger.info(
                    "Person %s had a check-in open for %s; treating it as abandoned and "
                    "starting a fresh one",
                    person.id,
                    _spoken_duration(open_for),
                )

                return _ShiftDecision(
                    AttendanceDirection.In,
                    note=(
                        f"Your last check-in was {_spoken_duration(open_for)} ago and was "
                        "never closed. This is a new check-in."
                    ),
                )

        # Rule 2.
        if (
            wanted == AttendanceDirection.Out
            and open_in is not None
            and self._settings.minimum_shift_minutes > 0
        ):
            worked = now - open_in.created_at_utc
            minimum = timedelta(minutes=self._settings.minimum_shift_minutes)

            if worked < minimum:
                return _ShiftDecision(
                    AttendanceDirection.Out,
                    refusal=(
                        f"Checked in {_spoken_duration(worked)} ago, and a check-out needs "
                        f"at least {_spoken_duration(minimum)}. You are still checked in."
                    ),
                )

        return _ShiftDecision(wanted)

    async def _record(
        self,
        command: MarkAttendanceCommand,
        now,
        started_at: float,
        person: Person | None,
        decision: AttendanceDecision,
        score: float,
        threshold: float,
        matched_pose,
        matched_template_id: int | None,
        failure_reason: str | None,
        top_scores: list[FaceScore],
        include_diagnostics: bool = False,
        direction: AttendanceDirection | None = None,
        note: str | None = None,
    ) -> MarkAttendanceResult:
        resolved_direction = direction or command.direction or AttendanceDirection.In

        probe_key: str | None = None

        if self._settings.store_probe_images:
            # Written before the row is inserted, so the row's number does not exist yet. A
            # random token names the file instead; the row records the path, which is what
            # links them.
            probe_key = self._image_store.build_probe_key(
                command.tenant_id, now, uuid4(), command.image.content_type
            )

            try:
                await self._image_store.save(
                    probe_key, command.image.content, command.image.content_type
                )
            except Exception:
                # Losing the audit frame must not lose the punch.
                logger.exception("Could not store probe image %s", probe_key)
                probe_key = None

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)

        record = AttendanceEvent(
            tenant_id=command.tenant_id,
            person_id=person.id if person else None,
            direction=resolved_direction,
            decision=decision,
            match_score=score,
            threshold=threshold,
            matched_pose=matched_pose,
            matched_template_id=matched_template_id,
            provider_id=self._provider.provider_id,
            probe_image_object_key=probe_key,
            failure_reason=failure_reason,
            device_id=command.device_id,
            latitude=command.latitude,
            longitude=command.longitude,
            captured_at_utc=command.captured_at_utc,
            created_at_utc=now,
            processing_ms=elapsed_ms,
        )

        self._attendance.add(record)
        await self._session.commit()
        await self._session.refresh(record)

        if decision == AttendanceDecision.Accepted:
            logger.info(
                "Attendance %s accepted for %s at %.3f in %dms",
                resolved_direction.name,
                person.id if person else None,
                score,
                elapsed_ms,
            )
        else:
            logger.info(
                "Attendance %s for %s: %s (score %.3f, %dms)",
                decision.name,
                person.id if person else None,
                failure_reason,
                score,
                elapsed_ms,
            )

        return MarkAttendanceResult(
            success=decision == AttendanceDecision.Accepted,
            decision=decision,
            event_id=record.id,
            person_id=person.id if person else None,
            external_id=person.external_id if person else None,
            full_name=person.full_name if person else None,
            direction=resolved_direction if decision == AttendanceDecision.Accepted else None,
            score=score,
            threshold=threshold,
            matched_pose=matched_pose,
            recorded_at_utc=now,
            # On a refusal this is the reason; on an accepted punch it is only set when the
            # server recorded a different direction than the device asked for. Never both.
            message=failure_reason or note,
            processing_ms=elapsed_ms,
            top_scores=list(top_scores) if include_diagnostics else [],
        )
