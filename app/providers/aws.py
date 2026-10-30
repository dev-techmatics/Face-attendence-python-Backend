"""AWS Rekognition provider, for teams who would rather pay per call than own a model.

Two things differ from the self-hosted path, and both have consequences:

1. The template stored in your database is an opaque Rekognition face id, not a vector. Two
   enrolments cannot be compared offline, so the duplicate-identity and pose-consistency
   checks become no-ops under this provider.
2. Every registration and every punch sends a face image to AWS, putting biometric data in a
   third-party processor. That is a data-protection decision as much as a technical one.

Rekognition reports similarity on a 0-100 scale; it is rescaled to 0-1 here so the thresholds
in configuration stay comparable between providers.

boto3 is synchronous, so each call is pushed to a worker thread.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Sequence
from typing import Any

from app.core.config import FaceRecognitionSettings
from app.core.enums import FacePose, FaceTemplateKind
from app.core.errors import FaceError, FaceProcessingException
from app.core.faces import (
    FaceBox,
    FaceCandidate,
    FaceCaptureAnalysis,
    FaceImage,
    FaceMatchResult,
    FaceScore,
    FaceTemplateMaterial,
    HeadPose,
)
from app.providers.base import FaceRecognitionProvider, build_match_result

logger = logging.getLogger(__name__)


class RekognitionFaceRecognitionProvider(FaceRecognitionProvider):
    def __init__(self, settings: FaceRecognitionSettings, client: Any | None = None) -> None:
        self._options = settings.aws

        if client is not None:
            self._rekognition = client
        else:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - depends on the optional extra
                raise RuntimeError(
                    "The face provider is 'aws' but boto3 is not installed. "
                    "Install it with: pip install -r requirements-aws.txt"
                ) from exc

            self._rekognition = boto3.client("rekognition", region_name=self._options.region)

        self._provider_id = f"aws:rekognition:{self._options.collection_id}"

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def template_kind(self) -> FaceTemplateKind:
        return FaceTemplateKind.ProviderManaged

    async def analyze(self, image: FaceImage) -> FaceCaptureAnalysis:
        width, height = _dimensions(image)

        response = await asyncio.to_thread(
            lambda: self._rekognition.detect_faces(
                Image={"Bytes": image.content}, Attributes=["DEFAULT"]
            )
        )

        details = response.get("FaceDetails", [])

        if not details:
            return FaceCaptureAnalysis(
                face_detected=False,
                face_count=0,
                image_width=width,
                image_height=height,
                warnings=["No face was detected in the frame."],
            )

        detail = max(
            details,
            key=lambda d: (d.get("BoundingBox", {}).get("Width", 0.0) or 0.0)
            * (d.get("BoundingBox", {}).get("Height", 0.0) or 0.0),
        )

        return _to_analysis(detail, len(details), width, height)

    async def create_template(self, image: FaceImage, pose: FacePose) -> FaceTemplateMaterial:
        width, height = _dimensions(image)

        response = await asyncio.to_thread(
            lambda: self._rekognition.index_faces(
                CollectionId=self._options.collection_id,
                Image={"Bytes": image.content},
                DetectionAttributes=["DEFAULT"],
                MaxFaces=1,
                QualityFilter="AUTO",
            )
        )

        records = response.get("FaceRecords", [])

        if not records:
            raise FaceProcessingException(
                FaceError.NoFaceDetected,
                "Rekognition indexed no face in this frame. It may be too small, blurred, or "
                "turned too far.",
            )

        record = records[0]
        detail = record.get("FaceDetail", {})
        confidence = float(detail.get("Confidence") or 0.0)

        return FaceTemplateMaterial(
            provider_id=self._provider_id,
            kind=FaceTemplateKind.ProviderManaged,
            dimension=0,
            # The face id IS the template. Lose this row and the indexed face is orphaned in
            # the collection: still billable, no longer reachable, and invisible to an erasure
            # request.
            data=str(record["Face"]["FaceId"]).encode("utf-8"),
            quality_score=confidence / 100.0,
            detection_score=confidence / 100.0,
            analysis=_to_analysis(detail, 1, width, height),
        )

    async def match(
        self,
        probe: FaceImage,
        candidates: Sequence[FaceCandidate],
        threshold: float,
        top_n: int = 5,
    ) -> FaceMatchResult:
        def _search() -> dict[str, Any]:
            return self._rekognition.search_faces_by_image(
                CollectionId=self._options.collection_id,
                Image={"Bytes": probe.content},
                FaceMatchThreshold=self._options.search_threshold_percent,
                MaxFaces=max(self._options.max_faces, top_n),
            )

        try:
            response = await asyncio.to_thread(_search)
        except Exception as exc:
            # Rekognition raises InvalidParameterException when it finds no face at all in
            # the probe.
            if type(exc).__name__ == "InvalidParameterException" or _error_code(exc) == (
                "InvalidParameterException"
            ):
                raise FaceProcessingException(
                    FaceError.NoFaceDetected,
                    "Rekognition found no usable face in the frame.",
                    inner=exc,
                ) from exc
            raise

        # Map provider face ids back to our people. This is what scopes a collection-wide
        # search to one tenant, or narrows it to one person for a 1:1 verification.
        by_face_id = {
            candidate.template_data.decode("utf-8", errors="replace"): candidate
            for candidate in candidates
        }

        scores: list[FaceScore] = []

        for match in response.get("FaceMatches", []):
            face_id = (match.get("Face") or {}).get("FaceId")
            candidate = by_face_id.get(face_id) if face_id else None

            if candidate is not None:
                scores.append(
                    FaceScore(
                        person_id=candidate.person_id,
                        template_id=candidate.template_id,
                        pose=candidate.pose,
                        score=float(match.get("Similarity") or 0.0) / 100.0,
                    )
                )

        return build_match_result(scores, threshold, top_n, analysis=None)

    def compare_templates(self, a: bytes, b: bytes) -> float | None:
        """Not supported: a face id carries no comparable information.

        Returning ``None`` makes the enrolment cross-checks skip, rather than silently
        producing a wrong number.
        """
        return None

    async def delete_templates(self, template_data: Sequence[bytes]) -> None:
        face_ids = [
            data.decode("utf-8", errors="replace")
            for data in template_data
            if data and len(data) > 0
        ]
        face_ids = [face_id for face_id in face_ids if face_id]

        if not face_ids:
            return

        # Deleting our row is not enough: the indexed face would stay in the collection, which
        # matters for billing and for honouring an erasure request.
        for start in range(0, len(face_ids), 1000):
            batch = face_ids[start : start + 1000]
            await asyncio.to_thread(
                lambda b=batch: self._rekognition.delete_faces(
                    CollectionId=self._options.collection_id, FaceIds=b
                )
            )

        logger.info(
            "Deleted %d faces from collection %s", len(face_ids), self._options.collection_id
        )

    async def ensure_collection(self) -> None:
        """Create the collection if this is a fresh account. Safe to call repeatedly."""

        def _create() -> None:
            try:
                self._rekognition.create_collection(
                    CollectionId=self._options.collection_id
                )
                logger.info(
                    "Created Rekognition collection %s", self._options.collection_id
                )
            except Exception as exc:
                if type(exc).__name__ == "ResourceAlreadyExistsException" or _error_code(
                    exc
                ) == "ResourceAlreadyExistsException":
                    # Already there, which is the normal case after the first boot.
                    return
                raise

        await asyncio.to_thread(_create)


def _error_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code"))
    return None


def _dimensions(image: FaceImage) -> tuple[int, int]:
    """Rekognition does not report image dimensions, and its bounding boxes are ratios.

    Decoding locally is the cheapest way to get pixels so the shared validation rules still
    apply.
    """
    try:
        from PIL import Image as PilImage

        with PilImage.open(io.BytesIO(image.content)) as bitmap:
            return bitmap.width, bitmap.height
    except Exception:
        return 0, 0


def _to_analysis(
    detail: dict[str, Any], face_count: int, image_width: int, image_height: int
) -> FaceCaptureAnalysis:
    quality = detail.get("Quality") or {}
    box = detail.get("BoundingBox")
    pose = detail.get("Pose")

    return FaceCaptureAnalysis(
        face_detected=True,
        face_count=face_count,
        detection_score=float(detail.get("Confidence") or 0.0) / 100.0,
        quality_score=float(quality.get("Sharpness") if quality.get("Sharpness") is not None else 50.0)
        / 100.0,
        # Ratios scaled to pixels so min_face_dimension means the same thing here as elsewhere.
        box=FaceBox(
            float(box.get("Left") or 0.0) * image_width,
            float(box.get("Top") or 0.0) * image_height,
            float(box.get("Width") or 0.0) * image_width,
            float(box.get("Height") or 0.0) * image_height,
        )
        if box
        else None,
        head_pose=HeadPose(
            float(pose.get("Yaw") or 0.0),
            float(pose.get("Pitch") or 0.0),
            float(pose.get("Roll") or 0.0),
        )
        if pose
        else None,
        sharpness=float(quality.get("Sharpness") or 0.0) / 100.0,
        brightness=float(quality.get("Brightness") or 0.0) / 100.0,
        image_width=image_width,
        image_height=image_height,
        warnings=[],
    )
