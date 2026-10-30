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
from sqlalchemy import select

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
