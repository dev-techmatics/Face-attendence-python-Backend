"""The pages people look at.

Two audiences with different rules, and the tests that matter most are the ones proving they
stay apart: a staff member must see their own record and nobody else's, and neither view may
appear at all when no password is configured.
"""

from __future__ import annotations

import io
import zlib
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select, update

from openpyxl import load_workbook

from app.admin import queries
from app.api.rate_limit import reset_rate_limits
from app.core.config import Settings, reset_settings_cache
from app.core.enums import AttendanceDecision, AttendanceDirection
from app.db.session import get_session_factory
from app.domain.models import AttendanceEdit, AttendanceEvent, Person
from app.main import create_app

PASSWORD = "office-password-for-tests"
HR_PASSWORD = "hr-password-for-tests"
SECRET = "x" * 48
KIOSK = {"Authorization": "Bearer test-device-key"}

pytestmark = pytest.mark.asyncio


def photo(name: str) -> bytes:
    shade = 32 + (zlib.crc32(name.encode()) % 192)
    buffer = io.BytesIO()
    Image.new("RGB", (640, 640), (shade, shade, shade)).save(buffer, format="JPEG")
    return buffer.getvalue()


def upload(name: str) -> tuple[str, bytes, str]:
    return (name, photo(name), "image/jpeg")


def build(tmp_path, **overrides) -> Settings:
    values = dict(
        environment="Development",
        database={
            "url": f"sqlite+aiosqlite:///{tmp_path / 'dash.db'}",
            "migrate_on_startup": True,
        },
        api_keys={"admin": ["test-admin-key"], "device": ["test-device-key"]},
        storage={"provider": "local", "local_root_path": str(tmp_path / "faces")},
        face={
            "provider": "development",
            "required_poses": ["Front"],
            "infer_direction": False,
            "store_probe_images": True,
            # Off, so a test can record a full day of punches without them collapsing.
            "duplicate_punch_window_minutes": 0,
        },
        admin_ui_password=PASSWORD,
        hr_ui_password=HR_PASSWORD,
        session_secret=SECRET,
    )
    values.update(overrides)
    return Settings(**values)


@pytest_asyncio.fixture
async def client(tmp_path):
    reset_settings_cache()
    reset_rate_limits()

    app = create_app(build(tmp_path))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as http:
        async with app.router.lifespan_context(app):
            yield http


async def enrol(client, employee_id: str, name: str, identity: str) -> None:
    response = await client.post(
        "/v1/faces/enroll",
        headers=KIOSK,
        data={"employee_id": employee_id, "full_name": name},
        files={"face_front": upload(f"{identity}-front.jpg")},
    )
    assert response.json()["success"] is True, response.text


async def punch(client, employee_id: str, identity: str, kind: str, when: str) -> None:
    response = await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={"employee_id": employee_id, "type": kind, "captured_at": when},
        files={"face": upload(f"{identity}-front.jpg")},
    )
    assert response.json()["success"] is True, response.text


async def backdate(person_external_id: str, pairs: list[tuple[datetime, datetime]]) -> None:
    """Write accepted punches directly, for days the test needs in the past.

    The API always stamps 'now', and a week of history cannot be built by calling it.
    """
    factory = get_session_factory()

    async with factory() as session:
        person = (
            await session.execute(
                select(Person).where(Person.external_id == person_external_id)
            )
        ).scalars().one()

        for came, left in pairs:
            for when, direction in (
                (came, AttendanceDirection.In),
                (left, AttendanceDirection.Out),
            ):
                session.add(
                    AttendanceEvent(
                        tenant_id=person.tenant_id,
                        person_id=person.id,
                        direction=direction,
                        decision=AttendanceDecision.Accepted,
                        match_score=0.9,
                        threshold=0.42,
                        provider_id="test",
                        created_at_utc=when,
                        processing_ms=10,
                    )
                )

        await session.commit()


async def export_rows(client, query: str = "") -> list[dict]:
    """The workbook's rows as dicts, across every month sheet."""
    export = await client.get(f"/admin/monthly.xlsx{query}")
    book = load_workbook(io.BytesIO(export.content))

    rows = []

    for name in book.sheetnames:
        sheet = book[name]
        headers = [c.value for c in sheet[1]]
        for values in sheet.iter_rows(min_row=2, values_only=True):
            rows.append(dict(zip(headers, values)))

    return rows


async def backdate_in_only(person_external_id: str, times: list[datetime]) -> None:
    """Write check-ins with no matching check-out - somebody who forgot to punch out.

    Separate from :func:`backdate` because the interesting cases here are the unpaired ones,
    and a helper that always writes pairs cannot produce them.
    """
    factory = get_session_factory()

    async with factory() as session:
        person = (
            await session.execute(
                select(Person).where(Person.external_id == person_external_id)
            )
        ).scalars().one()

        for when in times:
            session.add(
                AttendanceEvent(
                    tenant_id=person.tenant_id,
                    person_id=person.id,
                    direction=AttendanceDirection.In,
                    decision=AttendanceDecision.Accepted,
                    match_score=0.9,
                    threshold=0.42,
                    provider_id="test",
                    created_at_utc=when,
                    processing_ms=10,
                )
            )

        await session.commit()


# ---- the dashboard is off unless configured ----------------------------------------------


async def test_no_password_means_no_dashboard_at_all(tmp_path):
    reset_settings_cache()
    app = create_app(build(tmp_path, admin_ui_password="", session_secret=""))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        async with app.router.lifespan_context(app):
            # Not 401 or a login page: the routes are not mounted, so there is nothing to find.
            assert (await http.get("/admin")).status_code == 404
            assert (await http.get("/me")).status_code == 404
            # The kiosk API is untouched by any of this.
            assert (await http.get("/v1/health")).status_code == 200


async def test_a_weak_cookie_secret_stops_the_service_starting(tmp_path):
    # A guessable signing key would let anybody mint a session cookie, making the password
    # pointless. Refusing to start is better than serving that.
    with pytest.raises(Exception, match="session_secret"):
        build(tmp_path, session_secret="short")


# ---- HR view -----------------------------------------------------------------------------


async def test_hr_pages_need_a_sign_in(client):
    for path in ("/admin", "/admin/register", "/admin/people", "/admin/problems", "/admin/monthly"):
        response = await client.get(path)

        assert response.status_code == 303, path
        assert response.headers["location"] == "/admin/login"


async def test_a_wrong_password_is_refused(client):
    response = await client.post("/admin/login", data={"username": "super", "password": "guess"})

    assert response.status_code == 401
    assert "do not match" in response.text


async def test_hr_can_sign_in_and_see_everyone(client):
    await enrol(client, "TS4012", "Alice Example", "alice")
    await enrol(client, "TS4013", "Bob Example", "bob")

    signed_in = await client.post("/admin/login", data={"username": "super", "password": PASSWORD})
    assert signed_in.status_code == 303

    today = await client.get("/admin")
    assert today.status_code == 200
    assert "Alice Example" in today.text
    assert "Bob Example" in today.text


async def test_signing_out_of_hr_ends_the_session(client):
    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})
    assert (await client.get("/admin")).status_code == 200

    await client.get("/admin/logout")

    assert (await client.get("/admin")).status_code == 303


async def test_the_register_shows_failures_and_exports_csv(client):
    await enrol(client, "TS4012", "Alice Example", "alice")
    await punch(client, "TS4012", "alice", "check_in", "2026-10-30T09:15:00+05:30")

    # A stranger's attempt, which must appear in the register as a failure.
    await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={
            "employee_id": "TS4012",
            "type": "check_in",
            "captured_at": "2026-10-30T09:20:00+05:30",
        },
        files={"face": upload("stranger-front.jpg")},
    )

    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})

    page = await client.get("/admin/register")
    assert page.status_code == 200
    assert "Recorded" in page.text
    assert "Face did not match" in page.text

    export = await client.get("/admin/register.csv")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert "attachment" in export.headers["content-disposition"]
    assert "Alice Example" in export.text


async def test_people_lists_unfinished_registrations_first(client):
    await enrol(client, "TS4012", "Alice Example", "alice")

    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})
    page = await client.get("/admin/people")

    assert page.status_code == 200
    assert "Alice Example" in page.text
    assert "Registered" in page.text


async def test_problems_groups_failures_with_a_likely_cause(client):
    await enrol(client, "TS4012", "Alice Example", "alice")

    for _ in range(3):
        await client.post(
            "/v1/attendance/punch",
            headers=KIOSK,
            data={
                "employee_id": "TS4012",
                "type": "check_in",
                "captured_at": "2026-10-30T09:20:00+05:30",
            },
            files={"face": upload("stranger-front.jpg")},
        )

    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})
    page = await client.get("/admin/problems")

    assert page.status_code == 200
    assert "Face did not match" in page.text
    # The point of the screen: what to do about it, not just a count.
    assert "registration photos no longer look like them" in page.text


async def test_monthly_totals_and_export(client):
    await enrol(client, "TS4012", "Alice Example", "alice")

    now = datetime.now(timezone.utc)
    await backdate(
        "TS4012",
        [
            (now - timedelta(days=2, hours=9), now - timedelta(days=2, hours=1)),
            (now - timedelta(days=1, hours=9), now - timedelta(days=1, hours=1)),
        ],
    )

    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})

    page = await client.get("/admin/monthly")
    assert page.status_code == 200
    assert "Alice Example" in page.text

    export = await client.get("/admin/monthly.xlsx")
    assert export.status_code == 200
    assert export.headers["content-type"].endswith("spreadsheetml.sheet")

    book = load_workbook(io.BytesIO(export.content))
    sheet = book[book.sheetnames[0]]
    headers = [c.value for c in sheet[1]]

    assert headers == ["Employee ID", "Name", "Department", "Date", "Day", "Check in",
                       "Check out", "Hours", "Missed punch", "Reason"]


# ---- staff view --------------------------------------------------------------------------


async def test_the_staff_page_asks_for_a_photo_first(client):
    response = await client.get("/me")

    assert response.status_code == 200
    assert "Tap to take your photo" in response.text
    # No name of anybody is shown before signing in.
    assert "My attendance" in response.text


async def test_a_staff_member_signs_in_with_their_face(client):
    await enrol(client, "TS4012", "Alice Example", "alice")

    signed_in = await client.post(
        "/me/signin", files={"photo": upload("alice-front.jpg")}
    )

    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == "/me"

    page = await client.get("/me")
    assert page.status_code == 200
    assert "Alice Example" in page.text
    assert "This week" in page.text


async def test_an_unrecognised_face_cannot_sign_in(client):
    await enrol(client, "TS4012", "Alice Example", "alice")

    response = await client.post("/me/signin", files={"photo": upload("stranger.jpg")})

    assert response.status_code == 401
    assert "could not recognise you" in response.text
    assert "Alice Example" not in response.text


async def test_a_photo_with_no_face_is_refused(client):
    response = await client.post(
        "/me/signin", files={"photo": ("x.jpg", b"", "image/jpeg")}
    )

    assert response.status_code == 401


async def test_one_staff_member_cannot_see_another(client):
    """The whole point of a per-person login."""
    await enrol(client, "TS4012", "Alice Example", "alice")
    await enrol(client, "TS4013", "Bob Example", "bob")

    await client.post("/me/signin", files={"photo": upload("alice-front.jpg")})

    page = await client.get("/me")

    assert "Alice Example" in page.text
    assert "Bob Example" not in page.text
    assert "TS4013" not in page.text


async def test_a_staff_sign_in_does_not_open_the_hr_pages(client):
    await enrol(client, "TS4012", "Alice Example", "alice")
    await client.post("/me/signin", files={"photo": upload("alice-front.jpg")})

    # Signing in as a person must not grant the view of everybody.
    for path in ("/admin", "/admin/register", "/admin/people", "/admin/monthly"):
        response = await client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/admin/login"


async def test_an_hr_sign_in_does_not_become_a_staff_member(client):
    await enrol(client, "TS4012", "Alice Example", "alice")
    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})

    page = await client.get("/me")

    # No person is attached to an HR session, so /me still asks for a photo.
    assert page.status_code == 200
    assert "Tap to take your photo" in page.text


async def test_signing_out_ends_the_staff_session(client):
    await enrol(client, "TS4012", "Alice Example", "alice")
    await client.post("/me/signin", files={"photo": upload("alice-front.jpg")})

    await client.get("/me/signout")

    page = await client.get("/me")
    assert "Alice Example" not in page.text
    assert "Tap to take your photo" in page.text


async def test_looking_at_your_hours_does_not_record_a_punch(client):
    await enrol(client, "TS4012", "Alice Example", "alice")

    for _ in range(3):
        await client.post("/me/signin", files={"photo": upload("alice-front.jpg")})
        await client.get("/me")

    register = await client.get(
        "/api/v1/attendance", headers={"X-Api-Key": "test-admin-key"}
    )

    # Checking a total must not add to it.
    assert register.json()["total"] == 0


async def test_hours_are_paired_not_measured_end_to_end(client):
    """A break punched out for must not be counted as time at work."""
    await enrol(client, "TS4012", "Alice Example", "alice")

    now = datetime.now(timezone.utc)
    await backdate(
        "TS4012",
        [
            # 09:00-13:00 and 14:00-18:00 yesterday: eight hours worked across nine elapsed.
            (now - timedelta(days=1, hours=9), now - timedelta(days=1, hours=5)),
            (now - timedelta(days=1, hours=4), now - timedelta(days=1)),
        ],
    )

    await client.post("/me/signin", files={"photo": upload("alice-front.jpg")})
    page = await client.get("/me")

    assert page.status_code == 200
    # Eight hours, not nine.
    assert "8h" in page.text


async def test_a_forgotten_punch_out_is_flagged_not_guessed(client):
    await enrol(client, "TS4012", "Alice Example", "alice")

    factory = get_session_factory()

    async with factory() as session:
        person = (
            await session.execute(select(Person).where(Person.external_id == "TS4012"))
        ).scalars().one()

        # Two Ins in a row, two days ago: somebody forgot to punch out.
        for offset in (30, 29):
            session.add(
                AttendanceEvent(
                    tenant_id=person.tenant_id,
                    person_id=person.id,
                    direction=AttendanceDirection.In,
                    decision=AttendanceDecision.Accepted,
                    match_score=0.9,
                    threshold=0.42,
                    provider_id="test",
                    created_at_utc=datetime.now(timezone.utc) - timedelta(hours=offset),
                    processing_ms=10,
                )
            )

        await session.commit()

    await client.post("/me/signin", files={"photo": upload("alice-front.jpg")})
    page = await client.get("/me")

    # Flagged for a human rather than silently given an end time, which would inflate pay.
    assert "check" in page.text


# ---- guessing the password ---------------------------------------------------------------
# The password is typed daily, so it is short enough to be typeable - which means it is also
# short enough to guess, given unlimited tries. Everybody in the office can reach this page.


async def test_repeated_wrong_passwords_lock_the_caller_out(client):
    from app.admin import lockout

    lockout.reset()

    for attempt in range(4):
        response = await client.post("/admin/login", data={"username": "super", "password": f"guess{attempt}"})
        assert response.status_code == 401
        assert "do not match" in response.text, f"attempt {attempt}"

    # The fifth starts the pause.
    fifth = await client.post("/admin/login", data={"username": "super", "password": "guess5"})
    assert fifth.status_code == 401
    assert "Too many wrong passwords" in fifth.text

    # And the right password is refused too, so the lockout cannot be stepped around by
    # guessing until you happen to be right.
    correct = await client.post("/admin/login", data={"username": "super", "password": PASSWORD})
    assert correct.status_code == 401
    assert "Too many wrong passwords" in correct.text

    lockout.reset()


async def test_the_right_password_clears_the_count(client):
    from app.admin import lockout

    lockout.reset()

    for attempt in range(3):
        await client.post("/admin/login", data={"username": "super", "password": f"guess{attempt}"})

    signed_in = await client.post("/admin/login", data={"username": "super", "password": PASSWORD})
    assert signed_in.status_code == 303

    # Somebody who fumbled their password twice and then got it right starts clean, rather
    # than being two tries from a lockout for the next quarter of an hour.
    for attempt in range(4):
        response = await client.post("/admin/login", data={"username": "super", "password": f"again{attempt}"})
        assert "do not match" in response.text, f"attempt {attempt}"

    lockout.reset()


async def test_a_lockout_is_per_address_not_global(client):
    """One person fumbling must not lock out the rest of the building."""
    from app.admin import lockout

    lockout.reset()

    for attempt in range(6):
        lockout.record_failure("172.24.61.50")

    assert lockout.seconds_remaining("172.24.61.50") > 0
    assert lockout.seconds_remaining("172.24.61.51") == 0

    lockout.reset()


async def test_staff_sign_in_is_not_affected_by_the_hr_lockout(client):
    # Staff do not use a password at all, so a locked-out HR login must not stop somebody
    # checking their own hours.
    from app.admin import lockout

    await enrol(client, "TS4012", "Alice Example", "alice")
    lockout.reset()

    for attempt in range(6):
        await client.post("/admin/login", data={"username": "super", "password": f"guess{attempt}"})

    signed_in = await client.post("/me/signin", files={"photo": upload("alice-front.jpg")})
    assert signed_in.status_code == 303

    lockout.reset()


# ---- the pages are a live view, never a cached one ----------------------------------------


async def test_hr_pages_are_never_cached(client):
    """Refreshing has to ask the server, or the board shows a moment that has passed."""
    await enrol(client, "TS4012", "Alice Example", "alice")
    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})

    for path in ("/admin", "/admin/register", "/admin/people", "/admin/problems", "/admin/monthly"):
        response = await client.get(path)

        assert response.status_code == 200, path
        assert "no-store" in response.headers["cache-control"], path


async def test_the_staff_page_and_both_login_pages_are_never_cached(client):
    # The login pages too: a cached one would be served to the next person at the same
    # browser, already carrying the previous person's state.
    for path in ("/me", "/admin/login"):
        response = await client.get(path)

        assert "no-store" in response.headers["cache-control"], path


async def test_a_new_registration_shows_on_the_board_without_a_punch(client):
    """Somebody who has just registered appears straight away, marked as not arrived.

    This is the question HR asks a minute after a kiosk enrolment: did it take?
    """
    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})

    before = await client.get("/admin")
    assert "Carol Example" not in before.text

    await enrol(client, "TS4099", "Carol Example", "carol")

    after = await client.get("/admin")
    assert "Carol Example" in after.text
    assert "TS4099" in after.text


# ---- deleting a stored face photograph ----------------------------------------------------


async def photographed_punch(client) -> int:
    """Register somebody, punch once, and return the attendance row id holding the photo."""
    await enrol(client, "TS4012", "Alice Example", "alice")
    await punch(client, "TS4012", "alice", "check_in", "2026-10-30T08:58:00Z")

    factory = get_session_factory()

    async with factory() as session:
        row = (
            await session.execute(
                select(AttendanceEvent.id, AttendanceEvent.probe_image_object_key)
                .order_by(AttendanceEvent.id.desc())
                .limit(1)
            )
        ).one()

    event_id, object_key = row
    assert object_key, "the fixture needs a punch that actually stored a photo"
    return event_id


async def test_the_photo_page_shows_the_face_and_who_it_belongs_to(client):
    event_id = await photographed_punch(client)
    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})

    page = await client.get(f"/admin/photo/{event_id}")

    assert page.status_code == 200
    assert "Alice Example" in page.text
    assert "TS4012" in page.text
    assert f"/admin/photo/{event_id}/image" in page.text
    # The image itself is a separate request, so opening the page does not fetch the face
    # until the browser asks for it.
    assert (await client.get(f"/admin/photo/{event_id}/image")).status_code == 200


async def test_deleting_a_photo_needs_confirming_first(client):
    event_id = await photographed_punch(client)
    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})

    plain = await client.get(f"/admin/photo/{event_id}")
    assert "Delete this photo" in plain.text
    assert "Yes, delete the photo" not in plain.text

    confirming = await client.get(f"/admin/photo/{event_id}?confirm=1")
    assert "Yes, delete the photo" in confirming.text
    assert "cannot be recovered" in confirming.text

    # Still there: asking the question must not answer it.
    assert (await client.get(f"/admin/photo/{event_id}/image")).status_code == 200


async def test_deleting_a_photo_removes_the_image_but_keeps_the_record(client):
    event_id = await photographed_punch(client)
    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})

    factory = get_session_factory()

    async with factory() as session:
        before = (
            await session.execute(
                select(AttendanceEvent.probe_image_object_key, AttendanceEvent.direction)
                .where(AttendanceEvent.id == event_id)
            )
        ).one()

    deleted = await client.post(f"/admin/photo/{event_id}/delete")
    assert deleted.status_code == 303
    assert deleted.headers["location"] == f"/admin/photo/{event_id}"

    # The picture is gone.
    assert (await client.get(f"/admin/photo/{event_id}/image")).status_code == 404

    async with factory() as session:
        after = (
            await session.execute(
                select(
                    AttendanceEvent.probe_image_object_key,
                    AttendanceEvent.direction,
                    AttendanceEvent.decision,
                ).where(AttendanceEvent.id == event_id)
            )
        ).one()

    assert after[0] is None
    # The record is not: this is the whole point. The hours must not move because a
    # photograph was erased.
    assert after[1] == before[1]
    assert after[2] == AttendanceDecision.Accepted


async def test_the_register_still_lists_a_punch_whose_photo_was_deleted(client):
    event_id = await photographed_punch(client)
    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})
    await client.post(f"/admin/photo/{event_id}/delete")

    register = await client.get("/admin/register")

    assert register.status_code == 200
    assert "Alice Example" in register.text
    # The row survives; only its "show" link goes.
    assert f'href="/admin/photo/{event_id}"' not in register.text


async def test_the_page_says_so_when_the_photo_is_already_gone(client):
    event_id = await photographed_punch(client)
    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})
    await client.post(f"/admin/photo/{event_id}/delete")

    page = await client.get(f"/admin/photo/{event_id}")

    assert page.status_code == 200
    assert "no photo for this attempt any more" in page.text
    assert "Delete this photo" not in page.text


async def test_deleting_twice_is_harmless(client):
    event_id = await photographed_punch(client)
    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})

    assert (await client.post(f"/admin/photo/{event_id}/delete")).status_code == 303
    # Somebody refreshing, or two people on the same row. Neither should produce an error.
    assert (await client.post(f"/admin/photo/{event_id}/delete")).status_code == 303


async def test_a_stranger_cannot_see_or_delete_a_photo(client):
    event_id = await photographed_punch(client)

    # No sign-in at all.
    for path in (f"/admin/photo/{event_id}", f"/admin/photo/{event_id}/image"):
        response = await client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/admin/login"

    refused = await client.post(f"/admin/photo/{event_id}/delete")
    assert refused.status_code == 303
    assert refused.headers["location"] == "/admin/login"

    # And it is still there afterwards.
    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})
    assert (await client.get(f"/admin/photo/{event_id}/image")).status_code == 200


async def test_a_photo_from_another_tenant_is_not_reachable(client):
    event_id = await photographed_punch(client)
    await client.post("/admin/login", data={"username": "super", "password": PASSWORD})

    factory = get_session_factory()

    async with factory() as session:
        await session.execute(
            update(AttendanceEvent)
            .where(AttendanceEvent.id == event_id)
            .values(tenant_id="somebody-else")
        )
        await session.commit()

    assert (await client.get(f"/admin/photo/{event_id}")).status_code == 404
    assert (await client.get(f"/admin/photo/{event_id}/image")).status_code == 404
    assert (await client.post(f"/admin/photo/{event_id}/delete")).status_code == 404


# ---- adding and removing people -----------------------------------------------------------


async def signed_in_hr(client):
    assert (await client.post("/admin/login", data={"username": "super", "password": PASSWORD})).status_code == 303


async def test_hr_can_add_somebody_who_can_then_clock_in(client):
    await signed_in_hr(client)

    added = await client.post(
        "/admin/people/add",
        data={"employee_id": "ts4014", "full_name": "  Priya   Sharma ", "department": "Ops"},
        files={"photo": upload("priya-front.jpg")},
    )

    assert added.status_code == 303

    page = await client.get("/admin/people")
    assert "Priya Sharma" in page.text          # whitespace tidied
    assert "TS4014" in page.text                # id upper-cased
    assert "Ops" in page.text
    assert "can clock in now" in page.text

    # The real proof: the kiosk recognises them.
    await punch(client, "TS4014", "priya", "check_in", "2026-10-30T09:00:00Z")


async def test_adding_rejects_a_duplicate_id_without_touching_the_original(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    clash = await client.post(
        "/admin/people/add",
        data={"employee_id": "TS4012", "full_name": "Someone Else"},
        files={"photo": upload("bob-front.jpg")},
    )

    assert clash.status_code == 303
    page = await client.get("/admin/people")
    assert "already on the list" in page.text
    assert "Someone Else" not in page.text
    assert "Alice Example" in page.text


async def test_adding_without_a_photo_says_so(client):
    await signed_in_hr(client)

    response = await client.post(
        "/admin/people/add", data={"employee_id": "TS4014", "full_name": "Priya Sharma"}
    )

    assert response.status_code == 303
    assert "photo of their face is needed" in (await client.get("/admin/people")).text


async def test_a_bad_employee_id_is_refused(client):
    await signed_in_hr(client)

    response = await client.post(
        "/admin/people/add",
        data={"employee_id": "no spaces allowed", "full_name": "Priya Sharma"},
        files={"photo": upload("priya-front.jpg")},
    )

    assert response.status_code == 303
    assert "letters, numbers, dot, dash or underscore" in (await client.get("/admin/people")).text


async def test_an_unusable_photo_leaves_no_half_made_record(client):
    await signed_in_hr(client)

    response = await client.post(
        "/admin/people/add",
        data={"employee_id": "TS4014", "full_name": "Priya Sharma"},
        files={"photo": ("empty.jpg", b"", "image/jpeg")},
    )

    assert response.status_code == 303

    # The id must not be left occupied by a record holding no usable face. Checked against
    # the table row, because the add form carries TS4014 as its placeholder.
    assert "/admin/people/TS4014/delete" not in (await client.get("/admin/people")).text

    factory = get_session_factory()

    async with factory() as session:
        left_behind = (
            await session.execute(select(Person).where(Person.external_id == "TS4014"))
        ).scalars().all()

    assert left_behind == []


async def test_removing_somebody_stops_attendance_but_keeps_their_hours(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await punch(client, "TS4012", "alice", "check_in", "2026-10-30T08:58:00Z")

    removed = await client.post("/admin/people/TS4012/archive")
    assert removed.status_code == 303

    people = await client.get("/admin/people")
    assert "Removed" in people.text
    assert "hours are still on record" in people.text

    # Off the Today board.
    assert "Alice Example" not in (await client.get("/admin")).text

    # The history is untouched.
    assert "Alice Example" in (await client.get("/admin/register")).text

    # And the kiosk will not accept them any more.
    refused = await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={
            "employee_id": "TS4012",
            "type": "check_out",
            "captured_at": "2026-10-30T17:30:00Z",
        },
        files={"face": upload("alice-front.jpg")},
    )
    assert refused.json()["success"] is False


async def test_removing_is_reversible(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    await client.post("/admin/people/TS4012/archive")
    restored = await client.post("/admin/people/TS4012/restore")

    assert restored.status_code == 303
    assert "back on the list" in (await client.get("/admin/people")).text

    # Back on the board and clocking in again.
    assert "Alice Example" in (await client.get("/admin")).text
    await punch(client, "TS4012", "alice", "check_in", "2026-10-30T09:05:00Z")


async def test_the_delete_screen_counts_what_would_be_destroyed(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await punch(client, "TS4012", "alice", "check_in", "2026-10-30T08:58:00Z")

    page = await client.get("/admin/people/TS4012/delete")

    assert page.status_code == 200
    assert "cannot be undone" in page.text
    assert "Type <strong>delete</strong> to confirm" in page.text
    # It points at the reversible option rather than only warning.
    assert "Remove</em> instead" in page.text


async def test_deleting_does_nothing_until_the_id_is_typed_correctly(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    for wrong in ("", "TS4012", "del", "delete please"):
        response = await client.post("/admin/people/TS4012/delete", data={"confirm": wrong})

        assert response.status_code == 400, wrong
        assert "Nothing has been deleted" in response.text

    assert "Alice Example" in (await client.get("/admin/people")).text


async def test_deleting_erases_the_person_their_face_and_their_history(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await enrol(client, "TS4013", "Bob Example", "bob")
    await punch(client, "TS4012", "alice", "check_in", "2026-10-30T08:58:00Z")

    factory = get_session_factory()

    async with factory() as session:
        alice_id = (
            await session.execute(select(Person.id).where(Person.external_id == "TS4012"))
        ).scalar_one()

    # Mixed case and stray spaces: somebody typing the word in a hurry still means it.
    gone = await client.post("/admin/people/TS4012/delete", data={"confirm": "  Delete "})
    assert gone.status_code == 303

    people = await client.get("/admin/people")
    assert "deleted for good" in people.text
    # Gone from the table. Her name is still on the page, in the sentence saying she was
    # deleted, so the row is what has to be checked.
    assert "/admin/people/TS4012/delete" not in people.text
    # The person who was not deleted is untouched.
    assert "/admin/people/TS4013/delete" in people.text
    assert "Bob Example" in people.text

    async with factory() as session:
        assert (
            await session.execute(select(Person).where(Person.external_id == "TS4012"))
        ).scalars().all() == []

        # No orphaned attendance rows pointing at a person who no longer exists.
        assert (
            await session.execute(
                select(AttendanceEvent).where(AttendanceEvent.person_id == alice_id)
            )
        ).scalars().all() == []

        assert (
            await session.execute(select(Person).where(Person.external_id == "TS4013"))
        ).scalars().first() is not None


async def test_a_deleted_id_can_be_used_again(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await client.post("/admin/people/TS4012/delete", data={"confirm": "delete"})

    await enrol(client, "TS4012", "Different Person", "bob")
    assert "Different Person" in (await client.get("/admin/people")).text


async def test_managing_people_needs_a_sign_in(client):
    await enrol(client, "TS4012", "Alice Example", "alice")

    posts = (
        ("/admin/people/add", {"employee_id": "TS9999", "full_name": "Nobody"}),
        ("/admin/people/TS4012/archive", {}),
        ("/admin/people/TS4012/restore", {}),
        ("/admin/people/TS4012/delete", {"confirm": "delete"}),
    )

    for path, data in posts:
        response = await client.post(path, data=data)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/admin/login", path

    stopped = await client.get("/admin/people/TS4012/delete")
    assert stopped.headers["location"] == "/admin/login"

    # Nothing happened to anybody.
    await signed_in_hr(client)
    page = await client.get("/admin/people")
    assert "Alice Example" in page.text
    assert "Removed" not in page.text
    assert "TS9999" not in page.text


async def test_acting_on_somebody_who_is_not_there_is_a_404(client):
    await signed_in_hr(client)

    assert (await client.post("/admin/people/NOBODY/archive")).status_code == 404
    assert (await client.post("/admin/people/NOBODY/restore")).status_code == 404
    assert (await client.get("/admin/people/NOBODY/delete")).status_code == 404
    assert (
        await client.post("/admin/people/NOBODY/delete", data={"confirm": "delete"})
    ).status_code == 404


# ---- the register, read as one line per person per day ------------------------------------


async def test_a_full_day_is_one_line_with_in_out_and_hours(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    # Written directly rather than punched through the API, for two reasons: the server
    # stamps its own clock so two calls land in the same second, and a check-out that soon
    # after a check-in is refused by the minimum-shift rule.
    came = datetime.now(timezone.utc).replace(
        hour=3, minute=30, second=0, microsecond=0
    ) - timedelta(days=1)
    await backdate("TS4012", [(came, came + timedelta(hours=8, minutes=30))])

    day = came.astimezone().date().isoformat()
    attempts = await client.get(f"/admin/register?from={day}&to={day}")
    days = await client.get(f"/admin/register?view=days&from={day}&to={day}")

    # Two rows in the log, one row in the day view - that is the whole difference.
    assert attempts.text.count("TS4012") == 2
    assert days.text.count("TS4012") == 1

    assert "One line per check-in" in days.text
    assert "Alice Example" in days.text


async def test_the_hours_skip_a_break_that_was_punched_out_and_back_in(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    # Written directly, because the server stamps the punch with its own clock - the
    # captured_at the kiosk sends does not set the time, so four calls would land in the
    # same second.
    yesterday = datetime.now(timezone.utc).replace(
        hour=3, minute=30, second=0, microsecond=0
    ) - timedelta(days=1)

    # Nine to five with an hour out at lunch: seven hours at work, not eight.
    await backdate(
        "TS4012",
        [
            (yesterday, yesterday + timedelta(hours=4)),
            (yesterday + timedelta(hours=5), yesterday + timedelta(hours=8)),
        ],
    )

    day = (yesterday.astimezone().date()).isoformat()
    page = await client.get(f"/admin/register?view=days&from={day}&to={day}")

    # Two sessions, because they checked out and back in: four hours and three, never the
    # eight that measuring first-to-last would have given.
    assert page.text.count("TS4012") == 2
    assert "4h" in page.text
    assert "3h" in page.text
    assert "8h" not in page.text


async def test_a_missing_punch_is_flagged_not_guessed(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await punch(client, "TS4012", "alice", "check_in", "2026-10-30T09:00:00Z")

    page = await client.get("/admin/register?view=days")

    # Still in the building as far as the system knows, so no invented leaving time.
    assert "Still in" in page.text


async def test_failures_are_left_out_of_the_day_view_but_stay_in_the_log(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await punch(client, "TS4012", "alice", "check_in", "2026-10-30T09:00:00Z")

    # A stranger at the kiosk: recorded as a failure, attached to nobody.
    await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={
            "employee_id": "TS4012",
            "type": "check_out",
            "captured_at": "2026-10-30T17:00:00Z",
        },
        files={"face": upload("stranger.jpg")},
    )

    assert "Face did not match" in (await client.get("/admin/register")).text
    # A failure has no other half, so counting it as a departure would be an invention.
    assert "Face did not match" not in (await client.get("/admin/register?view=days")).text


async def test_the_two_views_are_linked_both_ways(client):
    await signed_in_hr(client)

    attempts = await client.get("/admin/register")
    assert "view=days" in attempts.text

    days = await client.get("/admin/register?view=days")
    assert "Check in and check out" in days.text


async def test_the_filters_survive_switching_view(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await enrol(client, "TS4013", "Bob Example", "bob")
    await punch(client, "TS4012", "alice", "check_in", "2026-10-30T09:00:00Z")
    await punch(client, "TS4013", "bob", "check_in", "2026-10-30T09:05:00Z")

    page = await client.get("/admin/register?view=days&q=TS4013")

    assert "Bob Example" in page.text
    assert "Alice Example" not in page.text
    # The search has to be carried in the link back, or switching view loses it.
    assert "q=TS4013" in page.text


async def test_the_day_view_needs_a_sign_in(client):
    response = await client.get("/admin/register?view=days")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


# ---- one face, one identity ---------------------------------------------------------------


async def test_a_removed_person_cannot_register_again_under_a_new_id(client):
    """The bug this was written for.

    "Is this face already on file" is not "who may clock in". Asking the narrower question
    let somebody who had been removed come back as a second person, with their hours split
    across two records and nothing on screen saying so.
    """
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await client.post("/admin/people/TS4012/archive")

    again = await client.post(
        "/v1/faces/enroll",
        headers=KIOSK,
        data={"employee_id": "TS9001", "full_name": "Alice Again"},
        files={"face_front": upload("alice-front.jpg")},
    )

    body = again.json()
    assert body["success"] is False, "a removed person's face must still be recognised"
    assert "removed" in body["message"]
    assert "put that record back" in body["message"]

    # And no second record was left behind by the refusal.
    page = await client.get("/admin/people")
    assert "Alice Again" not in page.text
    assert "/admin/people/TS9001/delete" not in page.text


async def test_the_refusal_does_not_name_the_other_person(client):
    # The kiosk screen is in front of whoever is registering, so it must not tell them
    # whose face they matched.
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    clash = await client.post(
        "/v1/faces/enroll",
        headers=KIOSK,
        data={"employee_id": "TS9001", "full_name": "Somebody Else"},
        files={"face_front": upload("alice-front.jpg")},
    )

    message = clash.json()["message"]
    assert clash.json()["success"] is False
    assert "Alice" not in message
    assert "TS4012" not in message


async def test_a_live_duplicate_is_still_refused_with_its_own_wording(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    clash = await client.post(
        "/v1/faces/enroll",
        headers=KIOSK,
        data={"employee_id": "TS9001", "full_name": "Somebody Else"},
        files={"face_front": upload("alice-front.jpg")},
    )

    body = clash.json()
    assert body["success"] is False
    assert "already registered under another ID" in body["message"]
    # The live case must not send an operator looking for a removed record.
    assert "removed" not in body["message"]


async def test_putting_the_record_back_is_what_actually_fixes_it(client):
    """The instruction the refusal gives has to work."""
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await client.post("/admin/people/TS4012/archive")

    restored = await client.post("/admin/people/TS4012/restore")
    assert restored.status_code == 303

    # Back to clocking in as themselves, no second identity needed.
    await punch(client, "TS4012", "alice", "check_in", "2026-10-30T09:00:00Z")


async def test_a_removed_person_still_cannot_clock_in(client):
    # The fix widens the duplicate check only. Who may mark attendance is a separate
    # question and its answer must not change.
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await client.post("/admin/people/TS4012/archive")

    refused = await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={
            "employee_id": "TS4012",
            "type": "check_in",
            "captured_at": "2026-10-30T09:00:00Z",
        },
        files={"face": upload("alice-front.jpg")},
    )

    assert refused.json()["success"] is False


async def test_a_removed_person_is_not_matched_by_a_face_search(client):
    """1:N must still skip them, or a removed person clocks in without claiming an id."""
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await client.post("/admin/people/TS4012/archive")

    found = await client.post(
        "/v1/faces/identify", headers=KIOSK, files={"face": upload("alice-front.jpg")}
    )

    assert found.json()["success"] is False


async def test_a_genuinely_new_face_is_unaffected(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await client.post("/admin/people/TS4012/archive")

    # Somebody else entirely still gets in.
    await enrol(client, "TS4013", "Bob Example", "bob")
    assert "Bob Example" in (await client.get("/admin/people")).text


async def test_the_dashboard_add_form_refuses_a_removed_persons_face_too(client):
    # Same guard, or the hole simply moves to the other door.
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await client.post("/admin/people/TS4012/archive")

    added = await client.post(
        "/admin/people/add",
        data={"employee_id": "TS9001", "full_name": "Alice Again"},
        files={"photo": upload("alice-front.jpg")},
    )

    assert added.status_code == 303
    page = await client.get("/admin/people")
    assert "removed" in page.text
    assert "Alice Again" not in page.text


# ---- one person's whole record -----------------------------------------------------------
# Reached by clicking a name on People. The question "what has this person actually done?"
# previously needed somebody to know that the Attendance page was where to filter by name.


async def test_a_name_on_people_links_to_that_person(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    page = await client.get("/admin/people")

    assert '/admin/people/TS4012"' in page.text


async def test_a_person_page_shows_their_days_and_not_an_attempt_log(client):
    """Days only. The log was taken off this page deliberately.

    It answers "when did they come and go". The attempts behind those figures, failures
    included, stay on the Attendance page, which this page links to filtered by their id.
    """
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    came = datetime.now(timezone.utc).replace(
        hour=3, minute=30, second=0, microsecond=0
    ) - timedelta(days=1)
    await backdate("TS4012", [(came, came + timedelta(hours=7))])

    page = await client.get("/admin/people/TS4012")

    assert page.status_code == 200
    assert "Alice Example" in page.text
    assert "Check in and check out" in page.text
    assert "7h" in page.text

    assert "Every attempt" not in page.text
    # ...and the evidence is still reachable, filtered to this person.
    assert "/admin/register?q=TS4012" in page.text


async def test_a_person_pages_hours_cover_every_punch_on_record(client):
    """No cap on the totals.

    This is where somebody checks a figure they are being paid on, so a total that quietly
    left out the oldest punches would be worse than no total at all.
    """
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    start = datetime.now(timezone.utc).replace(
        hour=3, minute=0, second=0, microsecond=0
    ) - timedelta(days=30)
    days = [
        (start + timedelta(days=n), start + timedelta(days=n, hours=8))
        for n in range(25)
    ]
    await backdate("TS4012", days)

    factory = get_session_factory()

    async with factory() as session:
        history = await queries.person_history(session, "default", "TS4012")

    assert history.days_present == 25
    assert history.total_hours == 200.0         # 25 days x 8 hours


async def test_a_day_with_no_check_out_reads_as_missed_once_it_is_too_late(client):
    """The bug this replaced: an unclosed day from weeks ago still said "Still in".

    The cutoff is the same one the server uses to abandon a stale check-in, so a day turns
    from "still in" to "missed" at exactly the moment the next punch would start a fresh
    check-in rather than close this one.
    """
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    # Checked in two days ago and never out. Written directly: an In with no Out.
    came = datetime.now(timezone.utc) - timedelta(days=2)
    await backdate_in_only("TS4012", [came])

    page = await client.get("/admin/people/TS4012")

    # The badge in the Out column, not the "Missed check-outs" summary card above it.
    assert 't-bad">Missed<' in page.text
    assert "Still in" not in page.text

    factory = get_session_factory()

    async with factory() as session:
        history = await queries.person_history(
            session, "default", "TS4012", expiry_hours=16
        )

    assert history.missed_outs == 1
    assert history.sessions[0].missed_out is True
    assert history.sessions[0].open_now is False
    # No leaving time is invented, so the session earns nothing.
    assert history.sessions[0].hours == 0.0


async def test_a_check_in_from_an_hour_ago_still_reads_as_still_in(client):
    """The other side of the same cutoff - do not call somebody missing who is at their desk."""
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    await backdate_in_only("TS4012", [datetime.now(timezone.utc) - timedelta(hours=1)])

    page = await client.get("/admin/people/TS4012")

    assert "Still in" in page.text
    assert 't-bad">Missed<' not in page.text


async def test_a_person_who_is_not_there_is_a_404(client):
    await signed_in_hr(client)

    assert (await client.get("/admin/people/NOBODY")).status_code == 404


async def test_a_forgotten_check_in_does_not_swallow_the_hours_already_earned(client):
    """What pairing by session buys, in the case that worried me under the old model.

    Somebody completes a shift, comes back later and forgets to check out. The finished
    session keeps every minute of it; only the unfinished one is blank. Under the previous
    day-shaped model the two shared a row, so hiding the unfinished half hid the earned
    hours with it.
    """
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    # Anchored to a morning so both check-ins land on the same local date whatever time of
    # day the suite runs - otherwise "days present" is 1 or 2 depending on the clock.
    local = datetime.now().astimezone().tzinfo
    came = datetime.now(local).replace(hour=8, minute=0, second=0, microsecond=0) - timedelta(days=2)
    await backdate("TS4012", [(came, came + timedelta(hours=8))])
    await backdate_in_only("TS4012", [came + timedelta(hours=9)])

    page = await client.get("/admin/people/TS4012")

    assert 't-bad">Missed<' in page.text
    assert "8h" in page.text          # the finished shift is untouched

    factory = get_session_factory()

    async with factory() as session:
        history = await queries.person_history(
            session, "default", "TS4012", expiry_hours=16
        )

    finished, forgotten = sorted(history.sessions, key=lambda s: s.hours, reverse=True)
    assert finished.hours == 8.0 and finished.ended is not None
    assert forgotten.missed_out is True and forgotten.hours == 0.0
    assert history.total_hours == 8.0
    assert history.missed_outs == 1
    # Both started on the same date, so it is one day present, not two.
    assert history.days_present == 1


async def test_the_person_page_no_longer_totals_hours(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    came = datetime.now(timezone.utc).replace(hour=3, minute=0) - timedelta(days=1)
    await backdate("TS4012", [(came, came + timedelta(hours=8))])

    page = await client.get("/admin/people/TS4012")

    assert "Hours in total" not in page.text
    assert "Days present" in page.text
    assert "Missed check-outs" in page.text


async def test_going_out_and_coming_back_is_two_rows_not_a_contradiction(client):
    """Mighty Mike's row used to say "out 15:46" and "Still in" at the same time.

    Both were true of his day and the pair read as nonsense. As sessions there is nothing to
    reconcile: the finished one shows when he left, the open one shows no leaving time.
    """
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    base = datetime.now(timezone.utc) - timedelta(hours=3)
    await backdate("TS4012", [(base, base + timedelta(minutes=22))])
    await backdate_in_only("TS4012", [base + timedelta(minutes=81)])

    factory = get_session_factory()

    async with factory() as session:
        history = await queries.person_history(
            session, "default", "TS4012", expiry_hours=16
        )

    assert len(history.sessions) == 2
    newest, oldest = history.sessions

    assert newest.open_now is True and newest.ended is None
    assert oldest.open_now is False and oldest.hours == 0.37
    assert oldest.ended is not None
    # One date, two sessions.
    assert history.days_present == 1


async def test_a_night_shift_is_one_session_with_its_hours_intact(client):
    """The change the user asked for.

    22:00 to 06:00 is eight hours. Bucketed by calendar date first, as it used to be, the
    check-in could never find its check-out and the shift was worth nothing.
    """
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    local = datetime.now().astimezone().tzinfo
    started = datetime.now(local).replace(hour=22, minute=0, second=0, microsecond=0) - timedelta(days=2)
    await backdate("TS4012", [(started, started + timedelta(hours=8))])

    factory = get_session_factory()

    async with factory() as session:
        history = await queries.person_history(
            session, "default", "TS4012", expiry_hours=16
        )

    assert len(history.sessions) == 1
    night = history.sessions[0]

    assert night.hours == 8.0
    assert night.ends_next_day is True
    assert night.nights == 1
    # Attributed to the date they came to work, not the date they went home.
    assert night.day == started.date()
    assert history.days_present == 1
    assert history.missed_outs == 0

    page = await client.get("/admin/people/TS4012")
    assert "8h" in page.text
    assert "+1" in page.text


# ---- the payroll export ------------------------------------------------------------------
# HR downloads a quarter of this to reconcile pay, so the shape of it is the requirement:
# every employee, every date, in employee-id order, with the times and whether anything
# needs a human.


async def test_the_export_has_a_row_for_every_date_including_absences(client):
    """A day nobody turned up is the row a loss-of-pay check is hunting for."""
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    local = datetime.now().astimezone().tzinfo
    worked = datetime.now(local).replace(hour=9, minute=0, second=0, microsecond=0) - timedelta(days=3)
    await backdate("TS4012", [(worked, worked + timedelta(hours=8))])

    rows = await export_rows(client)

    mine = [r for r in rows if r["Employee ID"] == "TS4012"]
    worked_rows = [r for r in mine if r["Check in"] != "-"]
    empty_rows = [r for r in mine if r["Check in"] == "-" and r["Check out"] == "-"]

    assert len(worked_rows) == 1
    assert worked_rows[0]["Date"] == worked.date().isoformat()
    assert worked_rows[0]["Hours"] == 8.0
    # Both ends carry their date, so a night worker's two dates are readable off the row.
    assert worked_rows[0]["Check in"] == worked.strftime("%Y-%m-%d %H:%M")
    assert worked_rows[0]["Check out"] == (worked + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    # A date nobody turned up is still a row, dashed rather than blank - 96% of a sheet
    # left blank read as a failed download.
    assert empty_rows, "a date with no attendance must still appear"
    assert all(r["Missed punch"] == "-" for r in empty_rows)
    assert all(r["Reason"] is None for r in mine), "Reason is HR's column, never ours"


async def test_the_export_is_in_employee_id_order_numerically(client):
    """1267 before 12345. A plain string sort puts 12345 first, which reads as a mistake."""
    await signed_in_hr(client)
    await enrol(client, "12345", "Mike", "mike")
    await enrol(client, "1267", "Krishna", "krishna")
    await enrol(client, "TS4012", "Alice", "alice")

    rows = await export_rows(client)

    seen = []
    for r in rows:
        if r["Employee ID"] not in seen:
            seen.append(r["Employee ID"])

    assert seen == ["1267", "12345", "TS4012"]


async def test_a_missing_half_is_named_in_the_export(client):
    """"Missed punch" says which half never arrived - a fact, not a judgement."""
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    local = datetime.now().astimezone().tzinfo
    came = datetime.now(local).replace(hour=9, minute=0, second=0, microsecond=0) - timedelta(days=2)
    await backdate_in_only("TS4012", [came])

    rows = await export_rows(client)
    day = next(r for r in rows if r["Date"] == came.date().isoformat())

    assert day["Check in"] == came.strftime("%Y-%m-%d %H:%M")
    assert day["Check out"] == "-"
    assert day["Missed punch"] == "out"
    assert day["Reason"] is None


async def test_a_night_shift_in_the_export_carries_a_full_check_out_timestamp(client):
    """A bare "06:00" in a spreadsheet cannot say it was the next morning."""
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    local = datetime.now().astimezone().tzinfo
    started = datetime.now(local).replace(hour=22, minute=0, second=0, microsecond=0) - timedelta(days=2)
    await backdate("TS4012", [(started, started + timedelta(hours=8))])

    rows = await export_rows(client)

    night = next(r for r in rows if r["Date"] == started.date().isoformat())
    next_morning = started + timedelta(hours=8)

    assert night["Hours"] == 8.0
    # Counted against the date it started, and BOTH ends name their own date, so the night
    # worker's two dates can be read straight off the row.
    assert night["Check in"] == started.strftime("%Y-%m-%d %H:%M")
    assert night["Check out"] == next_morning.strftime("%Y-%m-%d %H:%M")
    assert night["Check out"].startswith((started.date() + timedelta(days=1)).isoformat())


async def test_a_month_value_still_means_that_whole_month(client):
    """Links from before the picker took dates must not silently fall back to today."""
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    export = await client.get("/admin/monthly.xlsx?from=2026-09&to=2026-11")

    assert export.status_code == 200
    # 2026-09 means the 1st, 2026-11 means the 30th - the whole span, not two single days.
    assert (
        "attendance-2026-09-01_to_2026-11-30.xlsx"
        in export.headers["content-disposition"]
    )

    book = load_workbook(io.BytesIO(export.content))

    # One sheet per month, in order, each in its own colour.
    assert book.sheetnames == ["Sep 2026", "Oct 2026", "Nov 2026"]
    colours = [book[name].sheet_properties.tabColor.rgb for name in book.sheetnames]
    assert len(set(colours)) == 3


async def test_the_export_takes_any_two_dates_not_whole_months(client):
    """A pay period rarely starts on the 1st."""
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    export = await client.get("/admin/monthly.xlsx?from=2026-10-16&to=2026-11-15")
    book = load_workbook(io.BytesIO(export.content))

    assert (
        "attendance-2026-10-16_to_2026-11-15.xlsx"
        in export.headers["content-disposition"]
    )
    # Split across month sheets, because that is how the workbook is organised.
    assert book.sheetnames == ["Oct 2026", "Nov 2026"]

    dates = []
    for name in book.sheetnames:
        sheet = book[name]
        for values in sheet.iter_rows(min_row=2, values_only=True):
            if values[3]:
                dates.append(values[3])

    # The person was enrolled today and has no attendance, so the sheet is clipped to the
    # dates they existed - that guard is correct and tested elsewhere. What this asserts is
    # the range itself: nothing may fall outside the two dates asked for.
    assert dates, "the requested months must produce rows"
    assert all("2026-10-16" <= d <= "2026-11-15" for d in dates)


async def test_a_backwards_range_is_read_the_right_way_round(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    export = await client.get("/admin/monthly.xlsx?from=2026-11-15&to=2026-10-16")

    assert (
        "attendance-2026-10-16_to_2026-11-15.xlsx"
        in export.headers["content-disposition"]
    )


async def test_a_day_with_several_sessions_is_several_rows_none_contradicting(client):
    """The row the user circled: "in 15:24, out 15:46" beside a flag saying out was missing.

    Both halves were true of Mighty Mike's 10 November - those were the first check-in and
    the last check-out of the date, while a third check-in was never closed - and the row as
    a whole was nonsense. One row per pair makes the contradiction impossible to express.
    """
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    local = datetime.now().astimezone().tzinfo
    base = datetime.now(local).replace(hour=9, minute=0, second=0, microsecond=0) - timedelta(days=2)

    await backdate("TS4012", [(base, base + timedelta(hours=4))])
    await backdate_in_only("TS4012", [base + timedelta(hours=6)])

    rows = await export_rows(client)
    day = [r for r in rows if r["Date"] == base.date().isoformat()]

    assert len(day) == 2, "a finished pair and an unfinished one are two rows"

    finished, unfinished = sorted(day, key=lambda r: r["Check in"])

    assert finished["Check out"] != "-" and finished["Hours"] == 4.0
    assert finished["Missed punch"] == "-"

    assert unfinished["Check out"] == "-"
    assert unfinished["Missed punch"] == "out"

    # No row may claim a check-out and a missing check-out at once.
    assert not any(r["Check out"] != "-" and r["Missed punch"] == "out" for r in rows)


async def test_the_export_handles_several_people_sharing_a_date(client):
    """Regression: sorting compared an aware timestamp against a naive sentinel.

    With one person in a test the rows for a date never had to be ordered against each
    other, so it passed. With six people in the real database it raised on the first row and
    the download would have failed outright.
    """
    await signed_in_hr(client)

    for employee_id, identity in (("1267", "krishna"), ("12345", "mike"), ("TS4012", "alice")):
        await enrol(client, employee_id, f"Person {employee_id}", identity)

    local = datetime.now().astimezone().tzinfo
    worked = datetime.now(local).replace(hour=9, minute=0, second=0, microsecond=0) - timedelta(days=1)

    # All three on the same date, so the rows for it have to be ordered against each other -
    # which is the comparison that used to raise. They each punched, because the enrolment
    # guard would otherwise clip people who never did.
    for employee_id in ("1267", "12345", "TS4012"):
        await backdate(employee_id, [(worked, worked + timedelta(hours=8))])

    rows = await export_rows(client)
    same_day = [r for r in rows if r["Date"] == worked.date().isoformat()]

    assert len(same_day) == 3
    assert [r["Employee ID"] for r in same_day] == ["1267", "12345", "TS4012"]


# ---- the two logins ----------------------------------------------------------------------
# Shared logins, not accounts: the dashboard can tell an admin from somebody in HR and
# cannot tell one admin from another. Enough to decide which screens to show.


async def signed_in_as_hr(client):
    assert (
        await client.post("/admin/login", data={"username": "hr", "password": HR_PASSWORD})
    ).status_code == 303


async def test_the_login_asks_for_a_username_as_well(client):
    page = await client.get("/admin/login")

    assert 'name="username"' in page.text
    assert 'name="password"' in page.text


async def test_each_login_lands_in_its_own_role(client):
    await signed_in_as_hr(client)
    assert (await client.get("/admin")).status_code == 200

    await client.get("/admin/logout")

    await signed_in_hr(client)
    assert (await client.get("/admin")).status_code == 200


async def test_the_right_password_with_the_wrong_username_is_refused(client):
    """Otherwise the username is decoration and there is only really one credential."""
    response = await client.post(
        "/admin/login", data={"username": "hr", "password": PASSWORD}
    )

    assert response.status_code == 401
    assert (await client.get("/admin")).status_code == 303


async def test_the_refusal_never_says_which_half_was_wrong(client):
    """A message that distinguishes them turns the login into a way of finding usernames."""
    no_such_user = await client.post(
        "/admin/login", data={"username": "nobody", "password": "whatever"}
    )
    real_user_bad_password = await client.post(
        "/admin/login", data={"username": "super", "password": "whatever"}
    )

    assert "do not match" in no_such_user.text
    assert "do not match" in real_user_bad_password.text


async def test_hr_is_not_shown_problems_or_the_attempt_log(client):
    await signed_in_as_hr(client)

    page = await client.get("/admin")

    assert "/admin/problems" not in page.text
    assert "Every attempt" not in page.text


async def test_an_admin_is_shown_both(client):
    await signed_in_hr(client)

    page = await client.get("/admin")
    register = await client.get("/admin/register")

    assert "/admin/problems" in page.text
    assert "Every attempt" in register.text


async def test_hr_cannot_reach_the_admin_only_pages_by_typing_them(client):
    """Hiding a link is not access control.

    Each of these is reachable by address, so each is refused by the route as well as
    missing from the nav. A 404 rather than a 403, so the shape of what exists is not
    readable from outside.
    """
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await punch(client, "TS4012", "alice", "check_in", "2026-11-01T09:00:00Z")
    await client.get("/admin/logout")

    await signed_in_as_hr(client)

    for path in ("/admin/problems", "/admin/register.csv", "/admin/photo/1"):
        assert (await client.get(path)).status_code == 404, path


async def test_hr_gets_the_paired_view_even_when_they_ask_for_the_log(client):
    """?view= is not a permission. Forcing it server-side is what makes the tab honest."""
    await signed_in_as_hr(client)

    page = await client.get("/admin/register?view=attempts")

    assert "One line per check-in" in page.text
    assert "Every attempt, newest first" not in page.text


async def test_a_session_from_before_roles_existed_is_signed_out(client):
    """Such a cookie has no role, and guessing one would hand somebody the wrong access."""
    await signed_in_hr(client)
    assert (await client.get("/admin")).status_code == 200

    # Forge the old shape: signed in, no role.
    client.cookies.clear()

    assert (await client.get("/admin")).status_code == 303


# ---- correcting the record ---------------------------------------------------------------
# The one place a punch can be changed. Admin only, every change audited, and a hand-made
# punch must never read as a face the camera matched.


async def an_event_id(client, employee_id: str = "TS4012") -> int:
    rows = (
        await client.get("/api/v1/attendance", headers={"X-Api-Key": "test-admin-key"})
    ).json()["items"]
    return next(r["id"] for r in rows if r["external_id"] == employee_id)


async def test_hr_cannot_open_or_apply_an_edit(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await punch(client, "TS4012", "alice", "check_in", "2026-11-01T09:00:00Z")
    event_id = await an_event_id(client)
    await client.get("/admin/logout")

    await signed_in_as_hr(client)

    assert (await client.get(f"/admin/attendance/{event_id}/edit")).status_code == 404
    assert (
        await client.post(
            f"/admin/attendance/{event_id}/edit",
            data={"direction": "Out", "happened_at": "2026-11-01T18:00", "reason": "nope"},
        )
    ).status_code == 404
    assert (
        await client.post(
            "/admin/attendance/add",
            data={
                "employee_id": "TS4012",
                "direction": "In",
                "happened_at": "2026-11-01T09:00",
                "reason": "nope",
            },
        )
    ).status_code == 404


async def test_an_edit_changes_the_punch_and_is_recorded(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await punch(client, "TS4012", "alice", "check_in", "2026-11-01T09:00:00Z")
    event_id = await an_event_id(client)

    local = datetime.now().astimezone().tzinfo
    corrected = (datetime.now(local) - timedelta(hours=3)).replace(second=0, microsecond=0)

    response = await client.post(
        f"/admin/attendance/{event_id}/edit",
        data={
            "direction": "Out",
            "happened_at": corrected.strftime("%Y-%m-%dT%H:%M"),
            "reason": "Tapped the wrong button; confirmed with their manager",
        },
    )

    assert response.status_code == 303

    factory = get_session_factory()

    async with factory() as session:
        event = (
            await session.execute(
                select(AttendanceEvent).where(AttendanceEvent.id == event_id)
            )
        ).scalars().one()
        trail = (
            await session.execute(
                select(AttendanceEdit).where(AttendanceEdit.attendance_event_id == event_id)
            )
        ).scalars().all()

    assert event.direction == AttendanceDirection.Out
    assert event.created_at_utc.astimezone(local) == corrected
    assert event.edited_at_utc is not None

    fields = {t.field: t for t in trail}
    assert set(fields) == {"direction", "time"}
    assert fields["direction"].old_value == "In"
    assert fields["direction"].new_value == "Out"
    assert "wrong button" in fields["direction"].reason
    # Shared logins, so the honest answer is the role and the name column stays empty.
    assert fields["direction"].changed_by_role == "admin"
    assert fields["direction"].changed_by is None


async def test_an_edit_without_a_reason_is_refused(client):
    """An edit with no stated reason cannot be told from a mistake six months later."""
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await punch(client, "TS4012", "alice", "check_in", "2026-11-01T09:00:00Z")
    event_id = await an_event_id(client)

    response = await client.post(
        f"/admin/attendance/{event_id}/edit",
        data={"direction": "Out", "happened_at": "2026-11-01T18:00", "reason": "  "},
    )

    assert response.status_code == 400

    factory = get_session_factory()

    async with factory() as session:
        event = (
            await session.execute(
                select(AttendanceEvent).where(AttendanceEvent.id == event_id)
            )
        ).scalars().one()
        trail = (await session.execute(select(AttendanceEdit))).scalars().all()

    assert event.direction == AttendanceDirection.In, "nothing may change"
    assert list(trail) == []


async def test_a_punch_cannot_be_moved_into_the_future(client):
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await punch(client, "TS4012", "alice", "check_in", "2026-11-01T09:00:00Z")
    event_id = await an_event_id(client)

    later = (datetime.now().astimezone() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")

    response = await client.post(
        f"/admin/attendance/{event_id}/edit",
        data={"direction": "In", "happened_at": later, "reason": "trying it on"},
    )

    assert response.status_code == 400


async def test_a_hand_added_punch_never_looks_like_a_face_match(client):
    """It counts for pay, so it must be obvious it was typed rather than recognised."""
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")

    local = datetime.now().astimezone().tzinfo
    when = (datetime.now(local) - timedelta(hours=5)).replace(second=0, microsecond=0)

    response = await client.post(
        "/admin/attendance/add",
        data={
            "employee_id": "TS4012",
            "direction": "In",
            "happened_at": when.strftime("%Y-%m-%dT%H:%M"),
            "reason": "Kiosk was down; let in by reception",
        },
    )

    assert response.status_code == 303

    factory = get_session_factory()

    async with factory() as session:
        event = (
            await session.execute(
                select(AttendanceEvent).order_by(AttendanceEvent.id.desc()).limit(1)
            )
        ).scalars().one()
        trail = (
            await session.execute(
                select(AttendanceEdit).where(
                    AttendanceEdit.attendance_event_id == event.id
                )
            )
        ).scalars().all()

    assert event.device_id == "manual"
    assert event.match_score == 0.0
    assert event.direction == AttendanceDirection.In
    assert event.created_at_utc.astimezone(local) == when
    # It still counts, which is the point of adding it.
    assert event.decision == AttendanceDecision.Accepted

    assert len(trail) == 1
    assert trail[0].field == "created"
    assert "Kiosk was down" in trail[0].reason


async def test_the_decision_cannot_be_edited(client):
    """Turning a refusal into an acceptance would record attendance no camera ever saw."""
    await signed_in_hr(client)
    await enrol(client, "TS4012", "Alice Example", "alice")
    await punch(client, "TS4012", "alice", "check_in", "2026-11-01T09:00:00Z")
    event_id = await an_event_id(client)

    page = await client.get(f"/admin/attendance/{event_id}/edit")

    assert 'name="direction"' in page.text
    assert 'name="happened_at"' in page.text
    assert 'name="decision"' not in page.text


async def test_a_session_without_a_role_does_not_bounce_forever(client):
    """ERR_TOO_MANY_REDIRECTS, reported from a real browser.

    Every browser was holding a cookie that said signed-in and carried no role when roles
    shipped. The guard on /admin demanded a role and sent them to /admin/login; the login
    page saw only the timestamp and sent them straight back. Neither page was wrong on its
    own, and together they were a wall.

    The earlier test for this cleared the cookies first, so it proved nothing about the one
    case that mattered. This one forges the cookie exactly as the middleware writes it.
    """
    import base64
    import json

    import itsdangerous

    stale = base64.b64encode(
        json.dumps({"signed_in_at": datetime.now().astimezone().isoformat()}).encode()
    )
    client.cookies.set(
        "fa_session", itsdangerous.TimestampSigner(SECRET).sign(stale).decode()
    )

    # The login page must render, not redirect.
    landing = await client.get("/admin/login")
    assert landing.status_code == 200
    assert 'name="username"' in landing.text

    # And the guarded page must send them there exactly once.
    guarded = await client.get("/admin")
    assert guarded.status_code == 303
    assert guarded.headers["location"] == "/admin/login"


async def test_a_session_with_a_nonsense_role_is_treated_the_same(client):
    """Same wall, reached by a tampered or half-written cookie rather than an old one."""
    import base64
    import json

    import itsdangerous

    forged = base64.b64encode(
        json.dumps(
            {"signed_in_at": datetime.now().astimezone().isoformat(), "role": "wat"}
        ).encode()
    )
    client.cookies.set(
        "fa_session", itsdangerous.TimestampSigner(SECRET).sign(forged).decode()
    )

    assert (await client.get("/admin/login")).status_code == 200
    assert (await client.get("/admin")).status_code == 303
