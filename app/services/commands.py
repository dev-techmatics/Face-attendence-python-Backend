"""Service inputs and outputs.

Plain dataclasses, deliberately separate from the HTTP schemas: the services must not care
what shape a request arrived in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.enums import (
    AttendanceDecision,
    AttendanceDirection,
    EnrollmentStatus,
    FacePose,
    PersonStatus,
)
from app.core.faces import FaceCaptureAnalysis, FaceImage, FaceScore


@dataclass(frozen=True)
class CreatePersonCommand:
    tenant_id: str
    external_id: str
    full_name: str
    email: str | None = None
    department: str | None = None


@dataclass(frozen=True)
class CaptureFaceCommand:
    """One pose upload. All three uploads for a person carry the same person id."""

    tenant_id: str
    person_id: int
    pose: FacePose
    image: FaceImage


@dataclass(frozen=True)
class CaptureFaceResult:
    person_id: int
    pose: FacePose
    template_id: int
    version: int
    image_object_key: str
    enrollment_status: EnrollmentStatus
    analysis: FaceCaptureAnalysis | None = None
    captured_poses: list[FacePose] = field(default_factory=list)
    missing_poses: list[FacePose] = field(default_factory=list)
    #: True on the upload that completed the set and flipped the person to Enrolled.
    enrollment_completed: bool = False
    #: Replaced an earlier capture of the same pose.
    replaced_previous: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CapturedPoseInfo:
    pose: FacePose
    template_id: int
    version: int
    quality_score: float
    detection_score: float
    provider_id: str
    provider_matches_current: bool
    captured_at_utc: datetime


@dataclass(frozen=True)
class PoseSimilarity:
    a: FacePose
    b: FacePose
    similarity: float


@dataclass(frozen=True)
class EnrollmentSummary:
    person_id: int
    external_id: str
    full_name: str
    status: PersonStatus
    enrollment_status: EnrollmentStatus
    enrolled_at_utc: datetime | None = None
    poses: list[CapturedPoseInfo] = field(default_factory=list)
    missing_poses: list[FacePose] = field(default_factory=list)
    #: Pairwise similarity between the stored poses. Empty for provider-managed templates.
    pose_similarities: list[PoseSimilarity] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RegistrationCheck:
    """Answer to "is this face already on file?", asked before anybody fills in a form.

    ``registered`` is the answer; the dataclass existing at all is the point. A caller must
    be able to tell "we checked, and no" from "we could not check", because treating a
    failed check as a clear face is how a second record for the same person gets created -
    which is the thing the check exists to prevent. A failure therefore raises rather than
    returning ``registered=False``.

    ``status`` and ``can_mark_attendance`` are here because "already registered" has two
    very different endings: an active colleague should just clock in, while somebody whose
    record was removed needs an admin to put it back and must not be told to register again.
    """

    registered: bool
    score: float
    threshold: float
    #: True when the match came from comparing against one claimed person rather than
    #: searching everybody. The two use different thresholds, so this says which was applied.
    one_to_one: bool = False
    person_id: int | None = None
    external_id: str | None = None
    full_name: str | None = None
    status: PersonStatus | None = None
    enrollment_status: EnrollmentStatus | None = None
    can_mark_attendance: bool = False


@dataclass(frozen=True)
class MarkAttendanceCommand:
    tenant_id: str
    image: FaceImage

    #: Supply person_id or external_id for a 1:1 verification (fast, and much safer). Leave
    #: both None for a 1:N identification across the tenant.
    person_id: int | None = None
    external_id: str | None = None

    #: None lets the server infer In/Out from the previous event.
    direction: AttendanceDirection | None = None

    device_id: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    captured_at_utc: datetime | None = None


@dataclass(frozen=True)
class MarkAttendanceResult:
    success: bool
    decision: AttendanceDecision
    event_id: int
    score: float
    threshold: float
    recorded_at_utc: datetime
    processing_ms: int
    person_id: int | None = None
    external_id: str | None = None
    full_name: str | None = None
    direction: AttendanceDirection | None = None
    matched_pose: FacePose | None = None
    message: str | None = None
    #: Runner-up scores. Returned only when diagnostics are enabled.
    top_scores: list[FaceScore] = field(default_factory=list)
