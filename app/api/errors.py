"""Turns the two expected exception families into RFC 9457 problem responses.

Each carries a stable machine-readable code, so a capture screen can branch on the code
instead of parsing prose.
"""

from __future__ import annotations

import logging
import re
import uuid

from fastapi import FastAPI, Request, status
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse

from app.core.errors import DomainErrorKind, DomainException, FaceError, FaceProcessingException

logger = logging.getLogger(__name__)

#: 499 is the nginx convention for a client that went away; HTTP has no such code.
CLIENT_CLOSED_REQUEST = 499

PROBLEM_JSON = "application/problem+json"

_PASCAL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def to_snake_case(value: str) -> str:
    """``NoFaceDetected`` becomes ``no_face_detected``, so clients get a stable code."""
    return _PASCAL_BOUNDARY.sub("_", value).lower()


def status_for(error: FaceError) -> int:
    match error:
        case FaceError.ImageTooLarge:
            return status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        case FaceError.UnsupportedFormat:
            return status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
        # Conflicts, not bad requests: the upload was fine, it collided with existing state.
        case FaceError.DuplicateIdentity | FaceError.DuplicateFrame:
            return status.HTTP_409_CONFLICT
        # The caller cannot fix a missing model file by changing the request.
        case FaceError.ProviderNotConfigured:
            return status.HTTP_503_SERVICE_UNAVAILABLE
        case FaceError.ProviderFailure:
            return status.HTTP_502_BAD_GATEWAY
        case _:
            return status.HTTP_422_UNPROCESSABLE_ENTITY


def _problem(
    request: Request,
    http_status: int,
    code: str,
    title: str,
    detail: str,
    hints: list[str] | None = None,
) -> JSONResponse:
    trace_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex

    return JSONResponse(
        status_code=http_status,
        media_type=PROBLEM_JSON,
        content={
            "type": "about:blank",
            "title": title,
            "status": http_status,
            "detail": detail,
            "code": code,
            "hints": hints or [],
            "traceId": trace_id,
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(FaceProcessingException)
    async def _face(request: Request, exc: FaceProcessingException) -> JSONResponse:
        http_status = status_for(exc.error)

        _log(request, http_status, to_snake_case(exc.error.value), exc)

        # Curated messages are shown whatever the status code. A 503 saying "detector model
        # not found at <path>" is exactly what the operator needs; replacing it with "an
        # unexpected error occurred" turns a two-minute fix into an afternoon of log
        # archaeology.
        return _problem(
            request,
            http_status,
            to_snake_case(exc.error.value),
            "The image could not be used",
            exc.message,
            exc.hints,
        )

    @app.exception_handler(DomainException)
    async def _domain(request: Request, exc: DomainException) -> JSONResponse:
        http_status = {
            DomainErrorKind.NotFound: status.HTTP_404_NOT_FOUND,
            DomainErrorKind.Conflict: status.HTTP_409_CONFLICT,
            DomainErrorKind.Forbidden: status.HTTP_403_FORBIDDEN,
        }.get(exc.kind, status.HTTP_400_BAD_REQUEST)

        _log(request, http_status, exc.code, exc)

        return _problem(
            request,
            http_status,
            exc.code,
            "The request could not be completed",
            exc.message,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        hints = [
            f"{'.'.join(str(p) for p in error.get('loc', [])[1:])}: {error.get('msg')}"
            for error in exc.errors()
        ]

        _log(request, status.HTTP_400_BAD_REQUEST, "malformed_request", exc)

        return _problem(
            request,
            status.HTTP_400_BAD_REQUEST,
            "malformed_request",
            "The request could not be read",
            "One or more fields were missing or of the wrong type.",
            hints,
        )

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException) -> JSONResponse:
        code = {
            status.HTTP_401_UNAUTHORIZED: "unauthorized",
            status.HTTP_403_FORBIDDEN: "forbidden",
            status.HTTP_404_NOT_FOUND: "not_found",
            status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
        }.get(exc.status_code, f"http_{exc.status_code}")

        _log(request, exc.status_code, code, exc)

        response = _problem(
            request,
            exc.status_code,
            code,
            "The request could not be completed",
            str(exc.detail),
        )

        for header, value in (exc.headers or {}).items():
            response.headers[header] = value

        return response

    # A cancelled request gets no handler: asyncio.CancelledError is a BaseException, and a
    # client that has already disconnected cannot receive the 499 anyway. Starlette unwinds
    # the request itself; CLIENT_CLOSED_REQUEST is kept for access logs that want the code.

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled exception on %s %s", request.method, request.url.path
        )

        # Anything unrecognised gets a generic message: stack traces and connection strings
        # have a habit of ending up in exception text.
        return _problem(
            request,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "Something went wrong",
            "An unexpected error occurred. The incident has been logged.",
        )


def _log(request: Request, http_status: int, code: str, exc: BaseException) -> None:
    if http_status >= 500:
        logger.error(
            "Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc
        )
    else:
        logger.info(
            "%s on %s %s: %s", code, request.method, request.url.path, exc
        )
