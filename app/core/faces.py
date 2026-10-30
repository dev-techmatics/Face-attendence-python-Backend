"""Value objects the face pipeline passes around.

Deliberately free of provider-specific concepts; that is what makes swapping the recognition
engine cheap.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.core.enums import FacePose, FaceTemplateKind


@dataclass(frozen=True)
class FaceBox:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)


@dataclass(frozen=True)
class FaceLandmarks:
    """The five landmarks ArcFace-style models align on.

    Both eyes, nose tip, both mouth corners, each an ``(x, y)`` pair.
    """

    left_eye: tuple[float, float]
    right_eye: tuple[float, float]
    nose: tuple[float, float]
    mouth_left: tuple[float, float]
    mouth_right: tuple[float, float]

    def as_tuple(self) -> tuple[tuple[float, float], ...]:
        return (self.left_eye, self.right_eye, self.nose, self.mouth_left, self.mouth_right)


@dataclass(frozen=True)
class HeadPose:
    """Head orientation estimate, in degrees. Yaw is the left/right turn."""

    yaw_degrees: float
    pitch_degrees: float
    roll_degrees: float


class FaceImage:
    """An uploaded image plus the metadata the pipeline needs about it."""

    __slots__ = ("content", "content_type", "file_name", "sha256")

    def __init__(self, content: bytes, content_type: str, file_name: str | None = None) -> None:
        if content is None:
            raise ValueError("content must not be None.")

        self.content = content
        self.content_type = (
            "application/octet-stream" if not content_type or not content_type.strip()
            else content_type.strip().lower()
        )
        self.file_name = file_name
        #: Lowercase hex SHA-256 of :attr:`content`.
        self.sha256 = hashlib.sha256(content).hexdigest()

    @property
    def size_bytes(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class FaceCaptureAnalysis:
    """What the provider saw in a frame, before any decision is taken.

    Returned to the capture screen so the front end can coach the user ("move closer",
    "turn further left").
    """

    face_detected: bool = False
    face_count: int = 0
    detection_score: float = 0.0
    #: 0..1 composite of sharpness, exposure and face size.
    quality_score: float = 0.0
    box: FaceBox | None = None
    landmarks: FaceLandmarks | None = None
    head_pose: HeadPose | None = None
    #: Pose bucket inferred from yaw, when the provider can estimate it.
    inferred_pose: FacePose | None = None
    sharpness: float = 0.0
    brightness: float = 0.0
    image_width: int = 0
    image_height: int = 0
    #: Non-fatal observations worth surfacing to the operator.
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FaceCandidate:
    """One stored template to compare a probe frame against."""

    person_id: int
    template_id: int
    pose: FacePose
    template_data: bytes
    #: Needed by providers that compare images rather than vectors.
    image_object_key: str | None = None


@dataclass(frozen=True)
class FaceScore:
    person_id: int
    template_id: int
    pose: FacePose
    score: float


@dataclass(frozen=True)
class FaceMatchResult:
    is_match: bool = False
    person_id: int | None = None
    template_id: int | None = None
    pose: FacePose | None = None
    #: Best score seen, whether or not it cleared the threshold.
    score: float = 0.0
    threshold: float = 0.0
    #: Best scores per person, descending. Useful for tuning and for audit.
    top_scores: list[FaceScore] = field(default_factory=list)
    probe_analysis: FaceCaptureAnalysis | None = None
    #: Gap between the best and second-best *person*. A tiny margin on a 1:N search means two
    #: enrolled people look alike to the model; treat it as inconclusive rather than a match.
    margin: float | None = None

    @staticmethod
    def no_match(
        threshold: float,
        best_score: float = 0.0,
        top: list[FaceScore] | None = None,
        analysis: FaceCaptureAnalysis | None = None,
    ) -> "FaceMatchResult":
        return FaceMatchResult(
            is_match=False,
            score=best_score,
            threshold=threshold,
            top_scores=list(top or []),
            probe_analysis=analysis,
        )


@dataclass(frozen=True)
class FaceTemplateMaterial:
    """The biometric payload a provider produced for one capture."""

    provider_id: str
    kind: FaceTemplateKind
    data: bytes
    #: Embedding length, or 0 when :attr:`kind` is provider-managed.
    dimension: int = 0
    quality_score: float = 0.0
    detection_score: float = 0.0
    analysis: FaceCaptureAnalysis | None = None
