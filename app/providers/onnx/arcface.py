"""Turns an aligned 112x112 face crop into an L2-normalised embedding.

Two embeddings of the same person land close together on the unit sphere; cosine similarity
measures how close.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray

from app.core import vector_math
from app.core.config import OnnxProviderSettings, resolve_path
from app.core.errors import FaceError, FaceProcessingException
from app.providers.onnx.aligner import OUTPUT_SIZE


class ArcFaceEmbedder:
    def __init__(self, settings: OnnxProviderSettings) -> None:
        self._settings = settings
        model_path: Path = resolve_path(settings.recognition_model_path)

        if not model_path.is_file():
            raise FaceProcessingException(
                FaceError.ProviderNotConfigured,
                f"Recognition model not found at {model_path}. "
                "Run scripts/download_models.py, or set the face provider to development.",
            )

        session_options = ort.SessionOptions()
        if settings.intra_op_threads > 0:
            session_options.intra_op_num_threads = settings.intra_op_threads

        self._session = ort.InferenceSession(
            str(model_path), sess_options=session_options, providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name

        shape = self._session.get_outputs()[0].shape or []
        last = shape[-1] if shape else None

        #: Embedding length reported by the model, falling back to the usual 512.
        self.dimension: int = last if isinstance(last, int) and last > 0 else 512

    def embed(self, aligned: NDArray[np.uint8]) -> NDArray[np.float32]:
        """Embed an aligned crop.

        Input must already be the canonical 112x112 produced by the aligner; feeding a raw
        crop here is the single most common cause of "recognition does not work".
        """
        height, width = aligned.shape[0], aligned.shape[1]

        if width != OUTPUT_SIZE or height != OUTPUT_SIZE:
            raise RuntimeError(
                f"Expected a {OUTPUT_SIZE}x{OUTPUT_SIZE} aligned crop but received {width}x{height}."
            )

        # ArcFace preprocessing: (pixel - 127.5) / 127.5 into [-1, 1], NCHW.
        tensor = (aligned.astype(np.float32) - 127.5) / 127.5

        # Most InsightFace exports take RGB; a few ONNX-zoo conversions expect BGR. Guessing
        # wrong does not crash, it just quietly degrades every score, so it is a setting.
        if self._settings.recognition_input_bgr:
            tensor = tensor[:, :, ::-1]

        tensor = np.ascontiguousarray(tensor.transpose(2, 0, 1)[np.newaxis, ...])

        outputs = self._session.run(None, {self._input_name: tensor})
        embedding = np.asarray(outputs[0], dtype=np.float32).reshape(-1).copy()

        # Normalising here means every stored template is a unit vector, so comparison is a
        # plain dot product and thresholds stay comparable across captures.
        return vector_math.l2_normalize(embedding)
