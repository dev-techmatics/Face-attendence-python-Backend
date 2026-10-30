"""Gatekeeper for everything that arrives from a capture screen.

Runs before any model does work, so bad frames cost a few microseconds instead of an
inference.
"""

from __future__ import annotations

from app.core.config import FaceRecognitionSettings
from app.core.enums import FacePose
from app.core.errors import FaceError, FaceProcessingException
from app.core.faces import FaceCaptureAnalysis, FaceImage


class FaceCaptureValidator:
    def __init__(self, settings: FaceRecognitionSettings) -> None:
        self._settings = settings

    def validate_upload(self, image: FaceImage) -> None:
        """Cheap checks on the raw upload: size, declared type, and actual file signature."""
        if image.size_bytes == 0:
            raise FaceProcessingException(
                FaceError.CorruptImage, "The uploaded image is empty."
            )

        if image.size_bytes > self._settings.max_image_bytes:
            raise FaceProcessingException(
                FaceError.ImageTooLarge,
                f"Image is {image.size_bytes // 1024}KB; the limit is "
                f"{self._settings.max_image_bytes // 1024}KB.",
                hints=[
                    "Capture at 720p and encode as JPEG quality 80 - that is ample for "
                    "recognition."
                ],
            )

        allowed = {t.lower() for t in self._settings.allowed_content_types}

        if image.content_type.lower() not in allowed:
            raise FaceProcessingException(
                FaceError.UnsupportedFormat,
                f"Content type '{image.content_type}' is not accepted. "
                f"Allowed: {', '.join(self._settings.allowed_content_types)}.",
            )

        # Never trust the declared content type: a client can label anything image/jpeg.
        if detect_signature(image.content) is None:
            raise FaceProcessingException(
                FaceError.UnsupportedFormat,
                "The file content is not a JPEG, PNG or WebP image, whatever the content "
                "type says.",
            )

    def validate_analysis(self, analysis: FaceCaptureAnalysis) -> None:
        """Checks that need the detector output."""
        minimum = self._settings.min_image_dimension

        if analysis.image_width < minimum or analysis.image_height < minimum:
            raise FaceProcessingException(
                FaceError.ImageTooSmall,
                f"Image is {analysis.image_width}x{analysis.image_height}; both sides must be "
                f"at least {minimum}px.",
            )

        if not analysis.face_detected:
            raise FaceProcessingException(
                FaceError.NoFaceDetected,
                "No face was found in the frame.",
                hints=[
                    "Fill roughly a third of the frame with the face.",
                    "Light the face from the front; avoid a bright window behind the subject.",
                ],
            )

        if analysis.face_count > 1:
            raise FaceProcessingException(
                FaceError.MultipleFacesDetected,
                f"{analysis.face_count} faces were found. Registration needs exactly one "
                "person in frame.",
            )

        if analysis.detection_score < self._settings.min_detection_score:
            raise FaceProcessingException(
                FaceError.LowQuality,
                f"Detector confidence {analysis.detection_score:.2f} is below the required "
                f"{self._settings.min_detection_score:.2f}.",
            )

        if (
            analysis.box is not None
            and min(analysis.box.width, analysis.box.height) < self._settings.min_face_dimension
        ):
            raise FaceProcessingException(
                FaceError.LowQuality,
                f"The face occupies only {analysis.box.width:.0f}x{analysis.box.height:.0f}px; "
                f"at least {self._settings.min_face_dimension}px is needed.",
                hints=["Ask the subject to move closer to the camera."],
            )

        if analysis.quality_score < self._settings.min_quality_score:
            hints: list[str] = []

            if analysis.sharpness < 0.3:
                hints.append(
                    "The frame is blurry - hold steady, or capture on a tap rather than "
                    "continuously."
                )

            if analysis.brightness < 0.25:
                hints.append("The frame is underexposed - add light on the face.")
            elif analysis.brightness > 0.85:
                hints.append(
                    "The frame is overexposed - reduce backlight or move out of direct sun."
                )

            raise FaceProcessingException(
                FaceError.LowQuality,
                f"Quality score {analysis.quality_score:.2f} is below the required "
                f"{self._settings.min_quality_score:.2f}.",
                hints=hints,
            )

    def validate_pose(self, analysis: FaceCaptureAnalysis, expected: FacePose) -> None:
        """Confirms the subject actually turned their head the way the prompt asked.

        Without this, three frontal shots sail through as a complete three-pose enrolment and
        you lose the angular coverage that made the three captures worth collecting.
        """
        if not self._settings.enforce_pose_check or analysis.head_pose is None:
            return

        yaw = analysis.head_pose.yaw_degrees
        min_profile = self._settings.min_profile_yaw_degrees

        if expected == FacePose.Front and abs(yaw) > self._settings.max_frontal_yaw_degrees:
            raise FaceProcessingException(
                FaceError.PoseMismatch,
                f"This is meant to be a straight-on capture but the head is turned "
                f"{abs(yaw):.0f} degrees.",
                hints=["Look directly at the camera."],
            )

        # Yaw sign convention: positive yaw means the subject turned to their own left.
        if expected == FacePose.Left and yaw < min_profile:
            raise FaceProcessingException(
                FaceError.PoseMismatch,
                f"Expected a left-turned capture of at least {min_profile:.0f} degrees; "
                f"measured {yaw:.0f}.",
                hints=[
                    "Ask the subject to turn their head to their left, keeping both eyes "
                    "visible."
                ],
            )

        if expected == FacePose.Right and yaw > -min_profile:
            raise FaceProcessingException(
                FaceError.PoseMismatch,
                f"Expected a right-turned capture of at least {min_profile:.0f} degrees; "
                f"measured {yaw:.0f}.",
                hints=[
                    "Ask the subject to turn their head to their right, keeping both eyes "
                    "visible."
                ],
            )


def detect_signature(content: bytes) -> str | None:
    """Returns the normalised content type implied by the file bytes, or None."""
    if len(content) >= 3 and content[0] == 0xFF and content[1] == 0xD8 and content[2] == 0xFF:
        return "image/jpeg"

    if len(content) >= 8 and content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"

    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"

    return None
