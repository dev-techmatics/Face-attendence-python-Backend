"""SCRFD face detector.

Returns boxes plus the five landmarks that alignment needs, which is why a ``bnkps`` model
variant is required rather than a plain box detector.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray
from PIL import Image

from app.core.config import OnnxProviderSettings, resolve_path
from app.core.errors import FaceError, FaceProcessingException
from app.core.faces import FaceBox, FaceLandmarks
from app.providers.onnx import image_ops

#: SCRFD heads are emitted at these three strides.
STRIDES: tuple[int, int, int] = (8, 16, 32)


@dataclass(frozen=True)
class DetectedFace:
    """A detected face in original-image coordinates."""

    box: FaceBox
    landmarks: FaceLandmarks
    score: float


class ScrfdFaceDetector:
    def __init__(self, settings: OnnxProviderSettings) -> None:
        self._settings = settings
        model_path: Path = resolve_path(settings.detector_model_path)

        if not model_path.is_file():
            raise FaceProcessingException(
                FaceError.ProviderNotConfigured,
                f"Detector model not found at {model_path}. "
                "Run scripts/download_models.py, or set the face provider to development.",
            )

        session_options = ort.SessionOptions()
        if settings.intra_op_threads > 0:
            session_options.intra_op_num_threads = settings.intra_op_threads

        self._session = ort.InferenceSession(
            str(model_path), sess_options=session_options, providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name

        # Group the heads by their last dimension rather than by name: SCRFD exports use
        # wildly different output names (score_8, 447, onnx::Concat_1), but the shapes are
        # stable - 1 value per anchor for scores, 4 for box distances, 10 for five landmark
        # pairs. Within each group the model emits strides 8, 16, 32 in order.
        scores: list[str] = []
        boxes: list[str] = []
        keypoints: list[str] = []

        for output in self._session.get_outputs():
            shape = output.shape or []
            last = shape[-1] if shape else None
            last_dimension = last if isinstance(last, int) else -1

            if last_dimension == 1:
                scores.append(output.name)
            elif last_dimension == 4:
                boxes.append(output.name)
            elif last_dimension == 10:
                keypoints.append(output.name)

        if len(scores) < len(STRIDES) or len(boxes) < len(STRIDES):
            raise FaceProcessingException(
                FaceError.ProviderNotConfigured,
                f"The detector at {model_path} exposes {len(scores)} score and {len(boxes)} "
                "box heads; an SCRFD model with three of each was expected.",
            )

        if len(keypoints) < len(STRIDES):
            raise FaceProcessingException(
                FaceError.ProviderNotConfigured,
                "This detector emits no facial landmarks, so faces cannot be aligned and "
                "recognition accuracy would collapse. Use an SCRFD variant with kps, such as "
                "a buffalo pack's det_500m or det_10g.",
            )

        self._score_outputs = scores
        self._box_outputs = boxes
        self._keypoint_outputs = keypoints

    def detect(self, image: Image.Image) -> list[DetectedFace]:
        """Detect faces, strongest first."""
        size = self._settings.detector_input_size
        letterboxed = image_ops.letterbox(image, size)

        # SCRFD preprocessing: RGB, (pixel - 127.5) / 128, NCHW.
        tensor = (letterboxed.pixels.astype(np.float32) - 127.5) / 128.0
        tensor = np.ascontiguousarray(tensor.transpose(2, 0, 1)[np.newaxis, ...])

        outputs = self._session.run(None, {self._input_name: tensor})
        named = {
            output.name: value
            for output, value in zip(self._session.get_outputs(), outputs)
        }

        candidates: list[DetectedFace] = []

        for level in range(len(STRIDES)):
            candidates.extend(self._decode_level(level, size, letterboxed, named))

        return _non_maximum_suppression(candidates, self._settings.detector_nms_threshold)

    def _decode_level(
        self,
        level: int,
        size: int,
        letterboxed: image_ops.Letterboxed,
        outputs: dict[str, NDArray[np.float32]],
    ) -> list[DetectedFace]:
        stride = STRIDES[level]
        scores = np.asarray(outputs[self._score_outputs[level]], dtype=np.float32).reshape(-1)
        boxes = np.asarray(outputs[self._box_outputs[level]], dtype=np.float32).reshape(-1, 4)
        keypoints = np.asarray(
            outputs[self._keypoint_outputs[level]], dtype=np.float32
        ).reshape(-1, 10)

        grid = size // stride
        locations = grid * grid

        # Most SCRFD exports place two anchors per cell, a few place one. Infer it rather than
        # assume: getting it wrong silently halves or doubles the decoded grid.
        anchors_per_location = 0 if locations == 0 else scores.shape[0] // locations
        if anchors_per_location not in (1, 2):
            return []

        keep = scores >= self._settings.detector_confidence_threshold
        if not np.any(keep):
            return []

        indices = np.nonzero(keep)[0]
        location = indices // anchors_per_location
        center_x = (location % grid).astype(np.float32) * stride
        center_y = (location // grid).astype(np.float32) * stride

        # The box head predicts distances from the anchor centre to each edge, in strides.
        selected_boxes = boxes[indices] * stride
        x1 = center_x - selected_boxes[:, 0]
        y1 = center_y - selected_boxes[:, 1]
        x2 = center_x + selected_boxes[:, 2]
        y2 = center_y + selected_boxes[:, 3]

        selected_points = keypoints[indices] * stride
        point_x = selected_points[:, 0::2] + center_x[:, np.newaxis]
        point_y = selected_points[:, 1::2] + center_y[:, np.newaxis]

        # Undo the letterbox so every coordinate refers to the original image.
        pad_x = letterboxed.pad_x
        pad_y = letterboxed.pad_y
        scale = letterboxed.scale

        x1 = (x1 - pad_x) / scale
        y1 = (y1 - pad_y) / scale
        x2 = (x2 - pad_x) / scale
        y2 = (y2 - pad_y) / scale
        point_x = (point_x - pad_x) / scale
        point_y = (point_y - pad_y) / scale

        detections: list[DetectedFace] = []

        for i in range(indices.shape[0]):
            detections.append(
                DetectedFace(
                    box=FaceBox(
                        float(x1[i]),
                        float(y1[i]),
                        float(x2[i] - x1[i]),
                        float(y2[i] - y1[i]),
                    ),
                    landmarks=FaceLandmarks(
                        left_eye=(float(point_x[i, 0]), float(point_y[i, 0])),
                        right_eye=(float(point_x[i, 1]), float(point_y[i, 1])),
                        nose=(float(point_x[i, 2]), float(point_y[i, 2])),
                        mouth_left=(float(point_x[i, 3]), float(point_y[i, 3])),
                        mouth_right=(float(point_x[i, 4]), float(point_y[i, 4])),
                    ),
                    score=float(scores[indices[i]]),
                )
            )

        return detections


def _non_maximum_suppression(
    faces: list[DetectedFace], iou_threshold: float
) -> list[DetectedFace]:
    kept: list[DetectedFace] = []

    for face in sorted(faces, key=lambda f: f.score, reverse=True):
        if not any(_intersection_over_union(k.box, face.box) > iou_threshold for k in kept):
            kept.append(face)

    return kept


def _intersection_over_union(a: FaceBox, b: FaceBox) -> float:
    x1 = max(a.x, b.x)
    y1 = max(a.y, b.y)
    x2 = min(a.right, b.right)
    y2 = min(a.bottom, b.bottom)

    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = a.area + b.area - intersection

    return 0.0 if union <= 0.0 else intersection / union
