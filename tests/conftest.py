"""Shared fixtures and test doubles."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from uuid import UUID

import numpy as np
import pytest
import pytest_asyncio
from numpy.typing import NDArray
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
    HeadPose,
)
from app.db.base import Base
from app.providers.base import FaceRecognitionProvider, build_match_result
from app.storage import object_keys
from app.storage.base import FaceImageStore


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


class FakeImageStore(FaceImageStore):
    """In-memory image store that also lets a test assert what was written and deleted."""

    def __init__(self) -> None:
        self.saved: dict[str, bytes] = {}

    async def save(self, object_key: str, content: bytes, content_type: str) -> str:
        self.saved[object_key] = content
        return object_key

    async def read(self, object_key: str) -> bytes | None:
        return self.saved.get(object_key)

    async def delete(self, object_key: str) -> None:
        self.saved.pop(object_key, None)

    async def exists(self, object_key: str) -> bool:
        return object_key in self.saved

    def build_enrollment_key(
        self, tenant_id: str, person_id: int, pose: str, version: int, content_type: str
    ) -> str:
        return object_keys.enrollment(tenant_id, person_id, pose, version, content_type)

    def build_probe_key(
        self, tenant_id: str, when_utc: datetime, file_id: UUID, content_type: str
    ) -> str:
        return object_keys.probe(tenant_id, when_utc, file_id, content_type)


class ScriptedProvider(FaceRecognitionProvider):
    """A provider whose output a test controls directly.

    Set :attr:`identity` and every template produced belongs to that notional person. Lets the
    enrolment rules be tested without any model, image decoding, or randomness.
    """

    DIMENSION = 128

    def __init__(self) -> None:
        #: Whose face the next capture represents.
        self.identity = "default"
        #: When set, captures for this pose raise, to exercise failure paths.
        self.fail_on: FacePose | None = None

    @property
    def provider_id(self) -> str:
        return "test:scripted:v1"

    @property
    def template_kind(self) -> FaceTemplateKind:
        return FaceTemplateKind.Embedding

    async def analyze(self, image: FaceImage) -> FaceCaptureAnalysis:
        return _analysis()

    async def create_template(self, image: FaceImage, pose: FacePose) -> FaceTemplateMaterial:
        if self.fail_on == pose:
            raise FaceProcessingException(
                FaceError.LowQuality, f"Scripted failure on {FacePose(pose).name}."
            )

        embedding = _embedding_for(self.identity)

        return FaceTemplateMaterial(
            provider_id=self.provider_id,
            kind=FaceTemplateKind.Embedding,
            dimension=int(embedding.size),
            data=vector_math.to_bytes(embedding),
            quality_score=0.9,
            detection_score=0.95,
            analysis=_analysis(),
        )

    async def match(
        self,
        probe: FaceImage,
        candidates: Sequence[FaceCandidate],
        threshold: float,
        top_n: int = 5,
    ) -> FaceMatchResult:
        probe_embedding = _embedding_for(self.identity)

        scores = [
            FaceScore(
                person_id=c.person_id,
                template_id=c.template_id,
                pose=c.pose,
                score=vector_math.cosine_similarity(
                    probe_embedding, vector_math.from_bytes(c.template_data)
                ),
            )
            for c in candidates
        ]

        return build_match_result(scores, threshold, top_n, _analysis())

    def compare_templates(self, a: bytes, b: bytes) -> float | None:
        return vector_math.cosine_similarity(
            vector_math.from_bytes(a), vector_math.from_bytes(b)
        )


def _analysis() -> FaceCaptureAnalysis:
    return FaceCaptureAnalysis(
        face_detected=True,
        face_count=1,
        detection_score=0.95,
        quality_score=0.9,
        box=FaceBox(100, 100, 200, 200),
        head_pose=HeadPose(0, 0, 0),
        sharpness=0.8,
        brightness=0.5,
        image_width=640,
        image_height=640,
        warnings=[],
    )


def _embedding_for(identity: str) -> NDArray[np.float32]:
    """One fixed orthogonal direction per identity.

    The same identity scores 1.0 against itself, different identities score 0, which is
    exactly the property the enrolment rules are written against.
    """
    vector = np.zeros(ScriptedProvider.DIMENSION, dtype=np.float32)
    digest = int.from_bytes(identity.encode("utf-8"), "little", signed=False)
    vector[digest % ScriptedProvider.DIMENSION] = 1.0
    return vector


def jpeg(marker: str, length: int = 512) -> FaceImage:
    """A minimal valid JPEG header plus a marker, so each image has a distinct hash."""
    content = bytearray(length)
    content[0] = 0xFF
    content[1] = 0xD8
    content[2] = 0xFF

    seed = marker.encode("utf-8")[:64]
    content[16 : 16 + len(seed)] = seed

    return FaceImage(bytes(content), "image/jpeg", f"{marker}.jpg")


def face_settings(**overrides) -> FaceRecognitionSettings:
    """Settings tuned for tests: no pose gate, small limits, everything else as shipped."""
    values = {
        "provider": "development",
        "required_poses": [FacePose.Front, FacePose.Left, FacePose.Right],
        "allowed_content_types": ["image/jpeg"],
        "max_image_bytes": 1024 * 1024,
        "min_image_dimension": 240,
        "min_face_dimension": 80,
        "min_detection_score": 0.6,
        "min_quality_score": 0.35,
        "enforce_pose_check": False,
        "duplicate_enrollment_threshold": 0.6,
        "min_enrollment_pair_similarity": 0.2,
        "reject_duplicate_frames": True,
        "reject_duplicate_enrollments": True,
        "store_probe_images": False,
    }
    values.update(overrides)
    return FaceRecognitionSettings(**values)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A real SQLite database, so the repository queries are covered rather than mocked away."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with factory() as db:
        yield db

    await engine.dispose()


@pytest.fixture
def store() -> FakeImageStore:
    return FakeImageStore()


@pytest.fixture
def provider() -> ScriptedProvider:
    return ScriptedProvider()
