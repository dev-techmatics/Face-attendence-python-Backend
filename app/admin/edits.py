"""Correcting the attendance record by hand. Admin only.

The register is evidence, and this is the one place that can change it. Three things follow
from that, and none of them is optional:

* **Every change writes an audit row**, before the change is committed, in the same
  transaction. If the audit cannot be written the edit does not happen.
* **A reason is required.** Six months on, an edit with no stated reason is
  indistinguishable from a mistake, and the figure it produced cannot be defended.
* **A hand-made punch never looks like a face match.** It is recorded with ``device_id``
  of ``manual`` and a match score of nought, so every screen that shows a punch shows the
  difference without being taught to.

What can be changed is deliberately narrow: the **direction** and the **time** of a punch
that was actually made. Those are the real corrections - somebody tapped the wrong button,
or the clock was out.

Adding a punch nobody made was built and then removed at the user's request. Worth knowing
if it is ever wanted back: the hard part was not the form, it was that an invented punch
must never read as a face the camera matched, which is why anything of the sort has to
carry its own device name and a score of nought.

The **decision** is not editable, and should not become so. Turning a rejection into an
acceptance would fabricate an attendance that no camera ever saw, which is the one thing
this whole service exists to make impossible. If a rejected attempt was really that person,
the correct repair is to add a punch and say so in the reason - which leaves both the
refusal and the correction on the record.

Who made the change is recorded as a role, because the dashboard has shared logins. The
column for a name exists and is empty. That is the known limit of this feature and the
reason to add named accounts before anybody leans on it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app.admin.router import ADMIN, AdminOnly
from app.admin.router import templates as _shared_templates
from app.api.deps import SessionDep, TenantDep
from app.core.enums import AttendanceDecision, AttendanceDirection
from app.domain.models import AttendanceEdit, AttendanceEvent, Person

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/attendance", tags=["dashboard"], include_in_schema=False)

#: Long enough to say what happened, short enough that nobody writes an essay instead of a
#: reason.
MIN_REASON = 4
MAX_REASON = 500


def _audit(
    request: Request,
    event: AttendanceEvent,
    field: str,
    old: str | None,
    new: str | None,
    reason: str,
    now: datetime,
) -> AttendanceEdit:
    return AttendanceEdit(
        tenant_id=event.tenant_id,
        attendance_event_id=event.id,
        field=field,
        old_value=old,
        new_value=new,
        reason=reason,
        changed_by_role=ADMIN,
        changed_by=None,
        changed_from=request.client.host if request.client else None,
        changed_at_utc=now,
    )


def _back(request: Request) -> RedirectResponse:
    """Return to the attempt log the editor came from, filters and all."""
    came_from = request.headers.get("referer", "")
    target = came_from if "/admin/register" in came_from else "/admin/register"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


async def _load(session, tenant: str, event_id: int) -> AttendanceEvent:
    event = (
        await session.execute(
            select(AttendanceEvent).where(
                AttendanceEvent.id == event_id, AttendanceEvent.tenant_id == tenant
            )
        )
    ).scalars().first()

    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no such attendance row.")

    return event


@router.get("/{event_id}/edit", response_class=HTMLResponse)
async def edit_form(
    request: Request,
    event_id: int,
    session: SessionDep,
    tenant: TenantDep,
    _: AdminOnly,
) -> Response:
    event = await _load(session, tenant, event_id)

    person = None

    if event.person_id is not None:
        person = (
            await session.execute(select(Person).where(Person.id == event.person_id))
        ).scalars().first()

    history = (
        (
            await session.execute(
                select(AttendanceEdit)
                .where(AttendanceEdit.attendance_event_id == event_id)
                .order_by(AttendanceEdit.changed_at_utc.desc())
            )
        )
        .scalars()
        .all()
    )

    return _shared_templates.TemplateResponse(
        request=request,
        name="edit_attendance.html",
        context={
            "page": "register",
            "title": "Correct a punch",
            "show_nav": True,
            "is_admin": True,
            "role": ADMIN,
            "event": event,
            "person": person,
            "history": list(history),
            "error": None,
        },
    )


@router.post("/{event_id}/edit")
async def apply_edit(
    request: Request,
    event_id: int,
    session: SessionDep,
    tenant: TenantDep,
    _: AdminOnly,
    direction: Annotated[str, Form()] = "",
    happened_at: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    event = await _load(session, tenant, event_id)
    reason = reason.strip()

    async def refuse(message: str) -> Response:
        person = None

        if event.person_id is not None:
            person = (
                await session.execute(select(Person).where(Person.id == event.person_id))
            ).scalars().first()

        return _shared_templates.TemplateResponse(
            request=request,
            name="edit_attendance.html",
            context={
                "page": "register",
                "title": "Correct a punch",
                "show_nav": True,
                "is_admin": True,
                "role": ADMIN,
                "event": event,
                "person": person,
                "history": [],
                "error": message,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not MIN_REASON <= len(reason) <= MAX_REASON:
        return await refuse(
            "Say why this is being changed. It is the only thing that explains the figure "
            "afterwards."
        )

    try:
        wanted_direction = AttendanceDirection(direction)
    except ValueError:
        return await refuse("Choose check-in or check-out.")

    local_zone = datetime.now().astimezone().tzinfo

    try:
        # The form sends local wall-clock time, which is what the admin is reading off the
        # screen. Stored as UTC like every other timestamp.
        wanted_at = datetime.fromisoformat(happened_at).replace(tzinfo=local_zone)
    except ValueError:
        return await refuse("That is not a valid date and time.")

    now = datetime.now(local_zone)

    if wanted_at > now:
        return await refuse("A punch cannot be in the future.")

    changes: list[AttendanceEdit] = []
    was = AttendanceDirection(event.direction)

    if wanted_direction != was:
        changes.append(
            _audit(request, event, "direction", was.name, wanted_direction.name, reason, now)
        )

    if wanted_at != event.created_at_utc.astimezone(local_zone):
        changes.append(
            _audit(
                request,
                event,
                "time",
                event.created_at_utc.astimezone(local_zone).strftime("%Y-%m-%d %H:%M"),
                wanted_at.strftime("%Y-%m-%d %H:%M"),
                reason,
                now,
            )
        )

    if not changes:
        return await refuse("Nothing was changed.")

    # Audit rows and the change itself go in together. An edit that is not accounted for is
    # worse than an edit that did not happen.
    for change in changes:
        session.add(change)

    event.direction = wanted_direction
    event.created_at_utc = wanted_at
    event.edited_at_utc = now

    await session.commit()

    logger.warning(
        "Attendance %s edited by %s from %s: %s",
        event_id,
        ADMIN,
        request.client.host if request.client else "unknown",
        "; ".join(f"{c.field} {c.old_value} -> {c.new_value}" for c in changes),
    )

    return _back(request)
