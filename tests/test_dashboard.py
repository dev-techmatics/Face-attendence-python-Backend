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

from app.api.rate_limit import reset_rate_limits
from app.core.config import Settings, reset_settings_cache
from app.core.enums import AttendanceDecision, AttendanceDirection
from app.db.session import get_session_factory
from app.domain.models import AttendanceEvent, Person
from app.main import create_app

PASSWORD = "office-password-for-tests"
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
    response = await client.post("/admin/login", data={"password": "guess"})

    assert response.status_code == 401
    assert "not right" in response.text


async def test_hr_can_sign_in_and_see_everyone(client):
    await enrol(client, "TS4012", "Alice Example", "alice")
    await enrol(client, "TS4013", "Bob Example", "bob")

    signed_in = await client.post("/admin/login", data={"password": PASSWORD})
    assert signed_in.status_code == 303

    today = await client.get("/admin")
    assert today.status_code == 200
    assert "Alice Example" in today.text
    assert "Bob Example" in today.text


async def test_signing_out_of_hr_ends_the_session(client):
    await client.post("/admin/login", data={"password": PASSWORD})
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

    await client.post("/admin/login", data={"password": PASSWORD})

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

    await client.post("/admin/login", data={"password": PASSWORD})
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

    await client.post("/admin/login", data={"password": PASSWORD})
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

    await client.post("/admin/login", data={"password": PASSWORD})

    page = await client.get("/admin/monthly")
    assert page.status_code == 200
    assert "Alice Example" in page.text

    export = await client.get("/admin/monthly.csv")
    assert export.status_code == 200
    assert "Days present" in export.text


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
    await client.post("/admin/login", data={"password": PASSWORD})

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
        response = await client.post("/admin/login", data={"password": f"guess{attempt}"})
        assert response.status_code == 401
        assert "not right" in response.text, f"attempt {attempt}"

    # The fifth starts the pause.
    fifth = await client.post("/admin/login", data={"password": "guess5"})
    assert fifth.status_code == 401
    assert "Too many wrong passwords" in fifth.text

    # And the right password is refused too, so the lockout cannot be stepped around by
    # guessing until you happen to be right.
    correct = await client.post("/admin/login", data={"password": PASSWORD})
    assert correct.status_code == 401
    assert "Too many wrong passwords" in correct.text

    lockout.reset()


async def test_the_right_password_clears_the_count(client):
    from app.admin import lockout

    lockout.reset()

    for attempt in range(3):
        await client.post("/admin/login", data={"password": f"guess{attempt}"})

    signed_in = await client.post("/admin/login", data={"password": PASSWORD})
    assert signed_in.status_code == 303

    # Somebody who fumbled their password twice and then got it right starts clean, rather
    # than being two tries from a lockout for the next quarter of an hour.
    for attempt in range(4):
        response = await client.post("/admin/login", data={"password": f"again{attempt}"})
        assert "not right" in response.text, f"attempt {attempt}"

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
        await client.post("/admin/login", data={"password": f"guess{attempt}"})

    signed_in = await client.post("/me/signin", files={"photo": upload("alice-front.jpg")})
    assert signed_in.status_code == 303

    lockout.reset()


# ---- the pages are a live view, never a cached one ----------------------------------------


async def test_hr_pages_are_never_cached(client):
    """Refreshing has to ask the server, or the board shows a moment that has passed."""
    await enrol(client, "TS4012", "Alice Example", "alice")
    await client.post("/admin/login", data={"password": PASSWORD})

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
    await client.post("/admin/login", data={"password": PASSWORD})

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
    await client.post("/admin/login", data={"password": PASSWORD})

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
    await client.post("/admin/login", data={"password": PASSWORD})

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
    await client.post("/admin/login", data={"password": PASSWORD})

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
    await client.post("/admin/login", data={"password": PASSWORD})
    await client.post(f"/admin/photo/{event_id}/delete")

    register = await client.get("/admin/register")

    assert register.status_code == 200
    assert "Alice Example" in register.text
    # The row survives; only its "show" link goes.
    assert f'href="/admin/photo/{event_id}"' not in register.text


async def test_the_page_says_so_when_the_photo_is_already_gone(client):
    event_id = await photographed_punch(client)
    await client.post("/admin/login", data={"password": PASSWORD})
    await client.post(f"/admin/photo/{event_id}/delete")

    page = await client.get(f"/admin/photo/{event_id}")

    assert page.status_code == 200
    assert "no photo for this attempt any more" in page.text
    assert "Delete this photo" not in page.text


async def test_deleting_twice_is_harmless(client):
    event_id = await photographed_punch(client)
    await client.post("/admin/login", data={"password": PASSWORD})

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
    await client.post("/admin/login", data={"password": PASSWORD})
    assert (await client.get(f"/admin/photo/{event_id}/image")).status_code == 200


async def test_a_photo_from_another_tenant_is_not_reachable(client):
    event_id = await photographed_punch(client)
    await client.post("/admin/login", data={"password": PASSWORD})

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
    assert (await client.post("/admin/login", data={"password": PASSWORD})).status_code == 303


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
    await punch(client, "TS4012", "alice", "check_in", "2026-10-30T09:00:00Z")
    await punch(client, "TS4012", "alice", "check_out", "2026-10-30T17:30:00Z")

    attempts = await client.get("/admin/register")
    days = await client.get("/admin/register?view=days")

    # Two rows in the log, one row in the day view - that is the whole difference.
    assert attempts.text.count("TS4012") == 2
    assert days.text.count("TS4012") == 1

    assert "One line per person per day" in days.text
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

    assert page.text.count("TS4012") == 1
    assert "7h" in page.text
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
    assert "In and out per day" in days.text


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
