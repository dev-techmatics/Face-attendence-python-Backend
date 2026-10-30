"""Verification against the registered poses, and the resulting attendance record."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.api.deps import (
    AdminDep,
    AttendanceRepoDep,
    AttendanceServiceDep,
    DeviceDep,
    SettingsDep,
    TenantDep,
    to_face_image,
)
from app.api.rate_limit import attendance_rate_limit
from app.api.schemas import (
    AnalysisResponse,
    AttendanceEventResponse,
    MarkAttendanceResponse,
    PagedResponse,
    PreviewMatchResponse,
    ScoreResponse,
)
from app.core.enums import AttendanceDecision, AttendanceDirection, FacePose
from app.services.commands import MarkAttendanceCommand

router = APIRouter(prefix="/api/v1/attendance", tags=["attendance"])


@router.post(
    "/mark",
    response_model=MarkAttendanceResponse,
    dependencies=[Depends(attendance_rate_limit)],
    summary="Verify a live frame and record the punch",
    description=(
        "Send `person_id` or `external_id` whenever the device can obtain it - a badge tap, a "
        "PIN, a name picker. That makes this a 1:1 verification, which is faster and far more "
        "accurate than searching the whole population, because its error rate does not grow "
        "with headcount.\n\n"
        "Omitting both performs a 1:N identification. Convenient, but the chance of a false "
        "accept rises with the number of enrolled people, so the threshold is stricter and a "
        "near-tie between two candidates is refused rather than guessed.\n\n"
        "A non-match is still a 200 with `success: false` and a decision code: the request was "
        "processed correctly, it simply did not match. Reserve 4xx for malformed requests."
    ),
)
async def mark(
    attendance: AttendanceServiceDep,
    tenant: TenantDep,
    settings: SettingsDep,
    _: DeviceDep,
    image: Annotated[UploadFile, File()],
    person_id: Annotated[int | None, Form()] = None,
    external_id: Annotated[str | None, Form()] = None,
    direction: Annotated[AttendanceDirection | None, Form()] = None,
    device_id: Annotated[str | None, Form(max_length=128)] = None,
    latitude: Annotated[float | None, Form(ge=-90, le=90)] = None,
    longitude: Annotated[float | None, Form(ge=-180, le=180)] = None,
    captured_at_utc: Annotated[datetime | None, Form()] = None,
) -> MarkAttendanceResponse:
    face_image = await to_face_image(image, settings)

    result = await attendance.mark(
        MarkAttendanceCommand(
            tenant_id=tenant,
            person_id=person_id,
            external_id=external_id,
            image=face_image,
            direction=direction,
            device_id=device_id,
            latitude=latitude,
            longitude=longitude,
            captured_at_utc=captured_at_utc,
        ),
        # Runner-up scores are useful when tuning but would tell a kiosk operator who else the
        # camera nearly matched, which is not theirs to know.
        include_diagnostics=settings.is_development,
    )

    return MarkAttendanceResponse.of(result)


@router.post(
    "/preview",
    response_model=PreviewMatchResponse,
    summary="Score a frame without recording anything",
    description=(
        "Use it for a live \"ready to punch\" indicator, and for measuring real score "
        "distributions before settling on a threshold."
    ),
)
async def preview(
    attendance: AttendanceServiceDep,
    tenant: TenantDep,
    settings: SettingsDep,
    _: AdminDep,
    image: Annotated[UploadFile, File()],
    person_id: Annotated[int | None, Form()] = None,
) -> PreviewMatchResponse:
    face_image = await to_face_image(image, settings)

    match = await attendance.preview_match(tenant, person_id, face_image)

    return PreviewMatchResponse(
        is_match=match.is_match,
        person_id=match.person_id,
        pose=FacePose(match.pose).name if match.pose is not None else None,
        score=round(match.score, 4),
        threshold=round(match.threshold, 4),
        margin=round(match.margin, 4) if match.margin is not None else None,
        top_scores=[
            ScoreResponse(
                person_id=s.person_id, pose=FacePose(s.pose).name, score=round(s.score, 4)
            )
            for s in match.top_scores
        ],
        analysis=AnalysisResponse.of(match.probe_analysis),
    )


@router.get(
    "",
    response_model=PagedResponse[AttendanceEventResponse],
    summary="The attendance register, including rejected attempts",
    description=(
        "Those failures are the audit trail you need when somebody says the machine did not "
        "recognise them."
    ),
)
async def query(
    repository: AttendanceRepoDep,
    tenant: TenantDep,
    _: AdminDep,
    person_id: int | None = None,
    from_utc: datetime | None = None,
    to_utc: datetime | None = None,
    decision: AttendanceDecision | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> PagedResponse[AttendanceEventResponse]:
    items, total = await repository.query(
        tenant, person_id, from_utc, to_utc, decision, page, page_size
    )

    return PagedResponse[AttendanceEventResponse](
        items=[AttendanceEventResponse.of(e) for e in items],
        page=page,
        page_size=page_size,
        total=total,
    )
