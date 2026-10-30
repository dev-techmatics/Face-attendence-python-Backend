from __future__ import annotations

import pytest

from app.core.config import FaceRecognitionSettings
from app.core.errors import FaceError, FaceProcessingException
from app.core.faces import FaceBox, FaceCaptureAnalysis, FaceImage, HeadPose
from app.services.validator import FaceCaptureValidator, detect_signature


def build(**overrides) -> FaceCaptureValidator:
    values = {
        "allowed_content_types": ["image/jpeg", "image/png"],
        "max_image_bytes": 1024 * 1024,
        "min_image_dimension": 240,
        "min_face_dimension": 80,
        "min_detection_score": 0.6,
        "min_quality_score": 0.35,
        "min_profile_yaw_degrees": 12,
        "max_frontal_yaw_degrees": 18,
        "enforce_pose_check": True,
    }
    values.update(overrides)
    return FaceCaptureValidator(FaceRecognitionSettings(**values))


def jpeg_bytes(length: int = 2048) -> bytes:
    """A minimal valid JPEG header, enough to satisfy signature sniffing."""
    content = bytearray(length)
    content[0] = 0xFF
    content[1] = 0xD8
    content[2] = 0xFF
    return bytes(content)


def good_analysis(yaw: float = 0) -> FaceCaptureAnalysis:
    return FaceCaptureAnalysis(
        face_detected=True,
        face_count=1,
        detection_score=0.95,
        quality_score=0.8,
        box=FaceBox(50, 50, 200, 200),
        head_pose=HeadPose(yaw, 0, 0),
        sharpness=0.7,
        brightness=0.5,
        image_width=640,
        image_height=640,
    )


def test_validate_upload_good_jpeg_passes():
    build().validate_upload(FaceImage(jpeg_bytes(), "image/jpeg", "a.jpg"))


def test_validate_upload_empty_file_rejected():
    with pytest.raises(FaceProcessingException) as exc:
        build().validate_upload(FaceImage(b"", "image/jpeg", "a.jpg"))

    assert exc.value.error == FaceError.CorruptImage


def test_validate_upload_oversized_rejected():
    with pytest.raises(FaceProcessingException) as exc:
        build(max_image_bytes=64 * 1024).validate_upload(
            FaceImage(jpeg_bytes(128 * 1024), "image/jpeg", "a.jpg")
        )

    assert exc.value.error == FaceError.ImageTooLarge
    assert exc.value.hints


def test_validate_upload_unsupported_content_type_rejected():
    with pytest.raises(FaceProcessingException) as exc:
        build().validate_upload(FaceImage(jpeg_bytes(), "image/gif", "a.gif"))

    assert exc.value.error == FaceError.UnsupportedFormat


def test_validate_upload_text_file_labelled_as_jpeg_rejected():
    # The declared content type is attacker-controlled, so the bytes decide.
    not_an_image = b"<?php echo 1; ?>aaaaaaaaaaaa"

    with pytest.raises(FaceProcessingException) as exc:
        build().validate_upload(FaceImage(not_an_image, "image/jpeg", "shell.jpg"))

    assert exc.value.error == FaceError.UnsupportedFormat


def test_detect_signature_jpeg_recognised():
    assert detect_signature(bytes([0xFF, 0xD8, 0xFF, 0, 0])) == "image/jpeg"


def test_detect_signature_png_recognised():
    assert (
        detect_signature(bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]))
        == "image/png"
    )


def test_detect_signature_webp_recognised():
    assert detect_signature(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"


def test_detect_signature_garbage_is_none():
    assert detect_signature(bytes(range(1, 13))) is None


def test_validate_analysis_good_frame_passes():
    build().validate_analysis(good_analysis())


def test_validate_analysis_no_face_rejected():
    with pytest.raises(FaceProcessingException) as exc:
        build().validate_analysis(
            FaceCaptureAnalysis(face_detected=False, image_width=640, image_height=640)
        )

    assert exc.value.error == FaceError.NoFaceDetected
    assert exc.value.hints


def test_validate_analysis_two_faces_rejected():
    # Registering with a bystander in frame is how the wrong face gets enrolled.
    with pytest.raises(FaceProcessingException) as exc:
        build().validate_analysis(
            FaceCaptureAnalysis(
                face_detected=True,
                face_count=2,
                detection_score=0.9,
                quality_score=0.8,
                box=FaceBox(0, 0, 200, 200),
                image_width=640,
                image_height=640,
            )
        )

    assert exc.value.error == FaceError.MultipleFacesDetected


def test_validate_analysis_tiny_image_rejected():
    with pytest.raises(FaceProcessingException) as exc:
        build().validate_analysis(
            FaceCaptureAnalysis(
                face_detected=True,
                face_count=1,
                detection_score=0.9,
                quality_score=0.9,
                image_width=100,
                image_height=100,
            )
        )

    assert exc.value.error == FaceError.ImageTooSmall


def test_validate_analysis_face_too_far_away_rejected():
    with pytest.raises(FaceProcessingException) as exc:
        build().validate_analysis(
            FaceCaptureAnalysis(
                face_detected=True,
                face_count=1,
                detection_score=0.9,
                quality_score=0.9,
                box=FaceBox(0, 0, 40, 40),
                image_width=640,
                image_height=640,
            )
        )

    assert exc.value.error == FaceError.LowQuality


def test_validate_analysis_low_detection_score_rejected():
    with pytest.raises(FaceProcessingException) as exc:
        build().validate_analysis(
            FaceCaptureAnalysis(
                face_detected=True,
                face_count=1,
                detection_score=0.2,
                quality_score=0.9,
                box=FaceBox(0, 0, 200, 200),
                image_width=640,
                image_height=640,
            )
        )

    assert exc.value.error == FaceError.LowQuality


def test_validate_analysis_blurry_frame_says_so():
    with pytest.raises(FaceProcessingException) as exc:
        build().validate_analysis(
            FaceCaptureAnalysis(
                face_detected=True,
                face_count=1,
                detection_score=0.9,
                quality_score=0.1,
                box=FaceBox(0, 0, 200, 200),
                sharpness=0.05,
                brightness=0.5,
                image_width=640,
                image_height=640,
            )
        )

    assert exc.value.error == FaceError.LowQuality
    assert any("blurry" in hint for hint in exc.value.hints)
