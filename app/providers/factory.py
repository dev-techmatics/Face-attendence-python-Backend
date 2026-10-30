"""Chooses the recognition provider and the image store from configuration."""

from __future__ import annotations

import logging

from app.core.config import Settings
from app.providers.base import FaceRecognitionProvider
from app.storage.base import FaceImageStore

logger = logging.getLogger(__name__)


def build_recognition_provider(settings: Settings) -> FaceRecognitionProvider:
    provider = (settings.face.provider or "onnx").lower()

    if provider == "development":
        # A stub that matches on file name must never be reachable in a real environment: it
        # would accept anybody and stamp it as attendance.
        if not settings.is_development:
            raise RuntimeError(
                "The face provider is 'development', which performs no real face recognition, "
                "but the host environment is not Development. Set the provider to 'onnx' or "
                "'aws'."
            )

        from app.providers.development import DevelopmentFaceRecognitionProvider

        return DevelopmentFaceRecognitionProvider(settings.face)

    if provider == "aws":
        from app.providers.aws import RekognitionFaceRecognitionProvider

        return RekognitionFaceRecognitionProvider(settings.face)

    if provider == "onnx":
        # Built once per process on purpose: an inference session is expensive to build and
        # safe to share across threads. Per-request sessions would exhaust memory immediately.
        from app.providers.onnx.provider import OnnxFaceRecognitionProvider

        return OnnxFaceRecognitionProvider(settings.face)

    raise RuntimeError(
        f"Unknown face provider '{settings.face.provider}'. Valid values: onnx, aws, "
        "development."
    )


def build_image_store(settings: Settings) -> FaceImageStore:
    if (settings.storage.provider or "local").lower() == "s3":
        from app.storage.s3 import S3FaceImageStore

        return S3FaceImageStore(settings.storage)

    from app.storage.local import LocalFileFaceImageStore

    return LocalFileFaceImageStore(settings.storage)
