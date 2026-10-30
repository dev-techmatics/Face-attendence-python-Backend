"""The contract the iOS kiosk app already speaks.

The app was written against the earlier CompreFace-backed service and talks to four addresses
under ``/v1``, sends a ``Bearer`` token, and expects every reply to be a flat
``{"success": bool, "message": "<short human text>", ...}`` with HTTP 200 even for a refusal.
This backend's own API is a different shape on every one of those points.

Rather than make an iOS rewrite, a redeploy and a new certificate on every phone a
prerequisite for switching engines, this router translates. The app changes two lines of
configuration; everything else here absorbs the difference.

It is a translation layer, not a second implementation: every request is handed to the same
services the native API uses, so the enrolment rules, the duplicate-punch window and the
audit trail behave identically whichever door a request comes through.

Three places where this deliberately bends to the app rather than the other way round:

* **Side photos are optional.** The old service required only ``face_front`` and added the
  side photos as extra chances to match, ignoring a side it could not use - its own comment
  notes that "a strong turn may hide the face". So a side photo that fails validation is
  skipped here with a warning instead of failing the whole registration. The front photo
  must still succeed.
* **The app decides check-in vs check-out.** The native API prefers to infer the direction
  from the previous event; here the app's value is passed through, because the app's UI has
  already told the user which one it is doing.
* **Refusals are 200s.** ``{"success": false}`` with text to show on screen is what the app
  parses. A 4xx would surface to the user as "couldn't reach the server", which is both
  wrong and unhelpful.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.api.deps import (
    AttendanceServiceDep,
    EnrollmentServiceDep,
    PersonRepoDep,
    SessionDep,
    SettingsDep,
    TenantDep,
    to_face_image,
)
from app.core.config import Settings
from app.core.enums import AttendanceDecision, AttendanceDirection, EnrollmentStatus, FacePose
from app.core.errors import DomainException, FaceError, FaceProcessingException
from app.core.faces import FaceImage
from app.core.security import ADMIN_ROLE, DEVICE_ROLE, match_api_key
from app.domain.models import Person
from app.services.commands import CaptureFaceCommand, CreatePersonCommand, MarkAttendanceCommand

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["kiosk app (compatibility)"])

#: The app's own validation, mirrored so the message it shows matches what it used to show.
EMPLOYEE_ID = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,39}$")

NO_MATCH = "We couldn't match your face. New here? Tap Register."


def ok(message: str, **extra) -> JSONResponse:
    return JSONResponse({"success": True, "message": message, **extra})


def no(message: str, http_status: int = status.HTTP_200_OK) -> JSONResponse:
    """A refusal the app can show on screen.

    200 by default: the request was understood and answered, it just did not succeed. The app
    reads ``success``, and turning this into a 4xx would make it say "couldn't reach the
    server".
    """
    return JSONResponse({"success": False, "message": message}, status_code=http_status)


def require_kiosk(request: Request, settings: SettingsDep) -> str:
    """``Authorization: Bearer <key>``, which is what the app sends.

    The native API uses an ``X-Api-Key`` header; the keys themselves are the same, so a kiosk
    keeps its device key and gains nothing extra by coming in through this door.
    """
    header = request.headers.get("authorization", "")
    presented = header[7:].strip() if header[:7].lower() == "bearer " else header.strip()

    role = match_api_key(settings.api_keys, presented) if presented else None

    if role not in (DEVICE_ROLE, ADMIN_ROLE):
        client = request.client.host if request.client else "unknown"
        logger.warning("Rejected an unrecognised kiosk token from %s", client)
        # The app shows this verbatim, and it is the message it showed before.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This device isn't authorised. Contact your admin.",
        )

    return role


KioskDep = Annotated[str, Depends(require_kiosk)]


def normalise_employee_id(value: str) -> str:
    return value.strip().upper()


@router.get("/health", summary="Kiosk health check")
async def health(session: SessionDep, tenant: TenantDep) -> JSONResponse:
    """Anonymous, like the native /health: a kiosk shows this before anyone has signed in."""
    try:
        enrolled = (
            await session.execute(
                select(func.count())
                .select_from(Person)
                .where(
                    Person.tenant_id == tenant,
                    Person.enrollment_status == EnrollmentStatus.Enrolled,
                )
            )
        ).scalar_one()
    except Exception:
        logger.exception("Kiosk health check could not reach the database")
        return JSONResponse(
            {"success": False, "message": "face service unreachable", "employees": 0}
        )

    return JSONResponse({"success": True, "message": "ok", "employees": int(enrolled)})


@router.post("/faces/enroll", summary="Register a face (app contract)")
async def enroll(
    enrollment: EnrollmentServiceDep,
    people: PersonRepoDep,
    session: SessionDep,
    tenant: TenantDep,
    settings: SettingsDep,
    _: KioskDep,
    employee_id: Annotated[str, Form()],
    full_name: Annotated[str, Form()],
    face_front: Annotated[UploadFile, File()],
    face_left: Annotated[UploadFile | None, File()] = None,
    face_right: Annotated[UploadFile | None, File()] = None,
) -> JSONResponse:
    employee_id = normalise_employee_id(employee_id)
    full_name = " ".join(full_name.split())

    if not EMPLOYEE_ID.match(employee_id):
        return no("Employee ID can use letters, numbers, dot, dash or underscore only.")

    if not 2 <= len(full_name) <= 80:
        return no("Please enter the full name.")

    if await people.external_id_exists(tenant, employee_id):
        return no(f"ID {employee_id} is already registered. Ask the admin to reset it.")

    try:
        front = await to_face_image(face_front, settings)
    except FaceProcessingException as exc:
        return _enrol_message(exc, FacePose.Front)

    # The front photo decides whether the registration happens at all: it is the pose the
    # duplicate-identity check is most reliable on, and the one every later match leans on.
    try:
        person = await enrollment.create_person(
            CreatePersonCommand(tenant_id=tenant, external_id=employee_id, full_name=full_name)
        )
    except DomainException as exc:
        return no(f"ID {employee_id} is already registered. Ask the admin to reset it."
                  if exc.code == "external_id_exists" else exc.message)

    try:
        await enrollment.capture_face(
            CaptureFaceCommand(tenant, person.id, FacePose.Front, front)
        )
    except (FaceProcessingException, DomainException) as exc:
        # Nothing usable was stored, so the id must not stay occupied by an unusable record.
        await _discard(enrollment, people, session, tenant, person)
        return (
            _enrol_message(exc, FacePose.Front)
            if isinstance(exc, FaceProcessingException)
            else no(exc.message)
        )

    # Side photos are a bonus. The old service ignored a side it could not use, and the app
    # relies on that: it offers them but does not guarantee a usable turn.
    skipped: list[str] = []

    for pose, upload in ((FacePose.Left, face_left), (FacePose.Right, face_right)):
        if upload is None or not upload.filename:
            continue

        try:
            image = await to_face_image(upload, settings)
            await enrollment.capture_face(CaptureFaceCommand(tenant, person.id, pose, image))
        except (FaceProcessingException, DomainException) as exc:
            # A duplicate identity on a side photo is a real conflict and must not be
            # swallowed: it means this face already belongs to somebody else.
            if isinstance(exc, FaceProcessingException) and exc.error == FaceError.DuplicateIdentity:
                await _discard(enrollment, people, session, tenant, person)
                return _enrol_message(exc, pose)

            logger.info(
                "Skipped the %s photo for %s: %s", pose.name, employee_id, exc
            )
            skipped.append(pose.name.lower())

    if skipped:
        logger.info("Registered %s using the front photo only", employee_id)

    return ok("Face registered.")


@router.post("/faces/identify", summary="Identify a face without recording anything")
async def identify(
    attendance: AttendanceServiceDep,
    people: PersonRepoDep,
    tenant: TenantDep,
    settings: SettingsDep,
    _: KioskDep,
    face: Annotated[UploadFile, File()],
) -> JSONResponse:
    """Who is this? Records nothing - the app calls this first, then punches.

    Reachable with a device key on purpose. The native equivalent is admin-only, and leaving
    it that way would have forced an admin key onto every kiosk - a key that can download
    everybody's stored photographs.
    """
    try:
        image = await to_face_image(face, settings)
        match = await attendance.preview_match(tenant, None, image)
    except FaceProcessingException as exc:
        if exc.error in (FaceError.NoFaceDetected, FaceError.CorruptImage):
            return no("No face found. Please look at the camera.")
        if exc.error in (FaceError.LowQuality, FaceError.MultipleFacesDetected):
            return no("Please stand closer, with just you in the frame, and try again.")
        logger.error("Identify failed: %s", exc.message)
        return no("Face service is not responding. Please try again.", 503)

    if not match.is_match or match.person_id is None:
        return no(NO_MATCH)

    # A win that barely beats the runner-up is a coin toss between two people, not an
    # identification. The native punch path applies the same rule.
    if match.margin is not None and match.margin < settings.face.identification_margin:
        logger.info(
            "Refused an ambiguous identification: margin %.3f", match.margin
        )
        return no(NO_MATCH)

    person = await people.get_by_id(match.person_id, False)

    if person is None or person.tenant_id != tenant:
        return no(NO_MATCH)

    return ok("Matched.", employee_id=person.external_id, name=person.full_name)


@router.post("/attendance/punch", summary="Record a check-in or check-out")
async def punch(
    attendance: AttendanceServiceDep,
    tenant: TenantDep,
    settings: SettingsDep,
    _: KioskDep,
    employee_id: Annotated[str, Form()],
    type: Annotated[str, Form()],  # noqa: A002 - the app sends this field name
    captured_at: Annotated[str, Form()],
    face: Annotated[UploadFile, File()],
    latitude: Annotated[float | None, Form()] = None,
    longitude: Annotated[float | None, Form()] = None,
) -> JSONResponse:
    if type not in ("check_in", "check_out"):
        return no("Unknown attendance type.")

    try:
        taken = datetime.fromisoformat(captured_at)
    except ValueError:
        return no("Invalid time. Please try again.")

    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=timezone.utc)

    direction = (
        AttendanceDirection.In if type == "check_in" else AttendanceDirection.Out
    )

    try:
        image = await to_face_image(face, settings)
        result = await attendance.mark(
            MarkAttendanceCommand(
                tenant_id=tenant,
                external_id=normalise_employee_id(employee_id),
                image=image,
                direction=direction,
                device_id="kiosk",
                latitude=latitude,
                longitude=longitude,
                captured_at_utc=taken.astimezone(timezone.utc),
            )
        )
    except DomainException as exc:
        if exc.code == "person_not_found":
            return no("This employee isn't registered. Please register again.")
        return no(exc.message)
    except FaceProcessingException as exc:
        return _punch_message_for_face_error(exc)

    if result.decision == AttendanceDecision.Accepted:
        return ok("Checked in." if type == "check_in" else "Checked out.")

    return no(_punch_message_for_decision(result.decision, type))


# ---- message mapping ---------------------------------------------------------------------
# The app shows these strings to whoever is standing at the kiosk, so they say what to do
# next rather than naming a decision code.


def _punch_message_for_decision(decision: AttendanceDecision, kind: str) -> str:
    """What to show the person standing at the kiosk, in their words rather than a code."""
    match decision:
        case AttendanceDecision.Duplicate:
            # Deliberately does not name a direction. This fires when *any* punch was
            # accepted inside the window, so after a check-in the next attempt is collapsed
            # whichever way it was going - saying "you have already checked out" to someone
            # who just checked in would be a lie.
            return "Your attendance was already recorded a moment ago."
        case AttendanceDecision.NotEnrolled:
            return "This employee isn't registered. Please register again."
        case AttendanceDecision.NoFaceDetected:
            return "No face found. Please look at the camera."
        case AttendanceDecision.LowQuality:
            return "The photo was too dark or blurry. Please try again."
        case AttendanceDecision.ProviderError:
            return "Face service is not responding. Please try again."
        case _:
            return NO_MATCH


def _punch_message_for_face_error(exc: FaceProcessingException) -> JSONResponse:
    if exc.error in (FaceError.NoFaceDetected, FaceError.CorruptImage):
        return no("No face found. Please look at the camera.")
    if exc.error == FaceError.ImageTooLarge:
        return no("The photo was too large. Please try again.", 413)
    if exc.error in (FaceError.LowQuality, FaceError.MultipleFacesDetected):
        return no("The photo was too dark or blurry. Please try again.")
    if exc.error == FaceError.ProviderNotConfigured:
        return no("Face service is not responding. Please try again.", 503)
    return no("Something went wrong with that photo. Please try again.")


def _enrol_message(exc: FaceProcessingException, pose: FacePose) -> JSONResponse:
    where = "front" if pose == FacePose.Front else pose.name.lower()

    match exc.error:
        case FaceError.NoFaceDetected | FaceError.CorruptImage:
            return no(f"No face found in the {where} photo. Please try again.")
        case FaceError.MultipleFacesDetected:
            return no("More than one face in the photo. Only the person registering should be in frame.")
        case FaceError.DuplicateIdentity:
            return no("This face is already registered under another ID.")
        case FaceError.DuplicateFrame:
            return no("Those photos are the same picture. Please take a separate photo for each angle.")
        case FaceError.PoseMismatch:
            return no(f"Please look straight at the camera for the {where} photo.")
        case FaceError.LowQuality:
            return no(f"The {where} photo was too dark, blurry or far away. Please try again.")
        case FaceError.ImageTooLarge:
            return no("The photo was too large. Please try again.", 413)
        case FaceError.UnsupportedFormat:
            return no("That file isn't a photo. Please try again.", 415)
        case FaceError.ProviderNotConfigured:
            return no("Face service is not responding. Please try again.", 503)
        case _:
            return no(f"Could not use the {where} photo. Please try again.")


async def _discard(enrollment, people, session, tenant: str, person: Person) -> None:
    """Undo a registration that could not store a usable front photo.

    A person row holding no usable face is worse than no row at all: it occupies the employee
    id and reads as registered in a list.
    """
    try:
        await enrollment.purge_biometrics(tenant, person.id)
        await people.remove(person)
        await session.commit()
    except Exception:
        logger.exception(
            "Could not roll back the partial registration for %s; it may need manual removal",
            person.id,
        )
