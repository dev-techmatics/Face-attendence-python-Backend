"""The pose gate.

It is what stops three frontal snapshots being accepted as a complete three-angle enrolment,
which would quietly discard the whole point of capturing three images.
"""

from __future__ import annotations

import pytest

from app.core.config import FaceRecognitionSettings
from app.core.enums import FacePose
from app.core.errors import FaceError, FaceProcessingException
from app.core.faces import FaceCaptureAnalysis, HeadPose
from app.services.validator import FaceCaptureValidator


def build(enforce: bool = True) -> FaceCaptureValidator:
    return FaceCaptureValidator(
        FaceRecognitionSettings(
            enforce_pose_check=enforce,
            min_profile_yaw_degrees=12,
            max_frontal_yaw_degrees=18,
        )
    )


def with_yaw(yaw: float) -> FaceCaptureAnalysis:
    return FaceCaptureAnalysis(
        face_detected=True,
        face_count=1,
        detection_score=0.95,
        quality_score=0.8,
        head_pose=HeadPose(yaw, 0, 0),
        image_width=640,
        image_height=640,
    )


@pytest.mark.parametrize("yaw", [0, 10, -10, 17.9])
def test_front_within_tolerance_accepted(yaw):
    build().validate_pose(with_yaw(yaw), FacePose.Front)


@pytest.mark.parametrize("yaw", [25, -25])
def test_front_turned_too_far_rejected(yaw):
    with pytest.raises(FaceProcessingException) as exc:
        build().validate_pose(with_yaw(yaw), FacePose.Front)

    assert exc.value.error == FaceError.PoseMismatch


@pytest.mark.parametrize("yaw", [15, 40])
def test_left_turned_enough_accepted(yaw):
    build().validate_pose(with_yaw(yaw), FacePose.Left)


@pytest.mark.parametrize("yaw", [0, 5, -30])
def test_left_not_turned_enough_rejected(yaw):
    with pytest.raises(FaceProcessingException) as exc:
        build().validate_pose(with_yaw(yaw), FacePose.Left)

    assert exc.value.error == FaceError.PoseMismatch


@pytest.mark.parametrize("yaw", [-15, -40])
def test_right_turned_enough_accepted(yaw):
    build().validate_pose(with_yaw(yaw), FacePose.Right)


@pytest.mark.parametrize("yaw", [0, -5, 30])
def test_right_not_turned_enough_rejected(yaw):
    with pytest.raises(FaceProcessingException) as exc:
        build().validate_pose(with_yaw(yaw), FacePose.Right)

    assert exc.value.error == FaceError.PoseMismatch


def test_three_frontal_frames_would_all_be_rejected_for_the_profile_poses():
    validator = build()
    frontal = with_yaw(2)

    validator.validate_pose(frontal, FacePose.Front)

    with pytest.raises(FaceProcessingException):
        validator.validate_pose(frontal, FacePose.Left)

    with pytest.raises(FaceProcessingException):
        validator.validate_pose(frontal, FacePose.Right)


def test_pose_check_disabled_accepts_anything():
    # Useful for cameras whose yaw estimate cannot be trusted, at the cost of the guarantee.
    build(enforce=False).validate_pose(with_yaw(0), FacePose.Left)


def test_no_head_pose_available_skips_the_check():
    # A provider that cannot estimate yaw must not block enrolment outright.
    build().validate_pose(
        FaceCaptureAnalysis(face_detected=True, face_count=1, head_pose=None), FacePose.Left
    )
