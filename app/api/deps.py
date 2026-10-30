"""Request-scoped dependencies: the session, the tenant, the API key roles, the services.

The recognition provider and the image store are process-wide singletons held on
``app.state``: an inference session is expensive to build and safe to share across threads,
and per-request sessions would exhaust memory immediately.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings, get_settings
from app.core.errors import FaceError, FaceProcessingException
from app.core.faces import FaceImage
from app.core.security import (
    ADMIN_ROLE,
    API_KEY_HEADER,
    DEVICE_ROLE,
    TENANT_HEADER,
    match_api_key,
    normalize_tenant_id,
)
from app.db.session import get_session_factory
from app.providers.base import FaceRecognitionProvider
from app.repositories.attendance_repository import AttendanceRepository
from app.repositories.face_template_repository import FaceTemplateRepository
from app.repositories.person_repository import PersonRepository
from app.services.attendance import AttendanceService
from app.services.enrollment import EnrollmentService
from app.services.validator import FaceCaptureValidator
from app.storage.base import FaceImageStore

#: Chunk size for reading an upload. Matches the original 80KB buffer.
_READ_CHUNK = 81_920


def get_app_settings(request: Request) -> Settings:
    """The settings this app was built with.

    Read from ``app.state`` rather than the process-wide cache so that an app constructed with
    an explicit ``Settings`` - a test, or a second app in one process - uses those values
    everywhere, API keys included.
    """
    return getattr(request.app.state, "settings", None) or get_settings()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()

    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_provider(request: Request) -> FaceRecognitionProvider:
    return request.app.state.recognition_provider


ProviderDep = Annotated[FaceRecognitionProvider, Depends(get_provider)]


def get_image_store(request: Request) -> FaceImageStore:
    return request.app.state.image_store


ImageStoreDep = Annotated[FaceImageStore, Depends(get_image_store)]


def get_clock(request: Request) -> Clock:
    return request.app.state.clock


ClockDep = Annotated[Clock, Depends(get_clock)]


def get_tenant_id(request: Request, settings: SettingsDep) -> str:
    """Resolve which organisation a request belongs to.

    Single-site deployments can ignore this entirely and everything lands in the default
    tenant.

    The header is trusted because an API key is already required to reach any endpoint. If you
    later issue keys to parties who must not see each other's data, bind the tenant to the key
    instead of reading it from a header a caller controls.
    """
    header = request.headers.get(TENANT_HEADER)

    if header and header.strip():
        return normalize_tenant_id(header)

    return normalize_tenant_id(settings.default_tenant_id or "default")


TenantDep = Annotated[str, Depends(get_tenant_id)]


def _role_for(request: Request, settings: Settings) -> str:
    presented = request.headers.get(API_KEY_HEADER)

    if presented is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="An API key is required.",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )

    if not presented.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Empty API key."
        )

    role = match_api_key(settings.api_keys, presented)

    if role is None:
        # Deliberately vague: a specific message would help an attacker enumerate keys.
        client = request.client.host if request.client else "unknown"
        import logging

        logging.getLogger(__name__).warning(
            "Rejected an unrecognised API key from %s", client
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unrecognised API key."
        )

    return role


def require_admin(request: Request, settings: SettingsDep) -> str:
    """Full access, including enrolling faces and reading stored images."""
    role = _role_for(request, settings)

    if role != ADMIN_ROLE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires an admin API key.",
        )

    return role


def require_device(request: Request, settings: SettingsDep) -> str:
    """Marking attendance only.

    Kiosk and mobile keys get this; they are the keys most likely to be extracted from a
    device, so they must not be able to enrol a face or read a photo. An admin key can do
    anything a device can, so it satisfies this too.
    """
    role = _role_for(request, settings)

    if role not in (DEVICE_ROLE, ADMIN_ROLE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires a device or admin API key.",
        )

    return role


AdminDep = Annotated[str, Depends(require_admin)]
DeviceDep = Annotated[str, Depends(require_device)]


async def to_face_image(file: UploadFile, settings: Settings) -> FaceImage:
    """Read an upload into a :class:`FaceImage`, refusing anything oversized.

    The declared size is checked first so a hostile 2GB upload is rejected without being read
    into memory, and the copy is then bounded anyway in case the declared length was a lie.
    """
    declared = file.size

    if declared is not None and declared <= 0:
        raise FaceProcessingException(
            FaceError.CorruptImage, f"The uploaded file '{file.filename}' is empty."
        )

    limit = settings.face.max_image_bytes

    if declared is not None and declared > limit:
        raise FaceProcessingException(
            FaceError.ImageTooLarge,
            f"'{file.filename}' is {declared // 1024}KB; the limit is {limit // 1024}KB.",
        )

    buffer = bytearray()
    total = 0

    while True:
        chunk = await file.read(_READ_CHUNK)

        if not chunk:
            break

        total += len(chunk)

        if total > limit:
            raise FaceProcessingException(
                FaceError.ImageTooLarge, "The upload exceeded the size limit while being read."
            )

        buffer.extend(chunk)

    if total == 0:
        raise FaceProcessingException(
            FaceError.CorruptImage, f"The uploaded file '{file.filename}' is empty."
        )

    return FaceImage(bytes(buffer), file.content_type or "application/octet-stream", file.filename)


def get_validator(settings: SettingsDep) -> FaceCaptureValidator:
    return FaceCaptureValidator(settings.face)


ValidatorDep = Annotated[FaceCaptureValidator, Depends(get_validator)]


def get_person_repository(session: SessionDep) -> PersonRepository:
    return PersonRepository(session)


def get_template_repository(session: SessionDep) -> FaceTemplateRepository:
    return FaceTemplateRepository(session)


def get_attendance_repository(session: SessionDep) -> AttendanceRepository:
    return AttendanceRepository(session)


PersonRepoDep = Annotated[PersonRepository, Depends(get_person_repository)]
TemplateRepoDep = Annotated[FaceTemplateRepository, Depends(get_template_repository)]
AttendanceRepoDep = Annotated[AttendanceRepository, Depends(get_attendance_repository)]


def get_enrollment_service(
    session: SessionDep,
    people: PersonRepoDep,
    templates: TemplateRepoDep,
    image_store: ImageStoreDep,
    provider: ProviderDep,
    validator: ValidatorDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> EnrollmentService:
    return EnrollmentService(
        session=session,
        people=people,
        templates=templates,
        image_store=image_store,
        provider=provider,
        validator=validator,
        clock=clock,
        settings=settings.face,
    )


def get_attendance_service(
    session: SessionDep,
    people: PersonRepoDep,
    templates: TemplateRepoDep,
    attendance: AttendanceRepoDep,
    image_store: ImageStoreDep,
    provider: ProviderDep,
    validator: ValidatorDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> AttendanceService:
    return AttendanceService(
        session=session,
        people=people,
        templates=templates,
        attendance=attendance,
        image_store=image_store,
        provider=provider,
        validator=validator,
        clock=clock,
        settings=settings.face,
    )


EnrollmentServiceDep = Annotated[EnrollmentService, Depends(get_enrollment_service)]
AttendanceServiceDep = Annotated[AttendanceService, Depends(get_attendance_service)]
