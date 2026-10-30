"""HTTP request and response shapes.

Enums serialise as names: "Front" survives a renumbering in a way that 1 does not, and it
makes the API readable without a lookup table.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field

from app.core.enums import (
    AttendanceDecision,
    AttendanceDirection,
    EnrollmentStatus,
    FacePose,
    FaceTemplateKind,
    PersonStatus,
)
from app.core.faces import FaceCaptureAnalysis
from app.domain.models import AttendanceEvent, Person
from app.services.commands import (
    CaptureFaceResult,
    EnrollmentSummary,
    MarkAttendanceResult,
)

T = TypeVar("T")


def _name(value) -> str | None:
    """Serialise an IntEnum as its name, leaving None alone."""
    return None if value is None else value.name if hasattr(value, "name") else str(value)


class ApiModel(BaseModel):
    model_config = ConfigDict(use_enum_values=False, from_attributes=True)


# ---- Requests ----------------------------------------------------------------------------


class CreatePersonRequest(ApiModel):
    #: The id your own system already uses: employee code, roll number, badge number.
    external_id: str = Field(min_length=1, max_length=64)
    full_name: str = Field(min_length=1, max_length=200)
    email: EmailStr | None = Field(default=None, max_length=256)
    department: str | None = Field(default=None, max_length=128)


# ---- Responses ---------------------------------------------------------------------------


class PagedResponse(ApiModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_pages(self) -> int:
        return 0 if self.page_size <= 0 else math.ceil(self.total / self.page_size)


class PersonResponse(ApiModel):
    id: int
    external_id: str
    full_name: str
    email: str | None
    department: str | None
    status: str
    enrollment_status: str
    enrolled_at_utc: datetime | None
    created_at_utc: datetime

    @staticmethod
    def of(person: Person) -> "PersonResponse":
        return PersonResponse(
            id=person.id,
            external_id=person.external_id,
            full_name=person.full_name,
            email=person.email,
            department=person.department,
            status=PersonStatus(person.status).name,
            enrollment_status=EnrollmentStatus(person.enrollment_status).name,
            enrolled_at_utc=person.enrolled_at_utc,
            created_at_utc=person.created_at_utc,
        )


class BoxResponse(ApiModel):
    x: float
    y: float
    width: float
    height: float


class AnalysisResponse(ApiModel):
    """What the detector saw. Drives the coaching text on the capture screen."""

    face_detected: bool
    face_count: int
    detection_score: float
    quality_score: float
    sharpness: float
    brightness: float
    image_width: int
    image_height: int
    yaw_degrees: float | None = None
    pitch_degrees: float | None = None
    roll_degrees: float | None = None
    inferred_pose: str | None = None
    box: BoxResponse | None = None
    warnings: list[str] = Field(default_factory=list)

    @staticmethod
    def of(analysis: FaceCaptureAnalysis | None) -> "AnalysisResponse | None":
        if analysis is None:
            return None

        head = analysis.head_pose

        return AnalysisResponse(
            face_detected=analysis.face_detected,
            face_count=analysis.face_count,
            detection_score=round(analysis.detection_score, 4),
            quality_score=round(analysis.quality_score, 4),
            sharpness=round(analysis.sharpness, 4),
            brightness=round(analysis.brightness, 4),
            image_width=analysis.image_width,
            image_height=analysis.image_height,
            yaw_degrees=round(head.yaw_degrees, 2) if head else None,
            pitch_degrees=round(head.pitch_degrees, 2) if head else None,
            roll_degrees=round(head.roll_degrees, 2) if head else None,
            inferred_pose=_name(analysis.inferred_pose),
            box=BoxResponse(
                x=analysis.box.x,
                y=analysis.box.y,
                width=analysis.box.width,
                height=analysis.box.height,
            )
            if analysis.box
            else None,
            warnings=list(analysis.warnings),
        )


class CaptureFaceResponse(ApiModel):
    person_id: int
    pose: str
    template_id: int
    version: int
    replaced_previous: bool
    enrollment_status: str
    captured_poses: list[str]
    missing_poses: list[str]
    enrollment_completed: bool
    analysis: AnalysisResponse | None = None
    warnings: list[str] = Field(default_factory=list)

    @staticmethod
    def of(result: CaptureFaceResult) -> "CaptureFaceResponse":
        return CaptureFaceResponse(
            person_id=result.person_id,
            pose=FacePose(result.pose).name,
            template_id=result.template_id,
            version=result.version,
            replaced_previous=result.replaced_previous,
            enrollment_status=EnrollmentStatus(result.enrollment_status).name,
            captured_poses=[FacePose(p).name for p in result.captured_poses],
            missing_poses=[FacePose(p).name for p in result.missing_poses],
            enrollment_completed=result.enrollment_completed,
            analysis=AnalysisResponse.of(result.analysis),
            warnings=list(result.warnings),
        )


class PoseResponse(ApiModel):
    pose: str
    template_id: int
    version: int
    quality_score: float
    detection_score: float
    provider_id: str
    provider_matches_current: bool
    captured_at_utc: datetime


class PoseSimilarityResponse(ApiModel):
    a: str
    b: str
    similarity: float


class EnrollmentResponse(ApiModel):
    person_id: int
    external_id: str
    full_name: str
    status: str
    enrollment_status: str
    enrolled_at_utc: datetime | None
    poses: list[PoseResponse]
    missing_poses: list[str]
    pose_similarities: list[PoseSimilarityResponse]
    warnings: list[str] = Field(default_factory=list)

    @staticmethod
    def of(summary: EnrollmentSummary) -> "EnrollmentResponse":
        return EnrollmentResponse(
            person_id=summary.person_id,
            external_id=summary.external_id,
            full_name=summary.full_name,
            status=PersonStatus(summary.status).name,
            enrollment_status=EnrollmentStatus(summary.enrollment_status).name,
            enrolled_at_utc=summary.enrolled_at_utc,
            poses=[
                PoseResponse(
                    pose=FacePose(p.pose).name,
                    template_id=p.template_id,
                    version=p.version,
                    quality_score=round(p.quality_score, 4),
                    detection_score=round(p.detection_score, 4),
                    provider_id=p.provider_id,
                    provider_matches_current=p.provider_matches_current,
                    captured_at_utc=p.captured_at_utc,
                )
                for p in summary.poses
            ],
            missing_poses=[FacePose(p).name for p in summary.missing_poses],
            pose_similarities=[
                PoseSimilarityResponse(
                    a=FacePose(s.a).name,
                    b=FacePose(s.b).name,
                    similarity=round(s.similarity, 4),
                )
                for s in summary.pose_similarities
            ],
            warnings=list(summary.warnings),
        )


class RegisterPersonResponse(ApiModel):
    person: PersonResponse
    captures: list[CaptureFaceResponse]
    enrollment_status: str
    enrollment_completed: bool


class ScoreResponse(ApiModel):
    person_id: int
    pose: str
    score: float


class MarkAttendanceResponse(ApiModel):
    success: bool
    decision: str
    event_id: int
    person_id: int | None = None
    external_id: str | None = None
    full_name: str | None = None
    direction: str | None = None
    score: float
    threshold: float
    matched_pose: str | None = None
    recorded_at_utc: datetime
    message: str | None = None
    processing_ms: int
    top_scores: list[ScoreResponse] = Field(default_factory=list)

    @staticmethod
    def of(result: MarkAttendanceResult) -> "MarkAttendanceResponse":
        return MarkAttendanceResponse(
            success=result.success,
            decision=AttendanceDecision(result.decision).name,
            event_id=result.event_id,
            person_id=result.person_id,
            external_id=result.external_id,
            full_name=result.full_name,
            direction=_name(result.direction),
            score=round(result.score, 4),
            threshold=round(result.threshold, 4),
            matched_pose=_name(result.matched_pose),
            recorded_at_utc=result.recorded_at_utc,
            message=result.message,
            processing_ms=result.processing_ms,
            top_scores=[
                ScoreResponse(
                    person_id=s.person_id, pose=FacePose(s.pose).name, score=round(s.score, 4)
                )
                for s in result.top_scores
            ],
        )


class AttendanceEventResponse(ApiModel):
    id: int
    person_id: int | None
    external_id: str | None
    full_name: str | None
    direction: str
    decision: str
    match_score: float
    threshold: float
    matched_pose: str | None
    device_id: str | None
    failure_reason: str | None
    created_at_utc: datetime
    processing_ms: int

    @staticmethod
    def of(event: AttendanceEvent) -> "AttendanceEventResponse":
        return AttendanceEventResponse(
            id=event.id,
            person_id=event.person_id,
            external_id=event.person.external_id if event.person else None,
            full_name=event.person.full_name if event.person else None,
            direction=AttendanceDirection(event.direction).name,
            decision=AttendanceDecision(event.decision).name,
            match_score=round(event.match_score, 4),
            threshold=round(event.threshold, 4),
            matched_pose=FacePose(event.matched_pose).name
            if event.matched_pose is not None
            else None,
            device_id=event.device_id,
            failure_reason=event.failure_reason,
            created_at_utc=event.created_at_utc,
            processing_ms=event.processing_ms,
        )


class PreviewMatchResponse(ApiModel):
    is_match: bool
    person_id: int | None
    pose: str | None
    score: float
    threshold: float
    margin: float | None
    top_scores: list[ScoreResponse]
    analysis: AnalysisResponse | None


class ProviderInfoResponse(ApiModel):
    provider_id: str
    template_kind: str
    can_compare_templates_offline: bool
    required_poses: list[str]
    verification_threshold: float
    identification_threshold: float
    identification_margin: float
    duplicate_enrollment_threshold: float
    min_quality_score: float
    min_detection_score: float
    min_image_dimension: int
    min_face_dimension: int
    pose_check_enabled: bool
    store_probe_images: bool
    duplicate_punch_window_minutes: int


__all__ = [
    "AnalysisResponse",
    "ApiModel",
    "AttendanceEventResponse",
    "BoxResponse",
    "CaptureFaceResponse",
    "CreatePersonRequest",
    "EnrollmentResponse",
    "FaceTemplateKind",
    "MarkAttendanceResponse",
    "PagedResponse",
    "PersonResponse",
    "PoseResponse",
    "PoseSimilarityResponse",
    "PreviewMatchResponse",
    "ProviderInfoResponse",
    "RegisterPersonResponse",
    "ScoreResponse",
]
