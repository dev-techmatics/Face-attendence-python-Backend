"""The single seam between the application and whatever actually recognises faces.

Implemented three ways in this project:

* :class:`~app.providers.onnx.provider.OnnxFaceRecognitionProvider` - self-hosted SCRFD +
  ArcFace, embeddings in your own database. No cloud account, no per-call cost.
* :class:`~app.providers.aws.RekognitionFaceRecognitionProvider` - AWS Rekognition, for teams
  that would rather pay per call than own a model.
* :class:`~app.providers.development.DevelopmentFaceRecognitionProvider` - deterministic fake
  so the API, database and front end can be built and tested before any model file exists.

Keep this interface free of provider-specific concepts; that is what makes the swap cheap.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence

from app.core.enums import FacePose, FaceTemplateKind
from app.core.faces import (
    FaceCandidate,
    FaceCaptureAnalysis,
    FaceImage,
    FaceMatchResult,
    FaceScore,
    FaceTemplateMaterial,
)


class FaceRecognitionProvider(abc.ABC):
    @property
    @abc.abstractmethod
    def provider_id(self) -> str:
        """Stable identity of engine + model version, e.g. ``onnx:scrfd+arcface:v1:d512``.

        Persisted on every template; a change invalidates existing enrolments.
        """

    @property
    @abc.abstractmethod
    def template_kind(self) -> FaceTemplateKind: ...

    @abc.abstractmethod
    async def analyze(self, image: FaceImage) -> FaceCaptureAnalysis:
        """Detect and score a frame without enrolling it. Drives capture-screen feedback."""

    @abc.abstractmethod
    async def create_template(self, image: FaceImage, pose: FacePose) -> FaceTemplateMaterial:
        """Turn a frame into a storable template.

        Raises :class:`~app.core.errors.FaceProcessingException` when the frame is not usable.
        """

    @abc.abstractmethod
    async def match(
        self,
        probe: FaceImage,
        candidates: Sequence[FaceCandidate],
        threshold: float,
        top_n: int = 5,
    ) -> FaceMatchResult:
        """Score a probe frame against a candidate set.

        A single-person candidate set is a 1:1 verification; the whole enrolled population is
        a 1:N identification.
        """

    @abc.abstractmethod
    def compare_templates(self, a: bytes, b: bytes) -> float | None:
        """Compare two stored templates.

        Used to cross-check that the three registration poses belong to the same face, and to
        detect somebody enrolling twice.

        :return: Similarity, or ``None`` when the provider cannot compare stored templates
            offline.
        """

    async def delete_templates(self, template_data: Sequence[bytes]) -> None:
        """Release provider-side resources.

        For example, delete faces from a Rekognition collection. Nothing to do for providers
        whose templates live only in your own database.
        """
        return None

    def close(self) -> None:
        """Release local resources (inference sessions, thread pools)."""
        return None


def best_per_person(scores: list[FaceScore]) -> list[FaceScore]:
    """Collapse per-template scores to one score per person, best first.

    A person is represented by three poses; their score is the best of the three, since a
    profile probe should be allowed to match the profile template and ignore the others.
    """
    best: dict[int, FaceScore] = {}

    for score in scores:
        current = best.get(score.person_id)
        if current is None or score.score > current.score:
            best[score.person_id] = score

    return sorted(best.values(), key=lambda s: s.score, reverse=True)


def build_match_result(
    scores: list[FaceScore],
    threshold: float,
    top_n: int,
    analysis: FaceCaptureAnalysis | None = None,
) -> FaceMatchResult:
    """Shared decision step: pick the winner, compute the margin, apply the threshold."""
    if not scores:
        return FaceMatchResult.no_match(threshold, analysis=analysis)

    ranked = best_per_person(scores)
    winner = ranked[0]
    margin = winner.score - ranked[1].score if len(ranked) > 1 else None
    is_match = winner.score >= threshold

    return FaceMatchResult(
        is_match=is_match,
        person_id=winner.person_id if is_match else None,
        template_id=winner.template_id if is_match else None,
        pose=winner.pose if is_match else None,
        score=winner.score,
        threshold=threshold,
        top_scores=ranked[: max(1, top_n)],
        margin=margin,
        probe_analysis=analysis,
    )
