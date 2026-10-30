"""Introspection for operators.

Which engine is live, what the thresholds are, and what the detector makes of a given frame.
Invaluable when someone reports that recognition is wrong.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, UploadFile

from app.api.deps import AdminDep, ProviderDep, SettingsDep, ValidatorDep, to_face_image
from app.api.schemas import AnalysisResponse, ProviderInfoResponse
from app.core.enums import FacePose, FaceTemplateKind

router = APIRouter(prefix="/api/v1/diagnostics", tags=["diagnostics"])


@router.get(
    "/provider",
    response_model=ProviderInfoResponse,
    summary="The active engine and the numbers it decides by",
    description=(
        "Worth logging at deploy time: a changed provider id means every existing enrolment "
        "needs re-capturing."
    ),
)
async def get_provider(
    provider: ProviderDep,
    settings: SettingsDep,
    _: AdminDep,
) -> ProviderInfoResponse:
    face = settings.face

    return ProviderInfoResponse(
        provider_id=provider.provider_id,
        template_kind=FaceTemplateKind(provider.template_kind).name,
        can_compare_templates_offline=provider.compare_templates(bytes(4), bytes(4))
        is not None,
        required_poses=[FacePose(p).name for p in face.required_poses],
        verification_threshold=face.verification_threshold,
        identification_threshold=face.identification_threshold,
        identification_margin=face.identification_margin,
        duplicate_enrollment_threshold=face.duplicate_enrollment_threshold,
        min_quality_score=face.min_quality_score,
        min_detection_score=face.min_detection_score,
        min_image_dimension=face.min_image_dimension,
        min_face_dimension=face.min_face_dimension,
        pose_check_enabled=face.enforce_pose_check,
        store_probe_images=face.store_probe_images,
        # Exposed so an operator can tell a collapsed punch from a rejected one without
        # reading the config file off the server.
        duplicate_punch_window_minutes=face.duplicate_punch_window_minutes,
    )


@router.post(
    "/analyze",
    response_model=AnalysisResponse,
    summary="Run detection and quality scoring on a frame without storing anything",
    description="Use it to check camera placement and lighting before rolling a kiosk out.",
)
async def analyze(
    provider: ProviderDep,
    validator: ValidatorDep,
    settings: SettingsDep,
    _: AdminDep,
    image: Annotated[UploadFile, File()],
) -> AnalysisResponse:
    face_image = await to_face_image(image, settings)

    # The same upload gate the enrolment and attendance paths use. Without it this endpoint
    # would hand a text file to the provider and report a cheerful "face detected".
    validator.validate_upload(face_image)

    analysis = await provider.analyze(face_image)

    result = AnalysisResponse.of(analysis)
    assert result is not None  # analyze never returns None
    return result
