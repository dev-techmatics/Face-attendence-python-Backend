"""Registration side of the system.

Create a person, then attach one face template per pose, all under the same person id.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import FaceRecognitionSettings
from app.core.enums import EnrollmentStatus, FacePose, FaceTemplateKind, PersonStatus
from app.core.errors import DomainException, FaceError, FaceProcessingException
from app.core.faces import FaceImage, FaceTemplateMaterial
from app.domain.models import FaceTemplate, Person
from app.repositories.face_template_repository import FaceTemplateRepository
from app.repositories.person_repository import PersonRepository
from app.services.commands import (
    CapturedPoseInfo,
    CaptureFaceCommand,
    CaptureFaceResult,
    CreatePersonCommand,
    EnrollmentSummary,
    PoseSimilarity,
)
from app.services.validator import FaceCaptureValidator
from app.storage.base import FaceImageStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _EnrollmentEvaluation:
    status: EnrollmentStatus
    missing_poses: list[FacePose]
    warnings: list[str]


class EnrollmentService:
    def __init__(
        self,
        session: AsyncSession,
        people: PersonRepository,
        templates: FaceTemplateRepository,
        image_store: FaceImageStore,
        provider,  # FaceRecognitionProvider
        validator: FaceCaptureValidator,
        clock: Clock,
        settings: FaceRecognitionSettings,
    ) -> None:
        self._session = session
        self._people = people
        self._templates = templates
        self._image_store = image_store
        self._provider = provider
        self._validator = validator
        self._clock = clock
        self._settings = settings

    async def create_person(self, command: CreatePersonCommand) -> Person:
        if not command.external_id or not command.external_id.strip():
            raise DomainException.validation("external_id_required", "ExternalId is required.")

        if not command.full_name or not command.full_name.strip():
            raise DomainException.validation("full_name_required", "FullName is required.")

        external_id = command.external_id.strip()

        if await self._people.external_id_exists(command.tenant_id, external_id):
            raise DomainException.conflict(
                "external_id_exists",
                f"A person with ExternalId {external_id} already exists in this tenant.",
            )

        now = self._clock.utc_now()
        person = Person(
            tenant_id=command.tenant_id,
            external_id=external_id,
            full_name=command.full_name.strip(),
            email=command.email.strip() if command.email else None,
            department=command.department.strip() if command.department else None,
            status=PersonStatus.Active,
            enrollment_status=EnrollmentStatus.NotEnrolled,
            created_at_utc=now,
            updated_at_utc=now,
        )

        self._people.add(person)
        await self._session.commit()
        await self._session.refresh(person)

        logger.info(
            "Created person %s (%s) in tenant %s",
            person.id,
            person.external_id,
            person.tenant_id,
        )

        return person

    async def capture_face(self, command: CaptureFaceCommand) -> CaptureFaceResult:
        """Store one pose for a person.

        Calling it again for the same pose supersedes the previous capture rather than
        duplicating it, so a retry from the capture screen is always safe.
        """
        person = await self._load_person(command.tenant_id, command.person_id)

        if person.status != PersonStatus.Active:
            raise DomainException.conflict(
                "person_not_active",
                f"Person {person.id} is {PersonStatus(person.status).name} and cannot be "
                "enrolled.",
            )

        self._validator.validate_upload(command.image)

        # The same frame sent for Front and then Left would otherwise produce an enrolment
        # that looks complete while carrying no angular variety at all.
        if self._settings.reject_duplicate_frames and await self._templates.image_hash_exists_for_person(
            person.id, command.image.sha256, command.pose
        ):
            raise FaceProcessingException(
                FaceError.DuplicateFrame,
                "This exact image is already stored for a different pose of this person.",
                hints=["Capture a fresh frame for each of the three angles."],
            )

        material = await self._provider.create_template(command.image, command.pose)

        if material.analysis is not None:
            self._validator.validate_analysis(material.analysis)
            self._validator.validate_pose(material.analysis, command.pose)

        await self._guard_against_duplicate_identity(person, material)

        version = await self._templates.get_max_version(person.id, command.pose) + 1

        object_key = self._image_store.build_enrollment_key(
            person.tenant_id,
            person.id,
            FacePose(command.pose).name,
            version,
            command.image.content_type,
        )

        await self._image_store.save(
            object_key, command.image.content, command.image.content_type
        )

        previous = await self._templates.get_active_for_pose(person.id, command.pose)
        if previous is not None:
            previous.is_active = False

        template = FaceTemplate(
            person_id=person.id,
            pose=command.pose,
            provider_id=material.provider_id,
            dimension=material.dimension,
            template_data=material.data,
            quality_score=material.quality_score,
            detection_score=material.detection_score,
            image_object_key=object_key,
            image_content_type=command.image.content_type,
            image_size_bytes=command.image.size_bytes,
            image_sha256=command.image.sha256,
            image_width=material.analysis.image_width if material.analysis else 0,
            image_height=material.analysis.image_height if material.analysis else 0,
            is_active=True,
            version=version,
            created_at_utc=self._clock.utc_now(),
        )

        self._templates.add(template)
        person.updated_at_utc = self._clock.utc_now()
        await self._session.commit()
        await self._session.refresh(template)

        # Re-read so the status decision sees exactly what the database now holds.
        active = await self._templates.get_active_for_person(person.id)
        evaluation = self._evaluate(active)

        completed = (
            evaluation.status == EnrollmentStatus.Enrolled
            and person.enrollment_status != EnrollmentStatus.Enrolled
        )

        person.enrollment_status = evaluation.status
        person.enrolled_at_utc = (
            (person.enrolled_at_utc or self._clock.utc_now())
            if evaluation.status == EnrollmentStatus.Enrolled
            else None
        )
        person.updated_at_utc = self._clock.utc_now()
        await self._session.commit()

        logger.info(
            "Captured %s v%d for person %s; enrollment is now %s",
            FacePose(command.pose).name,
            version,
            person.id,
            evaluation.status.name,
        )

        analysis_warnings = list(material.analysis.warnings) if material.analysis else []

        return CaptureFaceResult(
            person_id=person.id,
            pose=command.pose,
            template_id=template.id,
            version=version,
            image_object_key=object_key,
            analysis=material.analysis,
            enrollment_status=evaluation.status,
            captured_poses=sorted({FacePose(t.pose) for t in active}),
            missing_poses=evaluation.missing_poses,
            enrollment_completed=completed,
            replaced_previous=previous is not None,
            warnings=[*analysis_warnings, *evaluation.warnings],
        )

    async def capture_many(
        self,
        tenant_id: str,
        person_id: int,
        captures: Sequence[tuple[FacePose, FaceImage]],
    ) -> list[CaptureFaceResult]:
        """Store several poses in one request.

        Each is validated independently; the first failure stops the batch, leaving the poses
        already accepted in place.
        """
        if not captures:
            raise DomainException.validation("no_images", "At least one image is required.")

        seen: dict[FacePose, int] = {}
        for pose, _ in captures:
            seen[pose] = seen.get(pose, 0) + 1

        repeated = [pose for pose, count in seen.items() if count > 1]

        if repeated:
            names = ", ".join(FacePose(p).name for p in sorted(repeated))
            raise DomainException.validation(
                "duplicate_pose",
                f"Each pose may appear once per request; repeated: {names}.",
            )

        _ensure_distinct_pictures(captures)

        results: list[CaptureFaceResult] = []

        # Front first: it is the pose the duplicate-identity check is most reliable on, so
        # someone already registered under another id is caught before profiles are stored.
        ordered = sorted(captures, key=lambda c: 0 if c[0] == FacePose.Front else 1)

        for pose, image in ordered:
            results.append(
                await self.capture_face(CaptureFaceCommand(tenant_id, person_id, pose, image))
            )

        return results

    async def get_enrollment(self, tenant_id: str, person_id: int) -> EnrollmentSummary:
        person = await self._load_person(tenant_id, person_id)
        active = await self._templates.get_active_for_person(person_id)
        evaluation = self._evaluate(active)

        similarities: list[PoseSimilarity] = []

        for i in range(len(active)):
            for j in range(i + 1, len(active)):
                score = self._safe_compare(active[i], active[j])
                if score is not None:
                    similarities.append(
                        PoseSimilarity(
                            FacePose(active[i].pose), FacePose(active[j].pose), score
                        )
                    )

        return EnrollmentSummary(
            person_id=person.id,
            external_id=person.external_id,
            full_name=person.full_name,
            status=PersonStatus(person.status),
            enrollment_status=EnrollmentStatus(person.enrollment_status),
            enrolled_at_utc=person.enrolled_at_utc,
            poses=[
                CapturedPoseInfo(
                    pose=FacePose(t.pose),
                    template_id=t.id,
                    version=t.version,
                    quality_score=t.quality_score,
                    detection_score=t.detection_score,
                    provider_id=t.provider_id,
                    provider_matches_current=t.provider_id == self._provider.provider_id,
                    captured_at_utc=t.created_at_utc,
                )
                for t in sorted(active, key=lambda t: t.pose)
            ],
            missing_poses=evaluation.missing_poses,
            pose_similarities=similarities,
            warnings=evaluation.warnings,
        )

    async def delete_pose(self, tenant_id: str, person_id: int, pose: FacePose) -> None:
        """Retire one pose. The person drops back to InProgress if it was required."""
        person = await self._load_person(tenant_id, person_id)

        template = await self._templates.get_active_for_pose(person_id, pose)

        if template is None:
            raise DomainException.not_found(
                "template_not_found",
                f"No active {FacePose(pose).name} capture for this person.",
            )

        template.is_active = False
        await self._session.commit()

        active = await self._templates.get_active_for_person(person_id)
        person.enrollment_status = self._evaluate(active).status

        if person.enrollment_status != EnrollmentStatus.Enrolled:
            person.enrolled_at_utc = None

        person.updated_at_utc = self._clock.utc_now()
        await self._session.commit()

    async def purge_biometrics(self, tenant_id: str, person_id: int) -> None:
        """Erase all biometric data for a person.

        Provider-side templates, stored images and rows. This is what a privacy-law erasure
        request actually needs.
        """
        person = await self._load_person(tenant_id, person_id)
        all_templates = await self._templates.get_all_for_person(person_id)

        await self._provider.delete_templates([t.template_data for t in all_templates])

        for template in all_templates:
            if template.image_object_key:
                await self._image_store.delete(template.image_object_key)

            await self._templates.remove(template)

        person.enrollment_status = EnrollmentStatus.NotEnrolled
        person.enrolled_at_utc = None
        person.updated_at_utc = self._clock.utc_now()
        await self._session.commit()

        logger.warning(
            "Purged %d face templates and images for person %s", len(all_templates), person_id
        )

    async def register(
        self,
        command: CreatePersonCommand,
        captures: Sequence[tuple[FacePose, FaceImage]],
    ) -> tuple[Person, list[CaptureFaceResult]]:
        """Create the person and store every pose as one unit.

        If any capture is rejected the new person record is removed again, because a person
        row holding no usable face is worse than no row at all: it occupies the ExternalId and
        reads as enrolled in a list.

        This is compensation, not a database transaction: images already written to the store
        are deleted on the way out. A single SQL transaction would not help anyway, since the
        image store is a separate system.
        """
        # Checked before the person is created: an upload with the same picture in two slots
        # can never succeed, so there is no reason to insert a row, consume an id, and roll
        # it back.
        _ensure_distinct_pictures(captures)

        person = await self.create_person(command)

        try:
            results = await self.capture_many(command.tenant_id, person.id, captures)
            return person, results
        except Exception:
            try:
                await self.purge_biometrics(command.tenant_id, person.id)
                await self._people.remove(person)
                await self._session.commit()
            except Exception:
                # Never let cleanup mask the real reason the registration failed.
                logger.exception(
                    "Could not roll back the partial registration for %s; it may need manual "
                    "removal",
                    person.id,
                )
                await self._session.rollback()

            raise

    # ---- internals -----------------------------------------------------------------------

    async def _load_person(self, tenant_id: str, person_id: int) -> Person:
        person = await self._people.get_by_id(person_id, include_templates=False)

        if person is None:
            raise DomainException.not_found(
                "person_not_found", f"Person {person_id} was not found."
            )

        if person.tenant_id != tenant_id:
            # Reported as not-found rather than forbidden: confirming the id exists in another
            # tenant would already leak more than the caller is entitled to know.
            raise DomainException.not_found(
                "person_not_found", f"Person {person_id} was not found."
            )

        return person

    async def _guard_against_duplicate_identity(
        self, person: Person, material: FaceTemplateMaterial
    ) -> None:
        """Refuse a registration whose face already belongs to a different person id.

        Without this, one person can hold two identities and cover for an absent colleague.
        """
        if (
            not self._settings.reject_duplicate_enrollments
            or material.kind != FaceTemplateKind.Embedding
        ):
            return

        # Everybody, including the archived and the suspended. "Is this face already on
        # file" is not the same question as "who may clock in", and asking the narrower one
        # here is what let a removed person register again under a new id.
        others = await self._templates.get_candidates(
            person.tenant_id,
            self._provider.provider_id,
            exclude_person_id=person.id,
            include_inactive=True,
        )

        if not others:
            return

        conflicting = None
        best = float("-inf")

        for candidate in others:
            score = self._provider.compare_templates(material.data, candidate.template_data)
            if score is not None and score > best:
                best = score
                conflicting = candidate

        if conflicting is not None and best >= self._settings.duplicate_enrollment_threshold:
            existing = await self._people.get_by_id(conflicting.person_id)
            archived = existing is not None and existing.status != PersonStatus.Active

            logger.warning(
                "Rejected enrollment for %s: scored %.3f against existing person %s (%s)",
                person.id,
                best,
                conflicting.person_id,
                "archived" if archived else "active",
            )

            # The screen that shows this is in front of whoever is registering, so it names
            # no colleague. What it does say is which way out applies, because "already
            # registered" sends an operator hunting for a record that is not in the list.
            raise FaceProcessingException(
                FaceError.DuplicateIdentity,
                (
                    "This face belongs to a record that was removed. Ask your admin to put "
                    "that record back instead of registering again."
                    if archived
                    else "This face is already registered under another ID."
                ),
                hints=[
                    "If this person is genuinely new, an operator should review the existing "
                    "record.",
                    f"Conflicting person id: {conflicting.person_id}"
                    + (" (archived)" if archived else ""),
                ],
            )

    def _evaluate(self, active: Sequence[FaceTemplate]) -> _EnrollmentEvaluation:
        captured = {FacePose(t.pose) for t in active}
        missing = [p for p in self._settings.required_poses if p not in captured]

        if missing:
            return _EnrollmentEvaluation(
                EnrollmentStatus.NotEnrolled if not active else EnrollmentStatus.InProgress,
                missing,
                [],
            )

        # Templates made by a retired model cannot be compared against fresh probes.
        if any(t.provider_id != self._provider.provider_id for t in active):
            return _EnrollmentEvaluation(
                EnrollmentStatus.Stale,
                [],
                [
                    "Some templates came from another engine or model version. "
                    f"Re-capture is required for provider {self._provider.provider_id}."
                ],
            )

        warnings: list[str] = []

        if self._settings.validate_enrollment_consistency:
            floor = self._settings.min_enrollment_pair_similarity

            for i in range(len(active)):
                for j in range(i + 1, len(active)):
                    score = self._safe_compare(active[i], active[j])
                    if score is not None and score < floor:
                        warnings.append(
                            f"{FacePose(active[i].pose).name} and "
                            f"{FacePose(active[j].pose).name} scored {score:.2f}, below the "
                            f"{floor:.2f} floor - they may not be the same person."
                        )

            if warnings:
                return _EnrollmentEvaluation(EnrollmentStatus.InProgress, [], warnings)

        return _EnrollmentEvaluation(EnrollmentStatus.Enrolled, [], warnings)

    def _safe_compare(self, a: FaceTemplate, b: FaceTemplate) -> float | None:
        try:
            return self._provider.compare_templates(a.template_data, b.template_data)
        except ValueError:
            logger.warning("Could not compare templates %s and %s", a.id, b.id)
            return None


def _ensure_distinct_pictures(captures: Sequence[tuple[FacePose, FaceImage]]) -> None:
    """Refuse a batch in which two slots carry the byte-identical picture, naming the slots.

    The per-capture check would catch this too, but only after the first pose was stored, and
    with a message that does not say which photos clash. Copying one file into all three slots
    is the most common registration mistake, so it gets a precise answer up front.
    """
    by_hash: dict[str, list[FacePose]] = {}

    for pose, image in captures:
        by_hash.setdefault(image.sha256, []).append(pose)

    clash = next((poses for poses in by_hash.values() if len(poses) > 1), None)

    if clash is None:
        return

    names = [FacePose(p).name for p in sorted(clash)]
    slots = (
        f"{names[0]} and {names[1]}"
        if len(names) == 2
        else f"{', '.join(names[:-1])} and {names[-1]}"
    )

    raise FaceProcessingException(
        FaceError.DuplicateFrame,
        f"The {slots} photos are the same picture. Each photo must be a different picture.",
        hints=[
            "Take a separate photo for each angle: looking straight, turned left, turned right.",
            "Renaming a file does not make it a different picture.",
        ],
    )
