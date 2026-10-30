"""Exercises the enrolment rules against a real (in-memory SQLite) database and a scripted
provider, so the repository queries are covered rather than mocked away.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.clock import SystemClock
from app.core.enums import EnrollmentStatus, FacePose
from app.core.errors import DomainErrorKind, DomainException, FaceError, FaceProcessingException
from app.domain.models import FaceTemplate, Person
from app.repositories.face_template_repository import FaceTemplateRepository
from app.repositories.person_repository import PersonRepository
from app.services.commands import CaptureFaceCommand, CreatePersonCommand
from app.services.enrollment import EnrollmentService
from app.services.validator import FaceCaptureValidator
from tests.conftest import face_settings, jpeg

pytestmark = pytest.mark.asyncio


class Harness:
    def __init__(self, session, store, provider, settings):
        self.session = session
        self.store = store
        self.provider = provider
        self.settings = settings
        self.service = EnrollmentService(
            session=session,
            people=PersonRepository(session),
            templates=FaceTemplateRepository(session),
            image_store=store,
            provider=provider,
            validator=FaceCaptureValidator(settings),
            clock=SystemClock(),
            settings=settings,
        )

    async def create(self, external_id: str = "EMP-1", name: str = "Test Person") -> Person:
        return await self.service.create_person(
            CreatePersonCommand("default", external_id, name)
        )

    async def capture(self, person: Person, pose: FacePose, marker: str):
        return await self.service.capture_face(
            CaptureFaceCommand("default", person.id, pose, jpeg(marker))
        )

    async def count_templates(self, person_id: int, active_only: bool = False) -> int:
        stmt = select(func.count()).select_from(FaceTemplate).where(
            FaceTemplate.person_id == person_id
        )
        if active_only:
            stmt = stmt.where(FaceTemplate.is_active.is_(True))
        return int((await self.session.execute(stmt)).scalar_one())

    async def any_people(self, **filters) -> bool:
        stmt = select(func.count()).select_from(Person)
        for column, value in filters.items():
            stmt = stmt.where(getattr(Person, column) == value)
        return bool((await self.session.execute(stmt)).scalar_one())


@pytest_asyncio.fixture
async def harness(session, store, provider):
    return Harness(session, store, provider, face_settings())


async def test_create_person_starts_not_enrolled(harness):
    person = await harness.create()

    assert person.enrollment_status == EnrollmentStatus.NotEnrolled
    assert person.enrolled_at_utc is None


async def test_create_person_duplicate_external_id_conflicts(harness):
    await harness.create("EMP-7")

    with pytest.raises(DomainException) as exc:
        await harness.create("EMP-7")

    assert exc.value.kind == DomainErrorKind.Conflict
    assert exc.value.code == "external_id_exists"


async def test_enrollment_completes_only_after_all_three_poses(harness):
    person = await harness.create()
    harness.provider.identity = "alice"

    front = await harness.capture(person, FacePose.Front, "f")
    assert front.enrollment_status == EnrollmentStatus.InProgress
    assert front.missing_poses == [FacePose.Left, FacePose.Right]

    left = await harness.capture(person, FacePose.Left, "l")
    assert left.enrollment_status == EnrollmentStatus.InProgress

    right = await harness.capture(person, FacePose.Right, "r")

    assert right.enrollment_status == EnrollmentStatus.Enrolled
    assert right.enrollment_completed
    assert right.missing_poses == []


async def test_recapturing_a_pose_supersedes_instead_of_duplicating(harness):
    person = await harness.create()
    harness.provider.identity = "alice"

    await harness.capture(person, FacePose.Front, "f1")
    second = await harness.capture(person, FacePose.Front, "f2")

    assert second.version == 2
    assert second.replaced_previous

    # Exactly one active Front, with the retired row kept for audit.
    assert await harness.count_templates(person.id, active_only=True) == 1
    assert await harness.count_templates(person.id) == 2


async def test_same_frame_for_two_poses_rejected(harness):
    person = await harness.create()
    harness.provider.identity = "alice"

    await harness.capture(person, FacePose.Front, "same")

    with pytest.raises(FaceProcessingException) as exc:
        await harness.capture(person, FacePose.Left, "same")

    assert exc.value.error == FaceError.DuplicateFrame


async def test_second_person_with_the_same_face_rejected(harness):
    alice = await harness.create("EMP-1", "Alice")
    harness.provider.identity = "alice"
    await harness.capture(alice, FacePose.Front, "a")

    impostor = await harness.create("EMP-2", "Impostor")

    with pytest.raises(FaceProcessingException) as exc:
        await harness.capture(impostor, FacePose.Front, "b")

    assert exc.value.error == FaceError.DuplicateIdentity


async def test_different_people_both_enrol_fine(harness):
    alice = await harness.create("EMP-1", "Alice")
    harness.provider.identity = "alice"
    await harness.capture(alice, FacePose.Front, "a")

    bob = await harness.create("EMP-2", "Bob")
    harness.provider.identity = "bob"

    result = await harness.capture(bob, FacePose.Front, "b")
    assert result.pose == FacePose.Front


async def test_inconsistent_poses_do_not_complete_enrollment(session, store, provider):
    # Three different faces submitted under one id: the set is complete but incoherent, so it
    # must not be marked Enrolled.
    harness = Harness(
        session, store, provider, face_settings(reject_duplicate_enrollments=False)
    )
    person = await harness.create()

    provider.identity = "alice"
    await harness.capture(person, FacePose.Front, "f")
    provider.identity = "bob"
    await harness.capture(person, FacePose.Left, "l")
    provider.identity = "carol"
    last = await harness.capture(person, FacePose.Right, "r")

    assert last.enrollment_status == EnrollmentStatus.InProgress
    assert last.warnings


async def test_deleting_a_pose_unenrols_the_person(harness):
    person = await harness.create()
    harness.provider.identity = "alice"

    await harness.capture(person, FacePose.Front, "f")
    await harness.capture(person, FacePose.Left, "l")
    await harness.capture(person, FacePose.Right, "r")

    await harness.service.delete_pose("default", person.id, FacePose.Left)

    summary = await harness.service.get_enrollment("default", person.id)

    assert summary.enrollment_status == EnrollmentStatus.InProgress
    assert summary.missing_poses == [FacePose.Left]


async def test_purge_removes_templates_and_images(harness):
    person = await harness.create()
    harness.provider.identity = "alice"
    await harness.capture(person, FacePose.Front, "f")

    assert len(harness.store.saved) == 1

    await harness.service.purge_biometrics("default", person.id)

    assert harness.store.saved == {}
    assert await harness.count_templates(person.id) == 0


async def test_register_rolls_back_the_person_when_a_capture_fails(harness):
    harness.provider.identity = "alice"
    harness.provider.fail_on = FacePose.Right

    with pytest.raises(FaceProcessingException):
        await harness.service.register(
            CreatePersonCommand("default", "EMP-50", "Doomed"),
            [
                (FacePose.Front, jpeg("f")),
                (FacePose.Left, jpeg("l")),
                (FacePose.Right, jpeg("r")),
            ],
        )

    # No orphan person holding the ExternalId, and no leftover images in the store.
    assert not await harness.any_people(external_id="EMP-50")
    assert harness.store.saved == {}


async def test_capture_for_another_tenant_is_reported_as_not_found(harness):
    person = await harness.create()

    with pytest.raises(DomainException) as exc:
        await harness.service.capture_face(
            CaptureFaceCommand("other-tenant", person.id, FacePose.Front, jpeg("f"))
        )

    assert exc.value.kind == DomainErrorKind.NotFound


async def test_register_same_picture_in_all_three_slots_names_them_and_saves_nothing(harness):
    harness.provider.identity = "alice"
    same = jpeg("one-picture")

    with pytest.raises(FaceProcessingException) as exc:
        await harness.service.register(
            CreatePersonCommand("default", "EMP-60", "Copied Thrice"),
            [(FacePose.Front, same), (FacePose.Left, same), (FacePose.Right, same)],
        )

    assert exc.value.error == FaceError.DuplicateFrame
    assert "Front, Left and Right" in exc.value.message

    # Refused before anything was written: no person, no photo.
    assert not await harness.any_people()
    assert harness.store.saved == {}


async def test_register_same_picture_in_two_slots_names_exactly_those_two(harness):
    harness.provider.identity = "alice"
    same = jpeg("one-picture")

    with pytest.raises(FaceProcessingException) as exc:
        await harness.service.register(
            CreatePersonCommand("default", "EMP-61", "Copied Twice"),
            [
                (FacePose.Front, same),
                (FacePose.Left, jpeg("different")),
                (FacePose.Right, same),
            ],
        )

    assert exc.value.error == FaceError.DuplicateFrame
    assert "Front and Right" in exc.value.message
    assert not await harness.any_people()


async def test_register_stores_all_three_and_completes(harness):
    harness.provider.identity = "alice"

    person, results = await harness.service.register(
        CreatePersonCommand("default", "EMP-70", "Complete"),
        [
            (FacePose.Front, jpeg("f")),
            (FacePose.Left, jpeg("l")),
            (FacePose.Right, jpeg("r")),
        ],
    )

    assert len(results) == 3
    assert results[-1].enrollment_status == EnrollmentStatus.Enrolled
    assert len(harness.store.saved) == 3
    assert await harness.count_templates(person.id, active_only=True) == 3


async def test_capture_many_rejects_a_repeated_pose(harness):
    person = await harness.create()

    with pytest.raises(DomainException) as exc:
        await harness.service.capture_many(
            "default",
            person.id,
            [(FacePose.Front, jpeg("a")), (FacePose.Front, jpeg("b"))],
        )

    assert exc.value.code == "duplicate_pose"


async def test_stale_templates_mark_the_person_stale(harness, session):
    person = await harness.create()
    harness.provider.identity = "alice"

    await harness.capture(person, FacePose.Front, "f")
    await harness.capture(person, FacePose.Left, "l")
    await harness.capture(person, FacePose.Right, "r")

    # Swapping the recognition model must not silently compare incompatible vectors.
    for template in await FaceTemplateRepository(session).get_active_for_person(person.id):
        template.provider_id = "onnx:old-model:v0"
    await session.commit()

    summary = await harness.service.get_enrollment("default", person.id)

    # The stored status is only re-evaluated on the next capture, so the summary reports the
    # mismatch as a warning and flags each pose rather than rewriting the person's status.
    assert summary.warnings
    assert "Re-capture is required" in summary.warnings[0]
    assert all(not pose.provider_matches_current for pose in summary.poses)

    # The next capture is what actually moves the person to Stale.
    harness.provider.identity = "alice"
    result = await harness.capture(person, FacePose.Front, "f-again")

    assert result.enrollment_status == EnrollmentStatus.Stale
