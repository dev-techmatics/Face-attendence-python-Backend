"""People who can be registered.

Administration only; a kiosk key cannot reach these.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Response, status

from app.api.deps import AdminDep, EnrollmentServiceDep, PersonRepoDep, TenantDep
from app.api.schemas import (
    CreatePersonRequest,
    EnrollmentResponse,
    PagedResponse,
    PersonResponse,
)
from app.core.enums import EnrollmentStatus
from app.core.errors import DomainException
from app.services.commands import CreatePersonCommand

router = APIRouter(prefix="/api/v1/persons", tags=["persons"])


@router.post(
    "",
    response_model=PersonResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a person",
    description="Faces are attached separately, or in one call via /registrations.",
)
async def create(
    request: CreatePersonRequest,
    enrollment: EnrollmentServiceDep,
    tenant: TenantDep,
    response: Response,
    _: AdminDep,
) -> PersonResponse:
    person = await enrollment.create_person(
        CreatePersonCommand(
            tenant_id=tenant,
            external_id=request.external_id,
            full_name=request.full_name,
            email=str(request.email) if request.email else None,
            department=request.department,
        )
    )

    response.headers["Location"] = f"/api/v1/persons/{person.id}"
    return PersonResponse.of(person)


@router.get("/{person_id}", response_model=PersonResponse)
async def get_by_id(
    person_id: int,
    people: PersonRepoDep,
    tenant: TenantDep,
    _: AdminDep,
) -> PersonResponse:
    person = await people.get_by_id(person_id, include_templates=False)

    if person is None or person.tenant_id != tenant:
        raise DomainException.not_found(
            "person_not_found", f"Person {person_id} was not found."
        )

    return PersonResponse.of(person)


@router.get(
    "/by-external-id/{external_id}",
    response_model=PersonResponse,
    summary="Look a person up by the id your own system uses",
)
async def get_by_external_id(
    external_id: str,
    people: PersonRepoDep,
    tenant: TenantDep,
    _: AdminDep,
) -> PersonResponse:
    person = await people.get_by_external_id(tenant, external_id, False)

    if person is None:
        raise DomainException.not_found(
            "person_not_found", f"No person with ExternalId {external_id} in this tenant."
        )

    return PersonResponse.of(person)


@router.get("", response_model=PagedResponse[PersonResponse])
async def search(
    people: PersonRepoDep,
    tenant: TenantDep,
    _: AdminDep,
    query: str | None = None,
    enrollment_status: EnrollmentStatus | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> PagedResponse[PersonResponse]:
    items, total = await people.search(tenant, query, enrollment_status, page, page_size)

    return PagedResponse[PersonResponse](
        items=[PersonResponse.of(p) for p in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/{person_id}/enrollment",
    response_model=EnrollmentResponse,
    summary="Current enrolment state",
    description=(
        "Which poses exist, which are missing, and how similar the stored poses are to each "
        "other."
    ),
)
async def get_enrollment(
    person_id: int,
    enrollment: EnrollmentServiceDep,
    tenant: TenantDep,
    _: AdminDep,
) -> EnrollmentResponse:
    return EnrollmentResponse.of(await enrollment.get_enrollment(tenant, person_id))


@router.delete(
    "/{person_id}/biometrics",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Erase every face template and stored image for this person",
    description=(
        "Includes the cloud provider when one is in use. This is the endpoint a data-erasure "
        "request needs."
    ),
)
async def purge_biometrics(
    person_id: int,
    enrollment: EnrollmentServiceDep,
    tenant: TenantDep,
    _: AdminDep,
) -> Response:
    await enrollment.purge_biometrics(tenant, person_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
