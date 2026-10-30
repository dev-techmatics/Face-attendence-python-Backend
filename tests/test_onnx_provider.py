"""Integration tests for the self-hosted ONNX pipeline.

Skipped unless both model files are present, so a clone without them still runs green. Put
them there with ``python scripts/download_models.py``.

These are the tests that would catch a wrong channel order, a mis-decoded anchor grid or an
inverted warp matrix - none of which crash, they just quietly destroy accuracy.
"""

from __future__ import annotations

import io
import math
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.core import vector_math
from app.core.config import FaceRecognitionSettings, resolve_path
from app.core.enums import FacePose
from app.core.errors import FaceError, FaceProcessingException
from app.core.faces import FaceCandidate, FaceImage

#: Images with a real face in them. Placed here by the developer or by CI; the tests skip
#: without them rather than downloading anything at test time.
FIXTURE_DIR = Path(os.environ.get("FA_TEST_FACE_DIR", "tests/fixtures")).resolve()


def _settings(**overrides) -> FaceRecognitionSettings:
    values = {
        "provider": "onnx",
        "onnx": {
            "detector_model_path": "models/scrfd_detector.onnx",
            "recognition_model_path": "models/arcface_recognizer.onnx",
            "model_version": "test",
            "max_concurrency": 2,
        },
    }
    values.update(overrides)
    return FaceRecognitionSettings(**values)


def _models_present() -> bool:
    onnx = _settings().onnx
    return (
        resolve_path(onnx.detector_model_path).is_file()
        and resolve_path(onnx.recognition_model_path).is_file()
    )


def _faces_present() -> bool:
    return FIXTURE_DIR.is_dir() and any(FIXTURE_DIR.glob("*.jpg"))


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _models_present(),
        reason="ONNX models are not present; run scripts/download_models.py",
    ),
]


@pytest.fixture(scope="module")
def provider():
    from app.providers.onnx.provider import OnnxFaceRecognitionProvider

    instance = OnnxFaceRecognitionProvider(_settings())
    yield instance
    instance.close()


def face_image(name: str) -> FaceImage:
    path = FIXTURE_DIR / name

    if not path.is_file():
        pytest.skip(f"{path} is not present")

    return FaceImage(path.read_bytes(), "image/jpeg", name)


def reencoded(name: str, scale: float = 0.85, quality: int = 70) -> FaceImage:
    """The same face, resized and re-encoded.

    A different file with different bytes, so it exercises the pipeline rather than a cache,
    while still being the same person - which is what a high score has to mean.
    """
    path = FIXTURE_DIR / name

    if not path.is_file():
        pytest.skip(f"{path} is not present")

    with Image.open(path) as source:
        resized = source.convert("RGB").resize(
            (int(source.width * scale), int(source.height * scale)), Image.LANCZOS
        )

    buffer = io.BytesIO()
    resized.save(buffer, format="JPEG", quality=quality)

    return FaceImage(buffer.getvalue(), "image/jpeg", f"reencoded-{name}")


async def test_provider_id_records_the_model_and_dimension(provider):
    # A changed provider id is what marks existing enrolments Stale, so it has to carry
    # everything that would invalidate a template.
    assert provider.provider_id.startswith("onnx:scrfd+arcface:test:d")
    assert provider.provider_id.endswith("512")


async def test_detects_a_face_in_a_real_photo(provider):
    analysis = await provider.analyze(face_image("portrait.jpg"))

    assert analysis.face_detected
    assert analysis.face_count >= 1
    assert analysis.detection_score > 0.6
    assert analysis.box is not None
    assert analysis.landmarks is not None
    assert analysis.image_width > 0

    # The five landmarks must sit inside the detected box, or the warp will be nonsense.
    box = analysis.box
    for x, y in analysis.landmarks.as_tuple():
        assert box.x - 10 <= x <= box.right + 10
        assert box.y - 10 <= y <= box.bottom + 10


async def test_finds_several_faces_in_a_group_photo(provider):
    analysis = await provider.analyze(face_image("group.jpg"))

    assert analysis.face_detected
    assert analysis.face_count > 1
    # The largest face wins, on the assumption that the person at the kiosk is nearest.
    assert any("faces were detected" in w for w in analysis.warnings)


async def test_no_face_in_a_blank_frame(provider):
    buffer = io.BytesIO()
    Image.new("RGB", (640, 640), (120, 120, 120)).save(buffer, format="JPEG")

    analysis = await provider.analyze(FaceImage(buffer.getvalue(), "image/jpeg", "blank.jpg"))

    assert not analysis.face_detected
    assert analysis.face_count == 0


async def test_create_template_returns_a_unit_length_512d_embedding(provider):
    material = await provider.create_template(face_image("portrait.jpg"), FacePose.Front)

    assert material.dimension == 512
    assert len(material.data) == 512 * 4

    embedding = vector_math.from_bytes(material.data)
    norm = math.sqrt(float(np.sum(embedding.astype(np.float64) ** 2)))

    # Every stored template is a unit vector, so comparison is a plain dot product and
    # thresholds stay comparable across captures.
    assert norm == pytest.approx(1.0, abs=1e-4)


async def test_create_template_on_a_blank_frame_is_refused(provider):
    buffer = io.BytesIO()
    Image.new("RGB", (640, 640), (10, 10, 10)).save(buffer, format="JPEG")

    with pytest.raises(FaceProcessingException) as exc:
        await provider.create_template(
            FaceImage(buffer.getvalue(), "image/jpeg", "blank.jpg"), FacePose.Front
        )

    assert exc.value.error == FaceError.NoFaceDetected


async def test_a_corrupt_upload_is_refused(provider):
    with pytest.raises(FaceProcessingException) as exc:
        await provider.analyze(FaceImage(b"\xff\xd8\xff" + bytes(200), "image/jpeg", "x.jpg"))

    assert exc.value.error == FaceError.CorruptImage


async def test_the_same_person_scores_far_above_the_threshold(provider):
    original = await provider.create_template(face_image("portrait.jpg"), FacePose.Front)
    variant = await provider.create_template(reencoded("portrait.jpg"), FacePose.Front)

    score = provider.compare_templates(original.data, variant.data)

    # Resized and re-encoded, so the bytes differ entirely - but it is the same face, and the
    # shipped verification threshold is 0.42.
    assert score > 0.8, f"same face scored only {score:.3f}"


async def test_a_different_person_scores_far_below_the_threshold(provider):
    one = await provider.create_template(face_image("portrait.jpg"), FacePose.Front)
    other = await provider.create_template(face_image("group.jpg"), FacePose.Front)

    score = provider.compare_templates(one.data, other.data)

    assert score < 0.3, f"different faces scored {score:.3f}, which is too close to a match"


async def test_match_picks_the_right_person_out_of_a_candidate_set(provider):
    mine = await provider.create_template(face_image("portrait.jpg"), FacePose.Front)
    theirs = await provider.create_template(face_image("group.jpg"), FacePose.Front)

    candidates = [
        FaceCandidate(
            person_id=1, template_id=11, pose=FacePose.Front, template_data=theirs.data
        ),
        FaceCandidate(
            person_id=2, template_id=22, pose=FacePose.Front, template_data=mine.data
        ),
    ]

    result = await provider.match(reencoded("portrait.jpg"), candidates, threshold=0.42)

    assert result.is_match
    assert result.person_id == 2
    assert result.template_id == 22
    assert result.score > 0.8
    # The runner-up is a different person, so the margin must be comfortable.
    assert result.margin is not None and result.margin > 0.3


async def test_match_against_only_the_wrong_person_does_not_match(provider):
    theirs = await provider.create_template(face_image("group.jpg"), FacePose.Front)

    candidates = [
        FaceCandidate(
            person_id=1, template_id=11, pose=FacePose.Front, template_data=theirs.data
        )
    ]

    result = await provider.match(face_image("portrait.jpg"), candidates, threshold=0.42)

    assert not result.is_match
    assert result.person_id is None
    assert result.score < 0.42


async def test_a_template_from_another_model_is_skipped_not_fatal(provider):
    """One stale row must not take down attendance for everybody."""
    mine = await provider.create_template(face_image("portrait.jpg"), FacePose.Front)

    candidates = [
        # A 128-d template, as a different model would have produced.
        FaceCandidate(
            person_id=1,
            template_id=11,
            pose=FacePose.Front,
            template_data=vector_math.to_bytes(np.zeros(128, dtype=np.float32)),
        ),
        FaceCandidate(
            person_id=2, template_id=22, pose=FacePose.Front, template_data=mine.data
        ),
    ]

    result = await provider.match(reencoded("portrait.jpg"), candidates, threshold=0.42)

    assert result.is_match
    assert result.person_id == 2
    # The incompatible row is skipped entirely rather than scored as zero.
    assert [s.person_id for s in result.top_scores] == [2]


async def test_quality_scoring_separates_a_sharp_frame_from_a_blurred_one(provider):
    from PIL import ImageFilter

    path = FIXTURE_DIR / "portrait.jpg"
    if not path.is_file():
        pytest.skip("portrait.jpg is not present")

    with Image.open(path) as source:
        blurred = source.convert("RGB").filter(ImageFilter.GaussianBlur(radius=6))

    buffer = io.BytesIO()
    blurred.save(buffer, format="JPEG", quality=92)

    sharp = await provider.analyze(face_image("portrait.jpg"))
    soft = await provider.analyze(FaceImage(buffer.getvalue(), "image/jpeg", "blurred.jpg"))

    if not soft.face_detected:
        pytest.skip("the blurred frame lost its face entirely, which is a valid outcome")

    assert soft.sharpness < sharp.sharpness
    assert soft.quality_score < sharp.quality_score


async def test_concurrent_matches_do_not_interfere(provider):
    import asyncio

    mine = await provider.create_template(face_image("portrait.jpg"), FacePose.Front)
    candidates = [
        FaceCandidate(
            person_id=7, template_id=70, pose=FacePose.Front, template_data=mine.data
        )
    ]

    # The concurrency cap is 2 here, so this queues as well as parallelises.
    results = await asyncio.gather(
        *(provider.match(reencoded("portrait.jpg"), candidates, 0.42) for _ in range(5))
    )

    assert all(r.is_match and r.person_id == 7 for r in results)
    # Deterministic: the same probe must produce the same score every time.
    assert len({round(r.score, 6) for r in results}) == 1
