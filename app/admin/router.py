"""The attendance dashboard.

Server-rendered pages for whoever runs attendance: who is in today, the full register, who
has not finished registering, where the failures cluster, and a monthly total for payroll.

Deliberately plain. No front-end framework, no CDN, one stylesheet in the base template —
this has to load on the office LAN with no internet, on a phone held at reception, and keep
working when nobody has touched it for a year.

Almost entirely read-only: the pages compute, they do not change anything. The single
exception is deleting a stored face photograph, which is deliberate - somebody has to be able
to erase a picture of a person on request, and that belongs on the screen where the picture is
shown. It removes the image only; the attendance row it belongs to is left alone.

Two decisions worth knowing about:

* **A password, not just the network restriction.** The LAN guard already keeps strangers
  out, but everybody in the office is on that LAN, and this page lists every person's
  movements. An empty ``admin_ui_password`` disables the dashboard entirely rather than
  serving it open, so a stock deployment never exposes it by accident.
* **Face photographs are not shown until asked for.** Every punch stores a photo. Putting
  them in the register table would mean a page that quietly displays every employee's face to
  anyone who opens it, which is the kind of thing a data-protection audit asks about. There is
  a "show" link per row instead, for when somebody is actually investigating a dispute.
"""

from __future__ import annotations

import csv
import hmac
import io
import logging
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from sqlalchemy import select, update

from app.admin import lockout, queries
from app.api.deps import ImageStoreDep, SessionDep, SettingsDep, TenantDep
from app.core.enums import AttendanceDecision, AttendanceDirection, EnrollmentStatus
from app.core.templating import TEMPLATE_DIR, register_filters
from app.domain.models import AttendanceEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["dashboard"], include_in_schema=False)

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
register_filters(templates)

#: Marks a session as signed in. The value is the moment of sign-in, so a session can expire
#: without anything server-side to keep.
SESSION_KEY = "signed_in_at"

REFRESH_SECONDS = 30


# ---- access ------------------------------------------------------------------------------


def dashboard_enabled(settings) -> bool:
    return bool(settings.admin_ui_password)


def require_signed_in(request: Request, settings: SettingsDep) -> None:
    """Redirects to the login page rather than returning an error.

    A person who opens a bookmark should land on a password box, not a JSON document.
    """
    if not dashboard_enabled(settings):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The dashboard is not switched on.",
        )

    signed_in_at = request.session.get(SESSION_KEY)

    if not signed_in_at:
        raise _to_login(request)

    try:
        when = datetime.fromisoformat(signed_in_at)
    except (TypeError, ValueError):
        raise _to_login(request) from None

    if datetime.now(when.tzinfo) - when > timedelta(hours=settings.session_hours):
        request.session.clear()
        raise _to_login(request)


def _to_login(request: Request) -> HTTPException:
    """A 303 dressed as an exception, so a dependency can send someone to the login page."""
    return HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": "/admin/login"},
        detail="Please sign in.",
    )


SignedIn = Annotated[None, Depends(require_signed_in)]


def _page(request: Request, name: str, page: str, title: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={"page": page, "title": title, "show_nav": True, **context},
    )


# ---- login -------------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, settings: SettingsDep) -> Response:
    if not dashboard_enabled(settings):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The dashboard is not switched on.")

    if request.session.get(SESSION_KEY):
        return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"page": "login", "title": "Sign in", "show_nav": False, "error": None},
    )


@router.post("/login", response_class=HTMLResponse)
async def sign_in(
    request: Request,
    settings: SettingsDep,
    password: Annotated[str, Form()] = "",
) -> Response:
    if not dashboard_enabled(settings):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The dashboard is not switched on.")

    client = request.client.host if request.client else "unknown"

    def refuse(message: str) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "page": "login",
                "title": "Sign in",
                "show_nav": False,
                "error": message,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Checked before the password is even looked at, so a locked-out caller learns nothing
    # from how long the answer takes.
    waiting = lockout.seconds_remaining(client)

    if waiting:
        logger.warning("Dashboard sign-in from %s is locked out for %ds", client, waiting)
        return refuse(
            f"Too many wrong passwords. Try again in {max(1, waiting // 60)} minutes."
        )

    # Fixed-time comparison: a plain equality check leaks the password through timing.
    if not hmac.compare_digest(password.encode(), settings.admin_ui_password.encode()):
        logger.warning("Failed dashboard sign-in from %s", client)
        locked_for = lockout.record_failure(client)

        if locked_for:
            return refuse(
                f"Too many wrong passwords. Try again in {locked_for // 60} minutes."
            )

        return refuse("That password is not right.")

    lockout.record_success(client)
    request.session[SESSION_KEY] = datetime.now().astimezone().isoformat()

    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/logout")
async def sign_out(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)


# ---- screens -----------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def today(
    request: Request,
    session: SessionDep,
    tenant: TenantDep,
    _: SignedIn,
) -> Response:
    window = queries.DayWindow.for_date(date.today())
    board = await queries.today_board(session, tenant, window)
    headline = await queries.headline_counts(session, tenant, window)

    counts = {
        "in_now": sum(1 for p in board if p.state == "In"),
        "left": sum(1 for p in board if p.state == "Out"),
        "not_seen": sum(1 for p in board if p.state is None),
        "failed": headline["failed"],
        "unfinished": headline["unfinished"],
    }

    return _page(
        request,
        "today.html",
        "today",
        "Today",
        window=window,
        board=board,
        counts=counts,
        refresh=REFRESH_SECONDS,
    )


def _dates(from_: str | None, to: str | None) -> tuple[date, date]:
    """Default to today, and tolerate the two being the wrong way round."""
    try:
        first = date.fromisoformat(from_) if from_ else date.today()
    except ValueError:
        first = date.today()

    try:
        last = date.fromisoformat(to) if to else first
    except ValueError:
        last = first

    return (last, first) if last < first else (first, last)


def _register_view(rows) -> list[dict]:
    return [
        {
            "id": row[0],
            "when": row[1],
            "external_id": row[2],
            "name": row[3],
            "direction": AttendanceDirection(row[4]).name,
            "result": queries.OUTCOME.get(AttendanceDecision(row[5]), str(row[5])),
            "accepted": AttendanceDecision(row[5]) == AttendanceDecision.Accepted,
            "device": row[6],
            "score": row[7],
            "has_photo": bool(row[8]),
        }
        for row in rows
    ]


@router.get("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    session: SessionDep,
    tenant: TenantDep,
    settings: SettingsDep,
    _: SignedIn,
) -> Response:
    params = request.query_params
    first, last = _dates(params.get("from"), params.get("to"))
    search = params.get("q") or None
    only_failures = bool(params.get("failures"))

    window = queries.DayWindow.for_range(first, last)
    rows = await queries.register_rows(session, tenant, window, search, only_failures)

    query_string = f"from={first}&to={last}"
    if search:
        query_string += f"&q={search}"
    if only_failures:
        query_string += "&failures=1"

    return _page(
        request,
        "register.html",
        "register",
        "Register",
        window=window,
        rows=_register_view(rows),
        date_from=first.isoformat(),
        date_to=last.isoformat(),
        search=search,
        only_failures=only_failures,
        query_string=query_string,
        retention_days=settings.face.probe_image_retention_days,
    )


@router.get("/register.csv")
async def register_csv(
    request: Request,
    session: SessionDep,
    tenant: TenantDep,
    _: SignedIn,
) -> StreamingResponse:
    params = request.query_params
    first, last = _dates(params.get("from"), params.get("to"))
    window = queries.DayWindow.for_range(first, last)

    rows = await queries.register_rows(
        session,
        tenant,
        window,
        params.get("q") or None,
        bool(params.get("failures")),
        limit=100_000,
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["When", "Employee ID", "Name", "In/Out", "Result", "Device", "Score"])

    for row in _register_view(rows):
        writer.writerow(
            [
                row["when"].astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                row["external_id"] or "",
                row["name"] or "",
                row["direction"],
                row["result"],
                row["device"] or "",
                f"{row['score']:.4f}" if row["score"] is not None else "",
            ]
        )

    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="attendance-{first}-to-{last}.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/people", response_class=HTMLResponse)
async def people(
    request: Request,
    session: SessionDep,
    tenant: TenantDep,
    _: SignedIn,
) -> Response:
    rows = [
        {
            "external_id": row[0],
            "name": row[1],
            "department": row[2],
            "state": queries.ENROLLMENT_LABEL.get(
                EnrollmentStatus(row[4]), str(row[4])
            ),
            "ready": EnrollmentStatus(row[4]) == EnrollmentStatus.Enrolled,
            "enrolled_at": row[5],
            "photos": row[7],
        }
        for row in await queries.people_rows(session, tenant)
    ]

    return _page(request, "people.html", "people", "People", rows=rows)


@router.get("/problems", response_class=HTMLResponse)
async def problems(
    request: Request,
    session: SessionDep,
    tenant: TenantDep,
    _: SignedIn,
) -> Response:
    params = request.query_params
    first, last = _dates(params.get("from"), params.get("to"))
    window = queries.DayWindow.for_range(first, last)

    groups = [
        {
            "result": queries.OUTCOME.get(AttendanceDecision(row[0]), str(row[0])),
            "cause": queries.LIKELY_CAUSE.get(AttendanceDecision(row[0]), ""),
            "device": row[1],
            "total": row[2],
            "first_at": row[3],
            "last_at": row[4],
        }
        for row in await queries.problem_summary(session, tenant, window)
    ]

    raw_hours = await queries.busiest_hours(session, tenant, window)
    hours = [{"hour": h, "accepted": ok, "failed": bad} for h, ok, bad in raw_hours]
    peak = max((h["accepted"] + h["failed"] for h in hours), default=1) or 1

    return _page(
        request,
        "problems.html",
        "problems",
        "Problems",
        window=window,
        groups=groups,
        hours=hours,
        peak=peak,
        date_from=first.isoformat(),
        date_to=last.isoformat(),
    )


def _month_window(month: str | None) -> tuple[str, queries.DayWindow]:
    today_ = date.today()

    try:
        year, month_number = (int(part) for part in (month or "").split("-", 1))
        first = date(year, month_number, 1)
    except (ValueError, TypeError):
        first = today_.replace(day=1)

    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    return first.strftime("%Y-%m"), queries.DayWindow.for_range(first, last)


def _monthly_view(rows) -> list[dict]:
    return [
        {
            "external_id": row[0],
            "name": row[1],
            "department": row[2],
            "days": row[3],
            "first": row[4],
            "last": row[5],
            "hours": row[6],
            "unpaired": row[7],
        }
        for row in rows
    ]


@router.get("/monthly", response_class=HTMLResponse)
async def monthly(
    request: Request,
    session: SessionDep,
    tenant: TenantDep,
    _: SignedIn,
) -> Response:
    month, window = _month_window(request.query_params.get("month"))
    rows = _monthly_view(await queries.monthly_totals(session, tenant, window))

    return _page(
        request,
        "monthly.html",
        "monthly",
        "Monthly",
        window=window,
        rows=rows,
        month=month,
    )


@router.get("/monthly.csv")
async def monthly_csv(
    request: Request,
    session: SessionDep,
    tenant: TenantDep,
    _: SignedIn,
) -> StreamingResponse:
    month, window = _month_window(request.query_params.get("month"))
    rows = _monthly_view(await queries.monthly_totals(session, tenant, window))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["Employee ID", "Name", "Department", "Days present", "Hours", "First in", "Last out", "Unpaired punches"]
    )

    for row in rows:
        writer.writerow(
            [
                row["external_id"],
                row["name"],
                row["department"] or "",
                row["days"],
                f"{row['hours']:.1f}",
                row["first"].astimezone().strftime("%Y-%m-%d %H:%M"),
                row["last"].astimezone().strftime("%Y-%m-%d %H:%M"),
                row["unpaired"],
            ]
        )

    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="attendance-{month}.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/photo/{event_id}", response_class=HTMLResponse)
async def photo_page(
    request: Request,
    event_id: int,
    session: SessionDep,
    tenant: TenantDep,
    settings: SettingsDep,
    _: SignedIn,
    confirm: str | None = None,
) -> Response:
    """The face stored at one punch, with the context needed to know whose it is.

    A page rather than the bare image, because this is where somebody decides whether the
    photograph should still exist - and that decision needs to be taken looking at the face,
    not at a row in a table.
    """
    event = await queries.one_event(session, tenant, event_id)

    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no such attendance row.")

    return _page(
        request,
        "photo.html",
        "register",
        "Photo",
        event=event,
        confirming=confirm == "1",
        retention_days=settings.face.probe_image_retention_days,
    )


@router.get("/photo/{event_id}/image")
async def photo_image(
    event_id: int,
    session: SessionDep,
    store: ImageStoreDep,
    tenant: TenantDep,
    _: SignedIn,
) -> Response:
    """The photograph itself, fetched only when a page asks for it.

    Scoped to the tenant and never cached: this is a photograph of a person's face.
    """
    row = (
        await session.execute(
            select(AttendanceEvent.probe_image_object_key).where(
                AttendanceEvent.id == event_id, AttendanceEvent.tenant_id == tenant
            )
        )
    ).first()

    if row is None or not row[0]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no photo for that row.")

    content = await store.read(row[0])

    if content is None:
        # Expected once retention has swept it: the row stays, the image does not.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That photo has been deleted by the retention rule.",
        )

    return Response(
        content=content,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@router.post("/photo/{event_id}/delete")
async def delete_photo(
    request: Request,
    event_id: int,
    session: SessionDep,
    store: ImageStoreDep,
    tenant: TenantDep,
    _: SignedIn,
) -> Response:
    """Erase one stored face photograph. The attendance record itself stays.

    Those are two different things and conflating them would be a bad trade: a photograph is
    personal data somebody may reasonably want gone, while the fact that they clocked in at
    08:58 is the record their pay is calculated from. Deleting the picture must not quietly
    delete the hours.

    File first, then the key - the same order the retention sweep uses, so an interruption
    leaves an orphaned file rather than a row pointing at a file that is no longer there.
    """
    row = (
        await session.execute(
            select(AttendanceEvent.probe_image_object_key).where(
                AttendanceEvent.id == event_id, AttendanceEvent.tenant_id == tenant
            )
        )
    ).first()

    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no such attendance row.")

    object_key = row[0]

    if object_key:
        await store.delete(object_key)

        await session.execute(
            update(AttendanceEvent)
            .where(AttendanceEvent.id == event_id, AttendanceEvent.tenant_id == tenant)
            .values(probe_image_object_key=None)
        )
        await session.commit()

        # Worth a line in the log: a deliberate erasure of personal data, by hand, is exactly
        # the kind of thing somebody asks about six months later.
        logger.warning(
            "Deleted the stored photo for attendance row %s at the request of a signed-in "
            "dashboard user from %s",
            event_id,
            request.client.host if request.client else "unknown",
        )

    return RedirectResponse(
        f"/admin/photo/{event_id}", status_code=status.HTTP_303_SEE_OTHER
    )
