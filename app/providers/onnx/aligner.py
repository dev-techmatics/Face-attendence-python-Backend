"""Maps the five detected landmarks onto the canonical 112x112 ArcFace arrangement."""

from __future__ import annotations

import math

from app.core.errors import FaceError, FaceProcessingException
from app.core.faces import FaceLandmarks, HeadPose

#: The ArcFace reference landmarks for a 112x112 crop, in the InsightFace convention
#: (left eye, right eye, nose, left mouth corner, right mouth corner). These exact numbers
#: are what make an embedding comparable with any other ArcFace embedding.
REFERENCE: tuple[tuple[float, float], ...] = (
    (38.2946, 51.6963),
    (73.5318, 51.5014),
    (56.0252, 71.7366),
    (41.5493, 92.3655),
    (70.7299, 92.2041),
)

OUTPUT_SIZE = 112


def compute_transform(
    landmarks: FaceLandmarks,
) -> tuple[float, float, float, float, float, float]:
    """Least-squares similarity transform taking the detected landmarks to the reference ones.

    Rotation, uniform scale and translation only. A full affine fit would also absorb shear
    and non-uniform scale, which distorts the face and moves the embedding; restricting to a
    similarity keeps facial proportions intact. This is the closed-form 2D solution, so no SVD
    is needed.

    Returned as ``(a, b, tx, c, d, ty)``, i.e. ``dest = [[a, b], [c, d]] * src + (tx, ty)``.
    """
    source = landmarks.as_tuple()
    n = len(source)

    source_mean_x = sum(p[0] for p in source) / n
    source_mean_y = sum(p[1] for p in source) / n
    target_mean_x = sum(p[0] for p in REFERENCE) / n
    target_mean_y = sum(p[1] for p in REFERENCE) / n

    # `aligned` accumulates the dot component, `perpendicular` the cross component; together
    # they give scale*cos and scale*sin directly.
    aligned = 0.0
    perpendicular = 0.0
    source_variance = 0.0

    for (sx_raw, sy_raw), (tx_raw, ty_raw) in zip(source, REFERENCE):
        sx = sx_raw - source_mean_x
        sy = sy_raw - source_mean_y
        tx = tx_raw - target_mean_x
        ty = ty_raw - target_mean_y

        aligned += (sx * tx) + (sy * ty)
        perpendicular += (sx * ty) - (sy * tx)
        source_variance += (sx * sx) + (sy * sy)

    if source_variance < 1e-6:
        # Degenerate landmarks (all coincident) - nothing sensible to fit.
        raise FaceProcessingException(
            FaceError.LowQuality,
            "The detected landmarks are degenerate; the face is probably too small or occluded.",
        )

    scale_cos = aligned / source_variance
    scale_sin = perpendicular / source_variance

    translate_x = target_mean_x - ((scale_cos * source_mean_x) - (scale_sin * source_mean_y))
    translate_y = target_mean_y - ((scale_sin * source_mean_x) + (scale_cos * source_mean_y))

    return (scale_cos, -scale_sin, translate_x, scale_sin, scale_cos, translate_y)


def estimate_head_pose(landmarks: FaceLandmarks, mirrored_input: bool) -> HeadPose:
    """Yaw estimate from landmark geometry.

    How far the nose has slid off the midpoint between the eyes, in units of inter-eye
    distance.

    Convention: **positive yaw means the subject turned towards their own left**, which in a
    non-mirrored frame moves the nose towards the right of the image.

    Front-facing phone cameras usually hand back a mirrored preview, which flips that sign.
    That is what ``mirrored_input`` is for - get it wrong and every left capture is rejected as
    a right one.

    This is a monocular heuristic, good to roughly +/-10 degrees and plenty for sorting a
    capture into front/left/right. It is not a substitute for a real 6DoF pose model.
    """
    eye_mid_x = (landmarks.left_eye[0] + landmarks.right_eye[0]) / 2.0
    eye_mid_y = (landmarks.left_eye[1] + landmarks.right_eye[1]) / 2.0

    eye_delta_x = landmarks.right_eye[0] - landmarks.left_eye[0]
    eye_delta_y = landmarks.right_eye[1] - landmarks.left_eye[1]
    eye_distance = math.sqrt((eye_delta_x * eye_delta_x) + (eye_delta_y * eye_delta_y))

    if eye_distance < 1e-3:
        return HeadPose(0.0, 0.0, 0.0)

    yaw = min(1.0, max(-1.0, (landmarks.nose[0] - eye_mid_x) / eye_distance)) * 90.0

    if mirrored_input:
        yaw = -yaw

    mouth_mid_y = (landmarks.mouth_left[1] + landmarks.mouth_right[1]) / 2.0
    eye_to_mouth = mouth_mid_y - eye_mid_y

    # Nose sitting high or low between the eye line and the mouth line implies pitch.
    pitch = (
        min(1.0, max(-1.0, ((landmarks.nose[1] - eye_mid_y) / eye_to_mouth) - 0.5)) * 90.0
        if eye_to_mouth > 1e-3
        else 0.0
    )

    # Roll comes straight out of the eye-line angle.
    roll = math.atan2(eye_delta_y, eye_delta_x) * 180.0 / math.pi

    return HeadPose(yaw, pitch, roll)
