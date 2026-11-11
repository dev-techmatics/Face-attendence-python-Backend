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
from app.admin.export import build_workbook
from app.api.deps import ImageStoreDep, SessionDep, SettingsDep, TenantDep
from app.core.enums import (
    AttendanceDecision,
    AttendanceDirection,
    EnrollmentStatus,
    PersonStatus,
)
from app.core.templating import TEMPLATE_DIR, register_filters
from app.domain.models import AttendanceEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["dashboard"], include_in_schema=False)

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
register_filters(templates)

#: Marks a session as signed in. The value is the moment of sign-in, so a session can expire
#: without anything server-side to keep.
SESSION_KEY = "signed_in_at"

#: Which of the two logins this session used. A session without it is from before roles
#: existed and is treated as signed out rather than quietly granted either role.
ROLE_KEY = "role"

ADMIN = "admin"
HR = "hr"

REFRESH_SECONDS = 30


# ---- access ------------------------------------------------------------------------------


def dashboard_enabled(settings) -> bool:
    return bool(settings.admin_ui_password)


def _role_of(request: Request, settings: SettingsDep) -> str:
    """The signed-in role, or a redirect to the login page.

    Redirects rather than erroring: a person who opens a bookmark should land on a sign-in
    box, not a JSON document.
    """
    if not dashboard_enabled(settings):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The dashboard is not switched on.",
        )

    signed_in_at = request.session.get(SESSION_KEY)
    role = request.session.get(ROLE_KEY)

    if not signed_in_at or role not in (ADMIN, HR):
        # Clear it before redirecting. A cookie that says signed-in but carries no usable
        # role is what every browser held when roles shipped, and leaving it in place sent
        # the login page straight back here - /admin to /admin/login to /admin, which a
        # browser reports as ERR_TOO_MANY_REDIRECTS and a person reports as "the site is
        # down".
        request.session.clear()
        raise _to_login(request)

    try:
        when = datetime.fromisoformat(signed_in_at)
    except (TypeError, ValueError):
        raise _to_login(request) from None

    if datetime.now(when.tzinfo) - when > timedelta(hours=settings.session_hours):
        request.session.clear()
        raise _to_login(request)

    return role


def require_signed_in(request: Request, settings: SettingsDep) -> str:
    """Either role will do."""
    return _role_of(request, settings)


def require_admin(request: Request, settings: SettingsDep) -> str:
    """Admin only.

    Hiding a link is not access control - HR can type the address. Every screen an admin
    alone may see is guarded here as well as hidden in the nav, and the two must stay in
    step: a page that is hidden but reachable is worse than one that is neither, because
    nobody is looking for the hole.

    A 404 rather than a 403, so the shape of what exists is not readable from the outside.
    """
    role = _role_of(request, settings)

    if role != ADMIN:
        logger.info("HR tried to open an admin-only page: %s", request.url.path)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="There is no such page."
        )

    return role


def _to_login(request: Request) -> HTTPException:
    """A 303 dressed as an exception, so a dependency can send someone to the login page."""
    return HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": "/admin/login"},
        detail="Please sign in.",
    )


SignedIn = Annotated[str, Depends(require_signed_in)]
AdminOnly = Annotated[str, Depends(require_admin)]


def _page(request: Request, name: str, page: str, title: str, **context) -> HTMLResponse:
    """Every dashboard page, with the signed-in role available to the template.

    ``is_admin`` drives what the nav shows. It is read from the session rather than passed
    in by each route, so a new page cannot forget it and quietly show HR an admin link.
    """
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "page": page,
            "title": title,
            "show_nav": True,
            "role": request.session.get(ROLE_KEY),
            "is_admin": request.session.get(ROLE_KEY) == ADMIN,
            **context,
        },
    )


# ---- login -------------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, settings: SettingsDep) -> Response:
    if not dashboard_enabled(settings):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The dashboard is not switched on.")

    # Both halves, and the same test the guard uses. Bouncing somebody to /admin on the
    # strength of a timestamp alone, when the guard there also demands a role, is how the
    # two pages sent each other in circles.
    if request.session.get(SESSION_KEY) and request.session.get(ROLE_KEY) in (ADMIN, HR):
        return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)

    # Anything less than a complete session is no session. Clearing it here means a stale
    # cookie costs one page load rather than wedging the browser.
    request.session.clear()

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"page": "login", "title": "Sign in", "show_nav": False, "error": None},
    )


def _match_login(settings, username: str, password: str) -> str | None:
    """Which role these credentials are, or None.

    Both pairs are always checked, with a fixed-time comparison on each, and the username is
    compared the same way as the password. Returning early on a username that does not exist
    would make "no such user" and "wrong password" take measurably different times, which is
    how somebody learns that ``super`` is a real login and ``admin`` is not.
    """
    given_user = username.strip().lower().encode()
    given_pass = password.encode()

    candidates = (
        (ADMIN, settings.admin_ui_username, settings.admin_ui_password),
        (HR, settings.hr_ui_username, settings.hr_ui_password),
    )

    matched: str | None = None

    for role, expected_user, expected_pass in candidates:
        if not expected_pass:
            # Not configured. Still spend the comparisons, so an unconfigured HR login is
            # not detectable from the outside either.
            hmac.compare_digest(given_user, b"")
            hmac.compare_digest(given_pass, b"")
            continue

        user_ok = hmac.compare_digest(given_user, expected_user.strip().lower().encode())
        pass_ok = hmac.compare_digest(given_pass, expected_pass.encode())

        if user_ok and pass_ok:
            matched = role

    return matched


@router.post("/login", response_class=HTMLResponse)
async def sign_in(
    request: Request,
    settings: SettingsDep,
    username: Annotated[str, Form()] = "",
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
                # Kept so a typo in the password does not cost them the username too.
                "username": username,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Checked before the credentials are even looked at, so a locked-out caller learns
    # nothing from how long the answer takes.
    waiting = lockout.seconds_remaining(client)

    if waiting:
        logger.warning("Dashboard sign-in from %s is locked out for %ds", client, waiting)
        return refuse(
            f"Too many wrong passwords. Try again in {max(1, waiting // 60)} minutes."
        )

    role = _match_login(settings, username, password)

    if role is None:
        # Never says which half was wrong: that would turn the login into a way of finding
        # out which usernames exist.
        logger.warning("Failed dashboard sign-in from %s", client)
        locked_for = lockout.record_failure(client)

        if locked_for:
            return refuse(
                f"Too many wrong passwords. Try again in {locked_for // 60} minutes."
            )

        return refuse("That username and password do not match.")

    lockout.record_success(client)
    request.session[SESSION_KEY] = datetime.now().astimezone().isoformat()
    request.session[ROLE_KEY] = role

    logger.info("Dashboard sign-in from %s as %s", client, role)

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
            # A row somebody changed by hand must say so wherever it is read, or the figure
            # it feeds cannot be explained later.
            "edited": row[9] is not None,
        }
        for row in rows
    ]


@router.get("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    session: SessionDep,
    tenant: TenantDep,
    settings: SettingsDep,
    role: SignedIn,
) -> Response:
    params = request.query_params
    first, last = _dates(params.get("from"), params.get("to"))
    search = params.get("q") or None
    only_failures = bool(params.get("failures"))

    # Two ways to read the same punches. "attempts" is the log - one row each, failures
    # included, which is what makes it evidence. "days" pairs them into one row per person
    # per day, which is what somebody asking "when did Krish come and go" actually wants.
    #
    # HR gets the paired view only, and gets it whatever the query string says: the tab is
    # hidden for them, and hiding a tab somebody can still reach by typing ?view= is not a
    # restriction, it is a decoration.
    paired = params.get("view") == "days" or role != ADMIN

    window = queries.DayWindow.for_range(first, last)

    query_string = f"from={first}&to={last}"
    if search:
        query_string += f"&q={search}"
    if only_failures:
        query_string += "&failures=1"

    if paired:
        days = await queries.worked_days(
            session, tenant, window, search, settings.face.open_shift_expiry_hours
        )

        return _page(
            request,
            "register.html",
            "register",
            "Attendance",
            window=window,
            paired=True,
            days=days,
            rows=[],
            date_from=first.isoformat(),
            date_to=last.isoformat(),
            search=search,
            only_failures=only_failures,
            query_string=query_string,
            retention_days=settings.face.probe_image_retention_days,
        )

    rows = await queries.register_rows(session, tenant, window, search, only_failures)

    return _page(
        request,
        "register.html",
        "register",
        "Attendance",
        window=window,
        paired=False,
        days=[],
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
    _: AdminOnly,
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
            "archived": PersonStatus(row[3]) != PersonStatus.Active,
            "state": queries.ENROLLMENT_LABEL.get(
                EnrollmentStatus(row[4]), str(row[4])
            ),
            "ready": EnrollmentStatus(row[4]) == EnrollmentStatus.Enrolled,
            "enrolled_at": row[5],
            "photos": row[7],
        }
        for row in await queries.people_rows(session, tenant)
    ]

    return _page(
        request,
        "people.html",
        "people",
        "People",
        rows=rows,
        active=sum(1 for r in rows if not r["archived"]),
        # Said once, after the write that caused it, and then gone.
        flash=request.session.pop("flash", None),
    )


@router.get("/people/{external_id}", response_class=HTMLResponse)
async def person_page(
    request: Request,
    external_id: str,
    session: SessionDep,
    tenant: TenantDep,
    settings: SettingsDep,
    _: SignedIn,
) -> Response:
    """One person's whole record, reached by clicking their name on People.

    The question this answers - "what has this person actually done?" - was previously only
    answerable by going to the register and filtering by their name, which meant knowing that
    the register was the place to look. It is the page somebody opens when an employee
    queries their hours, so it carries both the day totals and the attempts behind them.
    """
    history = await queries.person_history(
        session,
        tenant,
        external_id,
        expiry_hours=settings.face.open_shift_expiry_hours,
    )

    if history is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no such person.")

    return _page(
        request,
        "person.html",
        "people",
        history.person.full_name,
        history=history,
        expiry_hours=settings.face.open_shift_expiry_hours,
    )


@router.get("/problems", response_class=HTMLResponse)
async def problems(
    request: Request,
    session: SessionDep,
    tenant: TenantDep,
    _: AdminOnly,
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


def _export_range(first: str | None, last: str | None) -> tuple[date, date, queries.DayWindow]:
    """Any two dates, inclusive - not whole months.

    HR reconciles a quarter at a time, but a pay period rarely lines up with a calendar
    month, so the picker takes dates. A month value (``2026-11``) is still accepted and
    means that whole month, so links and bookmarks from before this took dates keep working
    rather than silently falling back to today.
    """

    def parse(value: str | None, month_end: bool) -> date | None:
        text = (value or "").strip()

        try:
            return date.fromisoformat(text)
        except ValueError:
            pass

        try:
            year, month = (int(part) for part in text.split("-", 1))
            first_of = date(year, month, 1)
        except (ValueError, TypeError):
            return None

        if not month_end:
            return first_of

        return (first_of.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    today = date.today()
    start = parse(first, month_end=False) or today.replace(day=1)
    end = parse(last, month_end=True)

    if end is None:
        # Only a start given: show the rest of that month, which is what the picker used to
        # do when it took months.
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    if end < start:
        start, end = end, start

    return start, end, queries.DayWindow.for_range(start, end)


def _csv_stamp(when) -> str:
    """A full local date and time for a spreadsheet, or empty when there is none."""
    return "" if when is None else when.astimezone().strftime("%Y-%m-%d %H:%M")


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
    settings: SettingsDep,
    _: SignedIn,
) -> Response:
    params = request.query_params
    first_day, last_day, window = _export_range(
        params.get("from") or params.get("month"), params.get("to")
    )
    rows = _monthly_view(
        await queries.monthly_totals(
            session, tenant, window, settings.face.open_shift_expiry_hours
        )
    )

    return _page(
        request,
        "monthly.html",
        "monthly",
        "Monthly",
        window=window,
        rows=rows,
        first_day=first_day.isoformat(),
        last_day=last_day.isoformat(),
    )


@router.get("/monthly.xlsx")
async def monthly_export(
    request: Request,
    session: SessionDep,
    tenant: TenantDep,
    settings: SettingsDep,
    _: SignedIn,
) -> StreamingResponse:
    """The payroll workbook: one row per employee per date, a sheet per month.

    Not a copy of what is on screen. The screen answers "how many days was everybody here";
    this answers "what happened on the 14th", which is the question somebody reconciling a
    quarter's pay is actually asking. See app/admin/export.py for why each column is there -
    in particular why Reason is left empty and why there is no status.
    """
    params = request.query_params
    first_day, last_day, window = _export_range(
        params.get("from") or params.get("month"), params.get("to")
    )

    rows = await queries.daily_breakdown(
        session, tenant, window, settings.face.open_shift_expiry_hours
    )

    book = build_workbook(rows, first_day, last_day)
    span = (
        first_day.isoformat()
        if first_day == last_day
        else f"{first_day.isoformat()}_to_{last_day.isoformat()}"
    )

    return StreamingResponse(
        iter([book]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="attendance-{span}.xlsx"',
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
    _: AdminOnly,
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
    _: AdminOnly,
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
    _: AdminOnly,
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
