"""The two expected exception families.

``DomainException`` is "your request was wrong"; ``FaceProcessingException`` is "the image
was not usable". Keeping them apart is what lets the API map each onto the right HTTP shape.
"""

from __future__ import annotations

from enum import Enum
from typing import Sequence


class DomainErrorKind(str, Enum):
    Validation = "Validation"
    NotFound = "NotFound"
    Conflict = "Conflict"
    Forbidden = "Forbidden"


class DomainException(Exception):
    """Application-level failure with an intended HTTP shape."""

    def __init__(self, kind: DomainErrorKind, code: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        #: Stable machine-readable code, e.g. ``person_not_found``.
        self.code = code
        self.message = message

    @staticmethod
    def not_found(code: str, message: str) -> "DomainException":
        return DomainException(DomainErrorKind.NotFound, code, message)

    @staticmethod
    def conflict(code: str, message: str) -> "DomainException":
        return DomainException(DomainErrorKind.Conflict, code, message)

    @staticmethod
    def validation(code: str, message: str) -> "DomainException":
        return DomainException(DomainErrorKind.Validation, code, message)

    @staticmethod
    def forbidden(code: str, message: str) -> "DomainException":
        return DomainException(DomainErrorKind.Forbidden, code, message)


class FaceError(str, Enum):
    NoFaceDetected = "NoFaceDetected"
    MultipleFacesDetected = "MultipleFacesDetected"
    LowQuality = "LowQuality"
    ImageTooSmall = "ImageTooSmall"
    ImageTooLarge = "ImageTooLarge"
    UnsupportedFormat = "UnsupportedFormat"
    CorruptImage = "CorruptImage"
    PoseMismatch = "PoseMismatch"
    DuplicateFrame = "DuplicateFrame"
    DuplicateIdentity = "DuplicateIdentity"
    InconsistentEnrollment = "InconsistentEnrollment"
    ProviderNotConfigured = "ProviderNotConfigured"
    ProviderFailure = "ProviderFailure"


class FaceProcessingException(Exception):
    """Raised for every expected, user-correctable failure in the face pipeline.

    The API maps :attr:`error` onto an HTTP status and a stable machine-readable code so the
    capture screen can react without string matching.
    """

    def __init__(
        self,
        error: FaceError,
        message: str,
        hints: Sequence[str] | None = None,
        inner: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.hints: list[str] = list(hints or [])
        self.inner = inner
