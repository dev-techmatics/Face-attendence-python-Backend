"""Self-hosted recognition: SCRFD for detection and landmarks, ArcFace for the embedding,
cosine similarity for the decision.

No cloud account, no per-call charge, and no face image leaving the building.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from app.core import vector_math
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
)
from app.providers.base import FaceRecognitionProvider, build_match_result
from app.providers.onnx import aligner, image_ops
from app.providers.onnx.arcface import ArcFaceEmbedder
from app.providers.onnx.scrfd import DetectedFace, ScrfdFaceDetector

logger = logging.getLogger(__name__)

#: Side of the square crop the quality heuristics are measured on.
_QUALITY_CROP_SIZE = 160


class OnnxFaceRecognitionProvider(FaceRecognitionProvider):
    def __init__(self, settings: FaceRecognitionSettings) -> None:
        self._root = settings
        self._settings = settings.onnx
        self._detector = ScrfdFaceDetector(self._settings)
        self._embedder = ArcFaceEmbedder(self._settings)

        # Inference is CPU-bound. Without a cap, a shift-change rush would spawn as many
        # parallel inferences as there are requests and starve the whole process.
        self._concurrency = asyncio.Semaphore(max(1, self._settings.max_concurrency))

        self._provider_id = (
            f"onnx:scrfd+arcface:{self._settings.model_version}:d{self._embedder.dimension}"
        )

        logger.info(
            "Local face recognition ready: %s, %d-d embeddings, max %d concurrent inferences",
            self._provider_id,
            self._embedder.dimension,
            self._settings.max_concurrency,
        )

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def template_kind(self) -> FaceTemplateKind:
        return FaceTemplateKind.Embedding

    async def analyze(self, image: FaceImage) -> FaceCaptureAnalysis:
        async with self._concurrency:
            analysis, _ = await asyncio.to_thread(self._analyze_bytes, image.content)
            return analysis

    async def create_template(self, image: FaceImage, pose: FacePose) -> FaceTemplateMaterial:
        async with self._concurrency:
            analysis, embedding = await asyncio.to_thread(self._embed_bytes, image.content)

        return FaceTemplateMaterial(
            provider_id=self._provider_id,
            kind=FaceTemplateKind.Embedding,
            dimension=int(embedding.size),
            data=vector_math.to_bytes(embedding),
            quality_score=analysis.quality_score,
            detection_score=analysis.detection_score,
            analysis=analysis,
        )

    async def match(
        self,
        probe: FaceImage,
        candidates: Sequence[FaceCandidate],
        threshold: float,
        top_n: int = 5,
    ) -> FaceMatchResult:
        async with self._concurrency:
            analysis, probe_embedding = await asyncio.to_thread(self._embed_bytes, probe.content)

        return _score_candidates(probe_embedding, candidates, threshold, top_n, analysis)

    def compare_templates(self, a: bytes, b: bytes) -> float | None:
        return vector_math.cosine_similarity(
            vector_math.from_bytes(a), vector_math.from_bytes(b)
        )

    async def delete_templates(self, template_data: Sequence[bytes]) -> None:
        """Nothing to release: embeddings live only in your database."""
        return None

    def close(self) -> None:
        self._detector = None  # type: ignore[assignment]
        self._embedder = None  # type: ignore[assignment]

    # ---- Synchronous inner pipeline, run on a worker thread ------------------------------

    def _embed_bytes(self, content: bytes) -> tuple[FaceCaptureAnalysis, NDArray[np.float32]]:
        with image_ops.decode(content) as bitmap:
            analysis, face = self._analyze(bitmap)

            if face is None:
                raise FaceProcessingException(
                    FaceError.NoFaceDetected, "No face was found in the frame."
                )

            transform = aligner.compute_transform(face.landmarks)
            aligned = image_ops.warp_aligned(bitmap, transform, aligner.OUTPUT_SIZE)

        return analysis, self._embedder.embed(aligned)

    def _analyze_bytes(
        self, content: bytes
    ) -> tuple[FaceCaptureAnalysis, DetectedFace | None]:
        with image_ops.decode(content) as bitmap:
            return self._analyze(bitmap)

    def _analyze(self, bitmap: Image.Image) -> tuple[FaceCaptureAnalysis, DetectedFace | None]:
        """Detect, pick the subject, and score the frame.

        When several faces are present the largest wins, on the assumption that the person at
        the kiosk is nearer than passers-by.
        """
        faces = self._detector.detect(bitmap)

        if not faces:
            return (
                FaceCaptureAnalysis(
                    face_detected=False,
                    face_count=0,
                    image_width=bitmap.width,
                    image_height=bitmap.height,
                    warnings=["No face was detected in the frame."],
                ),
                None,
            )

        primary = max(faces, key=lambda f: f.box.area)
        warnings: list[str] = []

        if len(faces) > 1:
            warnings.append(f"{len(faces)} faces were detected; the largest was used.")

        crop = image_ops.crop(
            bitmap, _clamp_to_image(primary.box, bitmap), _QUALITY_CROP_SIZE
        )

        laplacian_variance = image_ops.laplacian_variance(crop)
        brightness = image_ops.mean_brightness(crop)

        # Heuristic normalisations. The divisors are empirical and worth re-tuning against
        # your own cameras; what matters is that they are monotonic and stable.
        sharpness = min(1.0, max(0.0, laplacian_variance / 500.0))
        exposure = 1.0 - min(1.0, max(0.0, abs(brightness - 0.5) * 2.0))
        face_size = min(1.0, max(0.0, min(primary.box.width, primary.box.height) / 200.0))
        quality = (0.4 * sharpness) + (0.3 * exposure) + (0.3 * face_size)

        if sharpness < 0.3:
            warnings.append("The frame looks soft or motion-blurred.")

        if brightness < 0.25:
            warnings.append("The frame is underexposed.")
        elif brightness > 0.85:
            warnings.append("The frame is overexposed.")

        head = aligner.estimate_head_pose(primary.landmarks, self._settings.mirrored_input)

        if head.yaw_degrees >= self._root.min_profile_yaw_degrees:
            inferred = FacePose.Left
        elif head.yaw_degrees <= -self._root.min_profile_yaw_degrees:
            inferred = FacePose.Right
        else:
            inferred = FacePose.Front

        analysis = FaceCaptureAnalysis(
            face_detected=True,
            face_count=len(faces),
            detection_score=primary.score,
            quality_score=quality,
            box=primary.box,
            landmarks=primary.landmarks,
            head_pose=head,
            inferred_pose=inferred,
            sharpness=sharpness,
            brightness=brightness,
            image_width=bitmap.width,
            image_height=bitmap.height,
            warnings=warnings,
        )

        return analysis, primary


def _score_candidates(
    probe_embedding: NDArray[np.float32],
    candidates: Sequence[FaceCandidate],
    threshold: float,
    top_n: int,
    analysis: FaceCaptureAnalysis,
) -> FaceMatchResult:
    """Pure arithmetic, no model involved.

    This is the step that would otherwise be a paid API call, and it is why the whole thing
    works against an ordinary relational database.
    """
    scores: list[FaceScore] = []

    for candidate in candidates:
        stored = vector_math.from_bytes(candidate.template_data)

        if stored.size != probe_embedding.size:
            # A template from a different model. Skipped rather than thrown, so one stale row
            # cannot take down attendance for everybody.
            continue

        scores.append(
            FaceScore(
                person_id=candidate.person_id,
                template_id=candidate.template_id,
                pose=candidate.pose,
                score=vector_math.cosine_similarity(probe_embedding, stored),
            )
        )

    return build_match_result(scores, threshold, top_n, analysis)


def _clamp_to_image(box: FaceBox, bitmap: Image.Image) -> FaceBox:
    """Keep a box inside the image.

    Anchor decoding can place an edge slightly outside the frame for a face at the border,
    which would make the crop fail.
    """
    x = min(max(box.x, 0.0), bitmap.width - 1.0)
    y = min(max(box.y, 0.0), bitmap.height - 1.0)
    width = min(max(box.width, 1.0), bitmap.width - x)
    height = min(max(box.height, 1.0), bitmap.height - y)

    return FaceBox(x, y, width, height)
