"""Startup validation of the recognition settings.

A threshold typo such as 42 instead of 0.42 would silently reject every employee, so the
process refuses to start rather than failing quietly all day.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import FaceRecognitionSettings
from app.core.enums import FacePose


def valid(**overrides) -> dict:
    values = {
        "provider": "onnx",
        "required_poses": [FacePose.Front, FacePose.Left, FacePose.Right],
        "verification_threshold": 0.42,
        "identification_threshold": 0.52,
        "duplicate_enrollment_threshold": 0.6,
        "min_enrollment_pair_similarity": 0.2,
        "min_quality_score": 0.35,
        "min_detection_score": 0.6,
        "min_image_dimension": 240,
        "min_face_dimension": 80,
        "max_image_bytes": 8 * 1024 * 1024,
    }
    values.update(overrides)
    return values


def test_valid_defaults_pass():
    assert FaceRecognitionSettings(**valid()).provider == "onnx"


def test_threshold_above_one_rejected():
    # The classic typo: 42 meant as a percentage would reject every single employee.
    with pytest.raises(ValidationError, match="verification_threshold"):
        FaceRecognitionSettings(**valid(verification_threshold=42))


def test_identification_looser_than_verification_rejected():
    with pytest.raises(ValidationError):
        FaceRecognitionSettings(
            **valid(identification_threshold=0.30, verification_threshold=0.50)
        )


def test_no_required_poses_falls_back_to_the_standard_three():
    # An empty list is a configuration-binding artefact, not an instruction to enrol nobody.
    settings = FaceRecognitionSettings(**valid(required_poses=[]))

    assert settings.required_poses == [FacePose.Front, FacePose.Left, FacePose.Right]


def test_unknown_provider_rejected():
    with pytest.raises(ValidationError):
        FaceRecognitionSettings(**valid(provider="magic"))


def test_face_larger_than_image_rejected():
    with pytest.raises(ValidationError):
        FaceRecognitionSettings(**valid(min_face_dimension=500, min_image_dimension=240))


def test_tiny_image_dimension_rejected():
    with pytest.raises(ValidationError):
        FaceRecognitionSettings(**valid(min_image_dimension=32, min_face_dimension=16))


def test_tiny_max_image_bytes_rejected():
    with pytest.raises(ValidationError):
        FaceRecognitionSettings(**valid(max_image_bytes=1024))


def test_poses_are_deduped():
    settings = FaceRecognitionSettings(
        **valid(
            required_poses=[
                FacePose.Front,
                FacePose.Left,
                FacePose.Right,
                FacePose.Front,
                FacePose.Left,
            ]
        )
    )

    assert len(settings.required_poses) == 3


def test_poses_accept_names_as_the_config_file_writes_them():
    settings = FaceRecognitionSettings(**valid(required_poses=["Front", "Left", "Right"]))

    assert settings.required_poses == [FacePose.Front, FacePose.Left, FacePose.Right]


def test_content_types_are_normalised():
    settings = FaceRecognitionSettings(
        **valid(allowed_content_types=[" IMAGE/JPEG ", "image/jpeg", "image/png", ""])
    )

    assert settings.allowed_content_types == ["image/jpeg", "image/png"]


def test_empty_content_types_gets_defaults():
    settings = FaceRecognitionSettings(**valid(allowed_content_types=[]))

    assert "image/jpeg" in settings.allowed_content_types
