"""A deterministic stand-in that performs no recognition whatsoever.

It exists so the API, the database schema and the front-end capture flow can be built and
tested before anyone downloads a model file.

**It identifies files, not faces.** The embedding is derived from the upload file name, up to
the first dash, dot or underscore: ``alice-front.jpg``, ``alice-left.jpg`` and
``alice-right.jpg`` all produce near-identical vectors, so they enrol as one person and verify
against each other. ``bob-front.jpg`` will not match alice. Rename the files and the identity
changes; the pixels are irrelevant.

Selecting this provider outside development is refused at startup. Treat any attendance row it
produces as test data, never as evidence that somebody was present.
"""

from __future__ import annotations

import hashlib
import logging
import struct
from collections.abc import Sequence
from pathlib import PurePath

import numpy as np
from numpy.typing import NDArray

from app.core import vector_math
from app.core.config import FaceRecognitionSettings
from app.core.enums import FacePose, FaceTemplateKind
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

EMBEDDING_DIMENSION = 512


class DevelopmentFaceRecognitionProvider(FaceRecognitionProvider):
    def __init__(self, settings: FaceRecognitionSettings) -> None:
        self._settings = settings

        logger.warning(
            "FACE RECOGNITION IS STUBBED. The development provider matches on upload file "
            "name, not on faces. Set the face provider to onnx or aws before any real use."
        )

    @property
    def provider_id(self) -> str:
        return "development:filename-stub:v1"

    @property
    def template_kind(self) -> FaceTemplateKind:
        return FaceTemplateKind.Embedding

    async def analyze(self, image: FaceImage) -> FaceCaptureAnalysis:
        return self._build_analysis(None)

    async def create_template(self, image: FaceImage, pose: FacePose) -> FaceTemplateMaterial:
        analysis = self._build_analysis(pose)
        embedding = _build_embedding(_identity_seed_of(image), pose)

        return FaceTemplateMaterial(
            provider_id=self.provider_id,
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
        analysis = self._build_analysis(None)
        seed = _identity_seed_of(probe)
        scores: list[FaceScore] = []

        # Score against each candidate using that candidate's pose variant, mirroring how the
        # real provider treats three templates as three chances to match one person.
        for candidate in candidates:
            stored = vector_math.from_bytes(candidate.template_data)
            probe_embedding = _build_embedding(seed, candidate.pose)

            if stored.size != probe_embedding.size:
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

    def compare_templates(self, a: bytes, b: bytes) -> float | None:
        return vector_math.cosine_similarity(
            vector_math.from_bytes(a), vector_math.from_bytes(b)
        )

    def _build_analysis(self, pose: FacePose | None) -> FaceCaptureAnalysis:
        """Reports a healthy, correctly-posed face for any upload.

        Validation gates must not block a flow test, so quality and dimensions are pinned
        above the configured floors on purpose.
        """
        if pose == FacePose.Left:
            yaw = self._settings.min_profile_yaw_degrees + 10.0
        elif pose == FacePose.Right:
            yaw = -(self._settings.min_profile_yaw_degrees + 10.0)
        else:
            yaw = 0.0

        return FaceCaptureAnalysis(
            face_detected=True,
            face_count=1,
            detection_score=0.99,
            quality_score=max(0.9, self._settings.min_quality_score),
            box=FaceBox(100.0, 100.0, 200.0, 200.0),
            head_pose=HeadPose(yaw, 0.0, 0.0),
            inferred_pose=pose or FacePose.Front,
            sharpness=0.9,
            brightness=0.5,
            image_width=max(640, self._settings.min_image_dimension),
            image_height=max(640, self._settings.min_image_dimension),
            warnings=["Development provider: no real face analysis was performed."],
        )


def _identity_seed_of(image: FaceImage) -> str:
    """Identity key for the fake: the file name up to the first dash, dot or underscore.

    Falls back to the content hash, which makes each distinct upload its own person.
    """
    if not image.file_name or not image.file_name.strip():
        return image.sha256

    name = PurePath(image.file_name.replace("\\", "/")).name
    cuts = [name.find(c) for c in ("-", ".", "_")]
    positions = [c for c in cuts if c > 0]
    cut = min(positions) if positions else -1

    return (name[:cut] if cut > 0 else name).lower()


def _build_embedding(seed: str, pose: FacePose) -> NDArray[np.float32]:
    """Deterministic unit vector for an identity, nudged per pose.

    The three captures end up similar but not identical: close enough to pass the consistency
    check, different enough to exercise per-pose storage.
    """
    vector = _fill(seed)
    jitter = _fill(f"{seed}:{pose.name}")

    vector += np.float32(0.12) * jitter
    return vector_math.l2_normalize(vector)


def _fill(seed: str) -> NDArray[np.float32]:
    """Expand a seed into a unit vector by chaining SHA-256 blocks."""
    vector = np.zeros(EMBEDDING_DIMENSION, dtype=np.float32)
    source = seed.encode("utf-8")
    written = 0
    counter = 0

    while written < EMBEDDING_DIMENSION:
        block = hashlib.sha256(source + struct.pack("<i", counter)).digest()
        counter += 1

        for i in range(0, len(block) - 1, 2):
            if written >= EMBEDDING_DIMENSION:
                break
            # int16 little-endian, scaled to roughly [-1, 1], exactly as the original did.
            vector[written] = struct.unpack_from("<h", block, i)[0] / 32767.0
            written += 1

    return vector_math.l2_normalize(vector)
