"""Adding and removing people from the dashboard.

Everything here writes, which is why it is a separate module from the rest of the dashboard:
the screens that only read stay honest about reading only, and the handful that change the
database are all in one file somebody can audit in a sitting.

Three operations, and the difference between the middle one and the last one is the whole
design:

* **Add** registers somebody from a photograph, exactly as a kiosk would. It is for the
  person who cannot get to a kiosk, not a replacement for one.
* **Remove** archives. They stop being able to clock in, they leave the Today board, and
  every hour they ever worked is still there. It is reversible with one click, because the
  common reason to click it - somebody left - is also the one most likely to be undone when
  they come back, or clicked on the wrong row.
* **Delete permanently** erases the person, their face data and their whole attendance
  history. It is the privacy-law erasure, and it is the only one that cannot be undone, so
  it asks for the employee ID to be typed out before it will do anything.

Only the last one needs that friction. Making all three equally annoying would train people
to click through the warning that matters.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete as sql_delete
from sqlalchemy import select

from app.admin import queries
from app.admin.router import SignedIn
from app.api.deps import (
    EnrollmentServiceDep,
    ImageStoreDep,
    PersonRepoDep,
    SessionDep,
    SettingsDep,
    TenantDep,
    to_face_image,
)
from app.api.routers.compat_v1 import EMPLOYEE_ID, normalise_employee_id
from app.core.enums import EnrollmentStatus, FacePose, PersonStatus
from app.core.errors import DomainException, FaceProcessingException
from app.core.templating import TEMPLATE_DIR, register_filters
from app.domain.models import AttendanceEvent
from app.services.commands import CaptureFaceCommand, CreatePersonCommand

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/people", tags=["dashboard"], include_in_schema=False)

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
register_filters(templates)

#: Carries one sentence across the redirect that follows every write, so the page the user
#: lands on can say what just happened. Popped by the People screen.
FLASH = "flash"

PEOPLE = "/admin/people"


def say(request: Request, message: str, kind: str = "ok") -> None:
    request.session[FLASH] = {"message": message, "kind": kind}


def back() -> RedirectResponse:
    """Redirect after a write, so a refresh re-reads the list instead of repeating the write."""
    return RedirectResponse(PEOPLE, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/add")
async def add_person(
    request: Request,
    enrollment: EnrollmentServiceDep,
    people: PersonRepoDep,
    session: SessionDep,
    tenant: TenantDep,
    settings: SettingsDep,
    _: SignedIn,
    employee_id: Annotated[str, Form()] = "",
    full_name: Annotated[str, Form()] = "",
    department: Annotated[str, Form()] = "",
    photo: UploadFile | None = File(default=None),
) -> Response:
    """Register somebody here instead of at a kiosk, from one photograph.

    The same service the kiosk calls, so the rules about duplicate faces and unusable photos
    apply identically - there is no second, laxer way into the database.
    """
    employee_id = normalise_employee_id(employee_id)
    full_name = " ".join(full_name.split())
    department = department.strip() or None

    if not EMPLOYEE_ID.match(employee_id):
        say(request, "Employee ID can use letters, numbers, dot, dash or underscore only.", "bad")
        return back()

    if not 2 <= len(full_name) <= 80:
        say(request, "Please enter the person's full name.", "bad")
        return back()

    if photo is None or not photo.filename:
        say(request, "A photo of their face is needed to register them.", "bad")
        return back()

    if await people.external_id_exists(tenant, employee_id):
        say(request, f"{employee_id} is already on the list.", "bad")
        return back()

    try:
        image = await to_face_image(photo, settings)
    except FaceProcessingException as exc:
        say(request, exc.message, "bad")
        return back()

    try:
        person = await enrollment.create_person(
            CreatePersonCommand(
                tenant_id=tenant,
                external_id=employee_id,
                full_name=full_name,
                department=department,
            )
        )
    except DomainException as exc:
        say(request, exc.message, "bad")
        return back()

    try:
        await enrollment.capture_face(
            CaptureFaceCommand(tenant, person.id, FacePose.Front, image)
        )
    except (FaceProcessingException, DomainException) as exc:
        # A row holding no usable face is worse than no row: it occupies the employee id and
        # reads as registered. Same reasoning as the kiosk path.
        try:
            await enrollment.purge_biometrics(tenant, person.id)
            await people.remove(person)
            await session.commit()
        except Exception:
            logger.exception(
                "Could not roll back the half-made record for %s; it may need removing by hand",
                employee_id,
            )

        say(request, exc.message, "bad")
        return back()

    fresh = await people.get_by_external_id(tenant, employee_id)
    ready = fresh is not None and fresh.enrollment_status == EnrollmentStatus.Enrolled

    logger.info("%s was added from the dashboard", employee_id)

    if ready:
        say(request, f"{full_name} is registered and can clock in now.")
    else:
        # Honest rather than reassuring: with more than one required pose, one photo is not a
        # finished registration and the person would be turned away at the kiosk.
        say(
            request,
            f"{full_name} was added, but one photo is not enough to finish their "
            "registration. Ask them to register at a kiosk.",
            "bad",
        )

    return back()


@router.post("/{external_id}/archive")
async def archive_person(
    request: Request,
    external_id: str,
    people: PersonRepoDep,
    session: SessionDep,
    tenant: TenantDep,
    _: SignedIn,
) -> Response:
    """Stop somebody clocking in, keeping everything they ever did.

    Archived people are excluded from face matching and from the Today board, so this really
    does stop attendance rather than merely hiding them.
    """
    person = await people.get_by_external_id(tenant, external_id)

    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no such person.")

    person.status = PersonStatus.Archived
    await session.commit()

    logger.info("%s was archived from the dashboard", external_id)
    say(request, f"{person.full_name} has been removed. Their hours are still on record.")
    return back()


@router.post("/{external_id}/restore")
async def restore_person(
    request: Request,
    external_id: str,
    people: PersonRepoDep,
    session: SessionDep,
    tenant: TenantDep,
    _: SignedIn,
) -> Response:
    person = await people.get_by_external_id(tenant, external_id)

    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no such person.")

    person.status = PersonStatus.Active
    await session.commit()

    logger.info("%s was restored from the dashboard", external_id)
    say(request, f"{person.full_name} is back on the list.")
    return back()


@router.get("/{external_id}/delete", response_class=HTMLResponse)
async def confirm_delete(
    request: Request,
    external_id: str,
    session: SessionDep,
    tenant: TenantDep,
    _: SignedIn,
) -> Response:
    """Show exactly what a permanent delete would destroy, before it destroys it."""
    footprint = await queries.person_footprint(session, tenant, external_id)

    if footprint is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no such person.")

    return templates.TemplateResponse(
        request=request,
        name="confirm_delete.html",
        context={
            "page": "people",
            "title": f"Delete {footprint.full_name}",
            "show_nav": True,
            "person": footprint,
            "typed": None,
        },
    )


@router.post("/{external_id}/delete")
async def delete_person(
    request: Request,
    external_id: str,
    enrollment: EnrollmentServiceDep,
    people: PersonRepoDep,
    store: ImageStoreDep,
    session: SessionDep,
    tenant: TenantDep,
    _: SignedIn,
    confirm: Annotated[str, Form()] = "",
) -> Response:
    """Erase a person, their face data and their entire attendance history.

    Files before rows throughout, so an interruption leaves an orphaned image rather than a
    row pointing at an image that is no longer there - the same order the retention sweep
    uses.
    """
    footprint = await queries.person_footprint(session, tenant, external_id)

    if footprint is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no such person.")

    if normalise_employee_id(confirm) != footprint.external_id:
        # Not an error to shout about - they are being careful, which is the point.
        return templates.TemplateResponse(
            request=request,
            name="confirm_delete.html",
            context={
                "page": "people",
                "title": f"Delete {footprint.full_name}",
                "show_nav": True,
                "person": footprint,
                "typed": confirm,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    keys = (
        (
            await session.execute(
                select(AttendanceEvent.probe_image_object_key).where(
                    AttendanceEvent.tenant_id == tenant,
                    AttendanceEvent.person_id == footprint.person_id,
                    AttendanceEvent.probe_image_object_key.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    for object_key in keys:
        await store.delete(object_key)

    # Attendance rows hold the person with ON DELETE NO ACTION, so they have to go first or
    # the delete below is refused by the database.
    await session.execute(
        sql_delete(AttendanceEvent).where(
            AttendanceEvent.tenant_id == tenant,
            AttendanceEvent.person_id == footprint.person_id,
        )
    )
    await session.commit()

    # Provider-side templates and the enrolment photographs behind them.
    await enrollment.purge_biometrics(tenant, footprint.person_id)

    person = await people.get_by_external_id(tenant, external_id)

    if person is not None:
        await people.remove(person)
        await session.commit()

    logger.warning(
        "Permanently deleted %s (%s): %d attendance rows, %d face photos, requested from %s",
        footprint.external_id,
        footprint.full_name,
        footprint.punches,
        footprint.photos,
        request.client.host if request.client else "unknown",
    )

    say(
        request,
        f"{footprint.full_name} and their {footprint.punches} attendance "
        f"record{'' if footprint.punches == 1 else 's'} have been deleted for good.",
    )
    return back()
