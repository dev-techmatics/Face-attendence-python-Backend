"""What a member of staff sees about themselves.

One page: are you in or out, hours this week and this month, and the last fortnight day by
day. Nothing else, because this is the thing somebody checks on their phone between tasks.

**Signing in is done with a photo, not a password.** For forty or fifty people, passwords
mean forty or fifty handovers, a reset request every time somebody forgets, and a list of
credentials somebody has to keep. The recognition engine is already here and already trusted
to decide who is standing at the kiosk, so it decides who is looking at the page too. Nothing
to hand out and nothing to forget.

The photo is taken with an ordinary file input marked ``capture="user"``, which opens the
camera on both iOS and Android and needs no JavaScript and no camera API. That matters: the
page has to work on a self-signed certificate, where the browser APIs that require a fully
trusted secure context are not available.

What this costs, stated plainly: a photograph of somebody else would let you read that
person's hours. The page is read-only, shows one person's own record, and is reachable only
from the office network, so the worst case is a colleague seeing another colleague's working
hours. If that is not acceptable, the fix is a PIN alongside the photo — say so and it is a
small change. It is not the same risk as the kiosk itself, where a face writes a record.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.admin import queries
from app.api.deps import (
    AttendanceServiceDep,
    PersonRepoDep,
    SessionDep,
    SettingsDep,
    TenantDep,
    to_face_image,
)
from app.core.errors import FaceError, FaceProcessingException
from app.core.templating import TEMPLATE_DIR, register_filters

logger = logging.getLogger(__name__)

router = APIRouter(tags=["my attendance"], include_in_schema=False)

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
register_filters(templates)

#: Who is looking. Separate from the HR session key so one can never be mistaken for the
#: other: an HR sign-in does not make you a staff member, and a staff sign-in grants nothing
#: beyond your own record.
SESSION_PERSON = "person_id"
SESSION_SINCE = "person_since"


def _signed_in_person(request: Request, settings) -> int | None:
    person_id = request.session.get(SESSION_PERSON)
    since = request.session.get(SESSION_SINCE)

    if not person_id or not since:
        return None

    try:
        when = datetime.fromisoformat(since)
    except (TypeError, ValueError):
        return None

    from datetime import timedelta

    if datetime.now(when.tzinfo) - when > timedelta(hours=settings.session_hours):
        return None

    return int(person_id)


@router.get("/me", response_class=HTMLResponse)
async def my_page(
    request: Request,
    session: SessionDep,
    tenant: TenantDep,
    settings: SettingsDep,
) -> Response:
    person_id = _signed_in_person(request, settings)

    if person_id is None:
        return templates.TemplateResponse(
            request=request,
            name="staff_login.html",
            context={"title": "My attendance", "show_nav": False, "page": "login", "error": None},
        )

    record = await queries.my_attendance(
        session,
        tenant,
        person_id,
        expiry_hours=settings.face.open_shift_expiry_hours,
    )

    if record is None:
        # The person was removed while their session was still valid.
        request.session.pop(SESSION_PERSON, None)
        return RedirectResponse("/me", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request=request,
        name="staff.html",
        context={
            "title": record.full_name,
            "show_nav": False,
            "page": "me",
            "me": record,
            "today": datetime.now().astimezone().date(),
        },
    )


@router.post("/me/signin", response_class=HTMLResponse)
async def sign_in_with_face(
    request: Request,
    attendance: AttendanceServiceDep,
    people: PersonRepoDep,
    tenant: TenantDep,
    settings: SettingsDep,
    photo: Annotated[UploadFile, File()],
) -> Response:
    def refuse(message: str) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="staff_login.html",
            context={
                "title": "My attendance",
                "show_nav": False,
                "page": "login",
                "error": message,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    try:
        image = await to_face_image(photo, settings)
        # Records nothing: looking at your own hours is not a punch, and writing one here
        # would corrupt the register every time somebody checked their total.
        match = await attendance.preview_match(tenant, None, image)
    except FaceProcessingException as exc:
        if exc.error in (FaceError.NoFaceDetected, FaceError.CorruptImage):
            return refuse("No face in that photo. Hold the phone at arm's length and try again.")
        if exc.error in (FaceError.LowQuality, FaceError.MultipleFacesDetected):
            return refuse("Make sure it is just you in the picture, in good light.")
        if exc.error == FaceError.ImageTooLarge:
            return refuse("That photo was too large. Try again.")
        logger.error("Staff sign-in failed: %s", exc.message)
        return refuse("The face service is not responding. Try again in a moment.")

    if not match.is_match or match.person_id is None:
        return refuse("We could not recognise you. Ask HR to check you are registered.")

    # The same near-tie rule the kiosk uses: if two registered people score within a whisker
    # of each other, that is not an identification, and showing the wrong person's hours is
    # exactly the mistake to avoid.
    if match.margin is not None and match.margin < settings.face.identification_margin:
        logger.info("Refused an ambiguous staff sign-in: margin %.3f", match.margin)
        return refuse("We could not recognise you clearly. Try again in better light.")

    person = await people.get_by_id(match.person_id, False)

    if person is None or person.tenant_id != tenant:
        return refuse("We could not recognise you. Ask HR to check you are registered.")

    request.session[SESSION_PERSON] = person.id
    request.session[SESSION_SINCE] = datetime.now().astimezone().isoformat()

    logger.info("Staff sign-in: %s", person.external_id)

    return RedirectResponse("/me", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/me/signout")
async def sign_out(request: Request) -> Response:
    request.session.pop(SESSION_PERSON, None)
    request.session.pop(SESSION_SINCE, None)
    return RedirectResponse("/me", status_code=status.HTTP_303_SEE_OTHER)
