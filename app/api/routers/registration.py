"""Face registration: the three-pose capture flow.

Every upload for one person carries that same person id, which is what ties the three images
to one identity.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Response, UploadFile, status
from fastapi.responses import Response as RawResponse

from app.api.deps import (
    AdminDep,
    EnrollmentServiceDep,
    ImageStoreDep,
    PersonRepoDep,
    SettingsDep,
    TemplateRepoDep,
    TenantDep,
    to_face_image,
)
from app.api.schemas import (
    CaptureFaceResponse,
    PersonResponse,
    RegisterPersonResponse,
)
from app.core.enums import EnrollmentStatus, FacePose
from app.core.errors import DomainException
from app.core.faces import FaceImage
from app.services.commands import CaptureFaceCommand, CreatePersonCommand

router = APIRouter(prefix="/api/v1", tags=["registration"])


@router.post(
    "/registrations",
    response_model=RegisterPersonResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create the person and store all three poses in one request",
    description=(
        "This is the endpoint a capture wizard should call: either the whole registration "
        "succeeds, or the caller gets one clear reason why not, with no half-finished person "
        "left behind."
    ),
)
async def register(
    enrollment: EnrollmentServiceDep,
    tenant: TenantDep,
    settings: SettingsDep,
    response: Response,
    _: AdminDep,
    external_id: Annotated[str, Form(min_length=1, max_length=64)],
    full_name: Annotated[str, Form(min_length=1, max_length=200)],
    front: Annotated[UploadFile, File()],
    left: Annotated[UploadFile, File()],
    right: Annotated[UploadFile, File()],
    email: Annotated[str | None, Form(max_length=256)] = None,
    department: Annotated[str | None, Form(max_length=128)] = None,
) -> RegisterPersonResponse:
    captures: list[tuple[FacePose, FaceImage]] = [
        (FacePose.Front, await to_face_image(front, settings)),
        (FacePose.Left, await to_face_image(left, settings)),
        (FacePose.Right, await to_face_image(right, settings)),
    ]

    person, results = await enrollment.register(
        CreatePersonCommand(
            tenant_id=tenant,
            external_id=external_id,
            full_name=full_name,
            email=email,
            department=department,
        ),
        captures,
    )

    enrollment_status = results[-1].enrollment_status

    response.headers["Location"] = f"/api/v1/persons/{person.id}"

    return RegisterPersonResponse(
        person=PersonResponse.of(person),
        captures=[CaptureFaceResponse.of(r) for r in results],
        enrollment_status=EnrollmentStatus(enrollment_status).name,
        enrollment_completed=enrollment_status == EnrollmentStatus.Enrolled,
    )


@router.post(
    "/persons/{person_id}/faces",
    response_model=CaptureFaceResponse,
    summary="Store one pose for an existing person",
    description=(
        "Safe to call again for the same pose: the new capture supersedes the old one instead "
        "of adding a duplicate."
    ),
)
async def capture_face(
    person_id: int,
    enrollment: EnrollmentServiceDep,
    tenant: TenantDep,
    settings: SettingsDep,
    _: AdminDep,
    pose: Annotated[FacePose, Form()],
    image: Annotated[UploadFile, File()],
) -> CaptureFaceResponse:
    face_image = await to_face_image(image, settings)

    result = await enrollment.capture_face(
        CaptureFaceCommand(tenant_id=tenant, person_id=person_id, pose=pose, image=face_image)
    )

    return CaptureFaceResponse.of(result)


@router.post(
    "/persons/{person_id}/faces/batch",
    response_model=list[CaptureFaceResponse],
    summary="Store all three poses for a person who already exists",
)
async def capture_all_poses(
    person_id: int,
    enrollment: EnrollmentServiceDep,
    tenant: TenantDep,
    settings: SettingsDep,
    _: AdminDep,
    front: Annotated[UploadFile, File()],
    left: Annotated[UploadFile, File()],
    right: Annotated[UploadFile, File()],
) -> list[CaptureFaceResponse]:
    captures: list[tuple[FacePose, FaceImage]] = [
        (FacePose.Front, await to_face_image(front, settings)),
        (FacePose.Left, await to_face_image(left, settings)),
        (FacePose.Right, await to_face_image(right, settings)),
    ]

    results = await enrollment.capture_many(tenant, person_id, captures)

    return [CaptureFaceResponse.of(r) for r in results]


@router.delete(
    "/persons/{person_id}/faces/{pose}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Retire one pose",
    description="The person stops being enrolled until it is re-captured.",
)
async def delete_pose(
    person_id: int,
    pose: FacePose,
    enrollment: EnrollmentServiceDep,
    tenant: TenantDep,
    _: AdminDep,
) -> Response:
    await enrollment.delete_pose(tenant, person_id, pose)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/persons/{person_id}/faces/{pose}/image",
    summary="Download a stored registration image",
    description=(
        "Admin-only and deliberately not cacheable: this returns a photograph of a person's "
        "face."
    ),
    response_class=RawResponse,
)
async def get_pose_image(
    person_id: int,
    pose: FacePose,
    people: PersonRepoDep,
    templates: TemplateRepoDep,
    image_store: ImageStoreDep,
    tenant: TenantDep,
    _: AdminDep,
) -> RawResponse:
    person = await people.get_by_id(person_id, False)

    if person is None or person.tenant_id != tenant:
        raise DomainException.not_found(
            "person_not_found", f"Person {person_id} was not found."
        )

    template = await templates.get_active_for_pose(person_id, pose)

    if template is None:
        raise DomainException.not_found(
            "template_not_found", f"No active {FacePose(pose).name} capture for this person."
        )

    content = await image_store.read(template.image_object_key)

    if content is None:
        raise DomainException.not_found(
            "image_missing", "The template row exists but its image is gone from the store."
        )

    return RawResponse(
        content=content,
        media_type=template.image_content_type,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )
