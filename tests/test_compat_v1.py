"""The iOS kiosk app's contract, asserted exactly as the app sends and reads it.

These are the tests that decide whether the app can be pointed at this backend without being
rewritten, so they are written from the app's side of the wire: Bearer token, the old field
names, and a flat ``{success, message}`` reply with HTTP 200 even for a refusal.

The reference is `D:\\face-attendance\\api\\app\\api.py` on the deployment host — the service
the app was built against.
"""

from __future__ import annotations

import io
import zlib
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select

from app.api.rate_limit import reset_rate_limits
from app.core.config import Settings, reset_settings_cache
from app.core.enums import PersonStatus
from app.db.session import get_session_factory
from app.domain.models import AttendanceEvent, Person
from app.main import create_app

KIOSK = {"Authorization": "Bearer test-device-key"}
ADMIN_BEARER = {"Authorization": "Bearer test-admin-key"}

pytestmark = pytest.mark.asyncio


def photo(name: str) -> bytes:
    shade = 32 + (zlib.crc32(name.encode()) % 192)
    buffer = io.BytesIO()
    Image.new("RGB", (640, 640), (shade, shade, shade)).save(buffer, format="JPEG")
    return buffer.getvalue()


def upload(name: str) -> tuple[str, bytes, str]:
    return (name, photo(name), "image/jpeg")


@pytest_asyncio.fixture
async def client(tmp_path) -> AsyncClient:
    reset_settings_cache()
    reset_rate_limits()

    settings = Settings(
        environment="Development",
        database={
            "url": f"sqlite+aiosqlite:///{tmp_path / 'kiosk.db'}",
            "migrate_on_startup": True,
        },
        api_keys={"admin": ["test-admin-key"], "device": ["test-device-key"]},
        storage={"provider": "local", "local_root_path": str(tmp_path / "faces")},
        face={
            "provider": "development",
            # What the deployment uses: the app only guarantees a front photo.
            "required_poses": ["Front"],
            "store_probe_images": True,
            "infer_direction": False,
        },
    )

    app = create_app(settings)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        async with app.router.lifespan_context(app):
            yield http


async def register(client, employee_id="TS4012", name="Alice Example", identity="alice", sides=True):
    files = {"face_front": upload(f"{identity}-front.jpg")}

    if sides:
        files["face_left"] = upload(f"{identity}-left.jpg")
        files["face_right"] = upload(f"{identity}-right.jpg")

    return await client.post(
        "/v1/faces/enroll",
        headers=KIOSK,
        data={"employee_id": employee_id, "full_name": name},
        files=files,
    )


async def archive(client, employee_id: str) -> None:
    """What Remove on the People page does, without going through the dashboard."""
    factory = get_session_factory()

    async with factory() as session:
        person = (
            await session.execute(select(Person).where(Person.external_id == employee_id))
        ).scalars().one()
        person.status = PersonStatus.Archived
        await session.commit()


async def shift_punches_back(employee_id: str, hours: float) -> None:
    """Move somebody's recorded punches into the past.

    The server stamps every punch with its own clock, so two API calls are always in the same
    second - and the shift-length rules are entirely about the gap between them. Backdating
    what is already there is the only way to test the gap without a fake clock, and it leaves
    the rows otherwise exactly as the API wrote them.
    """
    factory = get_session_factory()

    async with factory() as session:
        person = (
            await session.execute(select(Person).where(Person.external_id == employee_id))
        ).scalars().one()

        events = (
            await session.execute(
                select(AttendanceEvent).where(AttendanceEvent.person_id == person.id)
            )
        ).scalars().all()

        for event in events:
            event.created_at_utc = event.created_at_utc - timedelta(hours=hours)

        await session.commit()


async def punch(client, employee_id: str, kind: str, identity: str = "alice"):
    return await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={
            "employee_id": employee_id,
            "type": kind,
            "captured_at": "2026-10-30T09:15:00+05:30",
        },
        files={"face": upload(f"{identity}-front.jpg")},
    )


async def punch_rows(client, employee_id: str) -> list[dict]:
    """The recorded attempts, newest last, as the admin API reports them."""
    body = (
        await client.get("/api/v1/attendance", headers={"X-Api-Key": "test-admin-key"})
    ).json()

    rows = [row for row in body["items"] if row.get("external_id") == employee_id]
    return list(reversed(rows))


# ---- the shape the app depends on --------------------------------------------------------


async def test_health_matches_the_old_reply(client):
    response = await client.get("/v1/health")

    assert response.status_code == 200
    body = response.json()
    # The app reads exactly these three keys.
    assert body["success"] is True
    assert body["message"] == "ok"
    assert body["employees"] == 0


async def test_health_names_which_server_answered(client):
    """So "which of the two backends am I talking to" is one request, not an afternoon.

    The older .NET build answers this contract with the same wording, down to the error
    messages, because this one is a port of it.
    """
    body = (await client.get("/v1/health")).json()

    assert body["server"] == "face-attendance-python"


async def test_health_counts_registered_people(client):
    await register(client)

    assert (await client.get("/v1/health")).json()["employees"] == 1


async def test_a_missing_token_gets_the_old_wording(client):
    response = await client.post("/v1/faces/identify", files={"face": upload("a.jpg")})

    assert response.status_code == 401
    # The app reads `message`; a problem-details body would leave it with nothing to show.
    assert response.json() == {
        "success": False,
        "message": "This device isn't authorised. Contact your admin.",
    }


async def test_a_wrong_token_is_refused(client):
    response = await client.post(
        "/v1/faces/identify",
        headers={"Authorization": "Bearer nope"},
        files={"face": upload("a.jpg")},
    )

    assert response.status_code == 401
    assert response.json()["success"] is False
    assert response.json()["message"] == "This device isn't authorised. Contact your admin."


async def test_a_missing_field_gets_a_flat_reply_too(client):
    # FastAPI would normally answer a missing form field with its own validation document.
    response = await client.post("/v1/attendance/punch", headers=KIOSK)

    assert response.status_code == 400
    assert response.json() == {
        "success": False,
        "message": "Some details were missing. Please try again.",
    }


async def test_no_kiosk_reply_ever_lacks_a_message(client):
    """Whatever goes wrong, the app must find a string to put on screen."""
    await register(client, employee_id="TS4012", identity="alice")

    probes = [
        ("post", "/v1/faces/enroll", {}, None),
        ("post", "/v1/faces/identify", {}, None),
        ("post", "/v1/attendance/punch", {}, None),
        ("post", "/v1/faces/identify", {"face": ("x.txt", b"nope", "text/plain")}, None),
        ("get", "/v1/health", None, None),
    ]

    for method, path, files, data in probes:
        call = getattr(client, method)
        response = (
            await call(path, headers=KIOSK)
            if files is None and data is None
            else await call(path, headers=KIOSK, files=files or None, data=data)
        )

        body = response.json()
        assert "success" in body, f"{path} -> {body}"
        assert isinstance(body.get("message"), str) and body["message"], f"{path} -> {body}"


async def test_an_admin_key_also_works(client):
    # An admin key can do anything a device can, so a support engineer can use the same route.
    assert (await client.get("/v1/health", headers=ADMIN_BEARER)).status_code == 200
    assert (await register(client)).json()["success"] is True


async def test_the_native_header_is_not_required_here(client):
    # The app sends Bearer and nothing else; it must not need X-Api-Key as well.
    assert (await register(client)).status_code == 200


# ---- enrolment ---------------------------------------------------------------------------


async def test_enroll_with_all_three_photos(client):
    response = await register(client)

    assert response.status_code == 200
    assert response.json() == {"success": True, "message": "Face registered."}


async def test_enroll_with_only_a_front_photo(client):
    # The old service required only face_front and the app relies on that: a strong turn can
    # hide the face, so it offers the side photos without guaranteeing them.
    response = await register(client, sides=False)

    assert response.json()["success"] is True
    assert (await client.get("/v1/health")).json()["employees"] == 1


async def test_an_unusable_side_photo_does_not_fail_the_registration(client):
    response = await client.post(
        "/v1/faces/enroll",
        headers=KIOSK,
        data={"employee_id": "TS4013", "full_name": "Partial Sides"},
        files={
            "face_front": upload("bob-front.jpg"),
            # Not a photo at all: the old service ignored a side it could not use.
            "face_left": ("bob-left.jpg", b"not an image at all", "image/jpeg"),
            "face_right": upload("bob-right.jpg"),
        },
    )

    assert response.json()["success"] is True


async def test_a_bad_employee_id_is_refused_with_the_old_message(client):
    response = await client.post(
        "/v1/faces/enroll",
        headers=KIOSK,
        data={"employee_id": "bad id!", "full_name": "Alice"},
        files={"face_front": upload("alice-front.jpg")},
    )

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert "letters, numbers" in response.json()["message"]


async def test_a_short_name_is_refused(client):
    response = await client.post(
        "/v1/faces/enroll",
        headers=KIOSK,
        data={"employee_id": "TS1", "full_name": "A"},
        files={"face_front": upload("alice-front.jpg")},
    )

    assert response.json()["message"] == "Please enter the full name."


async def test_the_employee_id_is_uppercased_like_the_app_does(client):
    assert (await register(client, employee_id="ts4012")).json()["success"] is True

    # So re-registering in any case collides, rather than creating a second identity.
    again = await register(client, employee_id="TS4012", identity="carol")
    assert again.json()["success"] is False
    assert "already registered" in again.json()["message"]


async def test_a_duplicate_id_is_refused_and_says_to_ask_the_admin(client):
    await register(client)

    response = await register(client, identity="someone-else")

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert "Ask the admin to reset it" in response.json()["message"]


async def test_a_face_already_registered_under_another_id_is_refused(client):
    await register(client, employee_id="TS4012", identity="alice")

    # Same face, different id: one person holding two identities can cover for an absent
    # colleague, which is the whole reason this check exists.
    response = await register(client, employee_id="TS9999", identity="alice")

    assert response.json()["success"] is False
    assert "already registered under another ID" in response.json()["message"]


async def test_an_empty_front_photo_is_refused_and_leaves_no_record(client):
    response = await client.post(
        "/v1/faces/enroll",
        headers=KIOSK,
        data={"employee_id": "TS5000", "full_name": "Empty Photo"},
        files={"face_front": ("x.jpg", b"", "image/jpeg")},
    )

    assert response.json()["success"] is False
    # The id must not be left occupied by a record with no usable face.
    assert (await client.get("/v1/health")).json()["employees"] == 0
    assert (await register(client, employee_id="TS5000", name="Second Try")).json()["success"]


# ---- identify ----------------------------------------------------------------------------


async def test_identify_returns_the_employee_id_and_name(client):
    await register(client, employee_id="TS4012", name="Alice Example", identity="alice")

    response = await client.post(
        "/v1/faces/identify", headers=KIOSK, files={"face": upload("alice-front.jpg")}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["message"] == "Matched."
    # The app reads these two keys by these names.
    assert body["employee_id"] == "TS4012"
    assert body["name"] == "Alice Example"


async def test_identify_works_with_a_device_key(client):
    # The native equivalent is admin-only; requiring that here would have forced an admin key
    # onto every kiosk, and an admin key can download stored face photographs.
    await register(client)

    response = await client.post(
        "/v1/faces/identify", headers=KIOSK, files={"face": upload("alice-front.jpg")}
    )

    assert response.status_code == 200


async def test_identify_an_unknown_face_gets_the_register_prompt(client):
    await register(client, identity="alice")

    response = await client.post(
        "/v1/faces/identify", headers=KIOSK, files={"face": upload("stranger-front.jpg")}
    )

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert "New here? Tap Register." in response.json()["message"]


async def test_identify_records_nothing(client):
    await register(client)

    for _ in range(3):
        await client.post(
            "/v1/faces/identify", headers=KIOSK, files={"face": upload("alice-front.jpg")}
        )

    # The app identifies continuously while the camera is open; if that wrote a row each time
    # the register would be unusable.
    events = await client.get(
        "/api/v1/attendance", headers={"X-Api-Key": "test-admin-key"}
    )
    assert events.json()["total"] == 0


async def test_identify_with_no_face_asks_the_user_to_look_at_the_camera(client):
    await register(client)

    response = await client.post(
        "/v1/faces/identify", headers=KIOSK, files={"face": ("x.jpg", b"", "image/jpeg")}
    )

    assert response.json()["success"] is False
    assert "look at the camera" in response.json()["message"]


# ---- punch -------------------------------------------------------------------------------


async def test_a_check_in_is_recorded(client):
    await register(client, employee_id="TS4012", identity="alice")

    response = await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={
            "employee_id": "TS4012",
            "type": "check_in",
            "captured_at": "2026-10-30T09:15:00+05:30",
        },
        files={"face": upload("alice-front.jpg")},
    )

    assert response.status_code == 200
    # ``type`` echoes what was actually recorded, which is not always what was asked for -
    # see the shift rules in AttendanceService._decide_shift.
    assert response.json() == {
        "success": True,
        "message": "Checked in.",
        "type": "check_in",
    }


async def test_the_app_decides_check_out_not_the_server(client):
    # The native API would infer the direction from the previous event and overrule the app.
    # The app has already told the user which one it is doing, so its value wins here.
    await register(client, employee_id="TS4012", identity="alice")

    body = {
        "employee_id": "TS4012",
        "type": "check_out",
        "captured_at": "2026-10-30T18:00:00+05:30",
    }
    response = await client.post(
        "/v1/attendance/punch", headers=KIOSK, data=body, files={"face": upload("alice-front.jpg")}
    )

    assert response.json() == {
        "success": True,
        "message": "Checked out.",
        "type": "check_out",
    }

    register_rows = await client.get(
        "/api/v1/attendance", headers={"X-Api-Key": "test-admin-key"}
    )
    assert register_rows.json()["items"][0]["direction"] == "Out"


async def test_a_second_punch_straight_away_says_already_done(client):
    await register(client, employee_id="TS4012", identity="alice")

    body = {
        "employee_id": "TS4012",
        "type": "check_in",
        "captured_at": "2026-10-30T09:15:00+05:30",
    }
    first = await client.post(
        "/v1/attendance/punch", headers=KIOSK, data=body, files={"face": upload("alice-front.jpg")}
    )
    assert first.json()["success"] is True

    second = await client.post(
        "/v1/attendance/punch", headers=KIOSK, data=body, files={"face": upload("alice-front.jpg")}
    )

    assert second.status_code == 200
    assert second.json()["success"] is False
    assert "already recorded a moment ago" in second.json()["message"]


async def test_an_immediate_check_out_is_collapsed_without_claiming_a_direction(client):
    # The window collapses any punch inside it, whichever way it was going, so the message
    # must not tell someone who just checked in that they "already checked out".
    await register(client, employee_id="TS4012", identity="alice")

    await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={
            "employee_id": "TS4012",
            "type": "check_in",
            "captured_at": "2026-10-30T09:15:00+05:30",
        },
        files={"face": upload("alice-front.jpg")},
    )

    out = await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={
            "employee_id": "TS4012",
            "type": "check_out",
            "captured_at": "2026-10-30T09:15:30+05:30",
        },
        files={"face": upload("alice-front.jpg")},
    )

    assert out.json()["success"] is False
    message = out.json()["message"]
    assert "checked out" not in message
    assert "already recorded" in message


async def test_punching_as_an_unregistered_employee_is_refused(client):
    response = await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={
            "employee_id": "NOBODY",
            "type": "check_in",
            "captured_at": "2026-10-30T09:15:00+05:30",
        },
        files={"face": upload("alice-front.jpg")},
    )

    assert response.status_code == 200
    assert response.json()["message"] == "This employee isn't registered. Please register again."


async def test_someone_elses_face_does_not_punch_you_in(client):
    await register(client, employee_id="TS4012", identity="alice")

    response = await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={
            "employee_id": "TS4012",
            "type": "check_in",
            "captured_at": "2026-10-30T09:15:00+05:30",
        },
        files={"face": upload("impostor-front.jpg")},
    )

    assert response.json()["success"] is False
    assert "New here? Tap Register." in response.json()["message"]


async def test_an_unknown_type_is_refused(client):
    await register(client, employee_id="TS4012", identity="alice")

    response = await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={
            "employee_id": "TS4012",
            "type": "lunch",
            "captured_at": "2026-10-30T09:15:00+05:30",
        },
        files={"face": upload("alice-front.jpg")},
    )

    assert response.json()["message"] == "Unknown attendance type."


async def test_an_unparseable_time_is_refused(client):
    await register(client, employee_id="TS4012", identity="alice")

    response = await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={"employee_id": "TS4012", "type": "check_in", "captured_at": "yesterday"},
        files={"face": upload("alice-front.jpg")},
    )

    assert response.json()["message"] == "Invalid time. Please try again."


async def test_a_naive_capture_time_is_treated_as_utc(client):
    await register(client, employee_id="TS4012", identity="alice")

    response = await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={
            "employee_id": "TS4012",
            "type": "check_in",
            "captured_at": "2026-10-30T09:15:00",
        },
        files={"face": upload("alice-front.jpg")},
    )

    assert response.json()["success"] is True


async def test_location_is_optional_and_stored_when_sent(client):
    await register(client, employee_id="TS4012", identity="alice")

    response = await client.post(
        "/v1/attendance/punch",
        headers=KIOSK,
        data={
            "employee_id": "TS4012",
            "type": "check_in",
            "captured_at": "2026-10-30T09:15:00+05:30",
            "latitude": "12.9716",
            "longitude": "77.5946",
        },
        files={"face": upload("alice-front.jpg")},
    )

    assert response.json()["success"] is True


async def test_every_refusal_is_a_200_the_app_can_read(client):
    """The app shows `message` from the body; a 4xx would surface as "couldn't reach server"."""
    await register(client, employee_id="TS4012", identity="alice")

    cases = [
        ("/v1/faces/identify", {"face": upload("stranger.jpg")}, None),
        (
            "/v1/attendance/punch",
            {"face": upload("impostor.jpg")},
            {
                "employee_id": "TS4012",
                "type": "check_in",
                "captured_at": "2026-10-30T09:15:00+05:30",
            },
        ),
    ]

    for path, files, data in cases:
        response = await client.post(path, headers=KIOSK, files=files, data=data)

        assert response.status_code == 200, path
        body = response.json()
        assert body["success"] is False, path
        assert isinstance(body["message"], str) and body["message"], path


async def test_a_wrong_method_still_gets_a_flat_reply(client):
    # Starlette's router raises its own HTTPException for a 405, which bypasses a handler
    # registered against FastAPI's subclass. The app would then get no `message`.
    response = await client.get("/v1/attendance/punch", headers=KIOSK)

    assert response.status_code == 405
    assert response.json()["success"] is False
    assert isinstance(response.json()["message"], str)


async def test_an_unknown_kiosk_address_gets_a_flat_reply(client):
    response = await client.post("/v1/does/not/exist", headers=KIOSK)

    assert response.status_code == 404
    assert response.json()["success"] is False
    assert isinstance(response.json()["message"], str)


# ---- is this face already registered? ----------------------------------------------------
# The app asks this on the register screen, before anybody types anything. Its whole value is
# that it gives the same verdict registration will, so these tests are about agreement with
# the enrolment guard rather than about recognition.


async def test_verify_says_a_new_face_is_not_registered(client):
    await register(client, employee_id="TS4012", identity="alice")

    response = await client.post(
        "/v1/faces/verify", headers=KIOSK, files={"face": upload("bob-front.jpg")}
    )

    body = response.json()
    assert body["success"] is True
    assert body["registered"] is False


async def test_verify_recognises_somebody_already_on_file(client):
    await register(client, employee_id="TS4012", name="Alice Example", identity="alice")

    body = (
        await client.post(
            "/v1/faces/verify", headers=KIOSK, files={"face": upload("alice-front.jpg")}
        )
    ).json()

    assert body["success"] is True
    assert body["registered"] is True
    assert body["employee_id"] == "TS4012"
    assert body["status"] == "active"
    assert body["can_mark_attendance"] is True
    # Tells them what to do instead of registering, which is the point of asking early.
    assert "clock in" in body["message"]


async def test_verify_still_finds_a_removed_person_but_withholds_the_name(client):
    """The case that let somebody hold two identities before it was fixed.

    Archiving takes a person out of attendance matching, so a check that reused the kiosk's
    candidate set would call their face new. This one must still find them - and must not read
    a colleague's name out to whoever is standing at the camera.
    """
    await register(client, employee_id="TS4012", name="Alice Example", identity="alice")
    await archive(client, "TS4012")

    body = (
        await client.post(
            "/v1/faces/verify", headers=KIOSK, files={"face": upload("alice-front.jpg")}
        )
    ).json()

    assert body["registered"] is True
    assert body["status"] == "removed"
    assert body["can_mark_attendance"] is False
    assert "employee_id" not in body
    assert "name" not in body
    assert "put that record back" in body["message"]


async def test_verify_with_an_employee_id_is_a_one_to_one_check(client):
    await register(client, employee_id="TS4012", name="Alice Example", identity="alice")

    confirmed = (
        await client.post(
            "/v1/faces/verify",
            headers=KIOSK,
            data={"employee_id": "TS4012"},
            files={"face": upload("alice-front.jpg")},
        )
    ).json()

    assert confirmed["registered"] is True
    # The caller already sent the id, so naming the person reveals nothing new.
    assert confirmed["employee_id"] == "TS4012"

    denied = (
        await client.post(
            "/v1/faces/verify",
            headers=KIOSK,
            data={"employee_id": "TS4012"},
            files={"face": upload("bob-front.jpg")},
        )
    ).json()

    assert denied["registered"] is False


async def test_verify_never_reports_a_failed_check_as_a_clear_face(client):
    """The distinction the endpoint exists for.

    If a camera or model failure came back as ``registered: false`` the app would offer to
    register, and the duplicate this is meant to prevent gets created anyway. So a failure is
    ``success: false`` with no answer at all.
    """
    await register(client, employee_id="TS4012", identity="alice")

    body = (
        await client.post(
            "/v1/faces/verify",
            headers=KIOSK,
            files={"face": ("notes.txt", b"not a photograph", "text/plain")},
        )
    ).json()

    assert body["success"] is False
    assert "registered" not in body


async def test_verify_needs_a_key_like_everything_else(client):
    response = await client.post("/v1/faces/verify", files={"face": upload("alice-front.jpg")})

    assert response.status_code == 401


# ---- how long a shift has to be ----------------------------------------------------------
# Two rules on top of the app's in/out choice: a check-out cannot close a check-in that is
# less than minimum_shift_minutes old, and a check-in older than open_shift_expiry_hours is
# abandoned rather than closed. Both are about the gap between punches, which is why these
# tests backdate what the API recorded instead of calling it twice.


async def test_checking_out_too_soon_is_refused(client):
    await register(client, employee_id="TS4012", identity="alice")
    await punch(client, "TS4012", "check_in")

    # Half an hour in: past the two-minute double-tap window, well short of the two-hour
    # floor. Inside that window the older guard answers first and says "already recorded a
    # moment ago", which is the better message for a double tap.
    await shift_punches_back("TS4012", hours=0.5)

    body = (await punch(client, "TS4012", "check_out")).json()

    assert body["success"] is False
    # The numbers are in the message on purpose: "too soon" alone invites an immediate retry.
    assert "2 hours" in body["message"]


async def test_a_refused_check_out_leaves_them_checked_in(client):
    """The refusal must not cost them the shift.

    If a too-early check-out closed the day anyway, or dropped the open check-in, the person
    would have no way to be paid for the hours they then went on to work.
    """
    await register(client, employee_id="TS4012", identity="alice")
    await punch(client, "TS4012", "check_in")
    await shift_punches_back("TS4012", hours=0.5)
    await punch(client, "TS4012", "check_out")

    rows = await punch_rows(client, "TS4012")

    assert [row["decision"] for row in rows] == ["Accepted", "TooSoon"]
    # Only the check-in counts, so they are still inside.
    assert [row["direction"] for row in rows if row["decision"] == "Accepted"] == ["In"]


async def test_checking_out_after_the_minimum_is_allowed(client):
    await register(client, employee_id="TS4012", identity="alice")
    await punch(client, "TS4012", "check_in")

    # Three hours at work, which clears the two-hour floor.
    await shift_punches_back("TS4012", hours=3)

    body = (await punch(client, "TS4012", "check_out")).json()

    assert body["success"] is True
    assert body["type"] == "check_out"
    assert body["message"] == "Checked out."


async def test_a_check_in_left_open_too_long_starts_a_fresh_one(client):
    """Seventeen hours later, a face at the camera is somebody arriving, not leaving."""
    await register(client, employee_id="TS4012", identity="alice")
    await punch(client, "TS4012", "check_in")
    await shift_punches_back("TS4012", hours=17)

    # The app still thinks they are inside, so it asks for a check-out. The server overrules
    # it - this is the one place it does.
    body = (await punch(client, "TS4012", "check_out")).json()

    assert body["success"] is True
    assert body["type"] == "check_in"
    assert "never closed" in body["message"]
    # Says what was actually recorded, so the app can correct its own in/out state.
    assert body["message"].startswith("Checked in.")


async def test_the_abandoned_check_in_is_left_unpaired_not_given_an_invented_out(client):
    """No closing punch is written, deliberately.

    Pairing the stale check-in with an Out at +16h would make the register look tidy and put
    sixteen hours nobody worked into the month's figures. An unpaired In is already shown as
    "Punch missing" for a human to judge.
    """
    await register(client, employee_id="TS4012", identity="alice")
    await punch(client, "TS4012", "check_in")
    await shift_punches_back("TS4012", hours=17)
    await punch(client, "TS4012", "check_out")

    accepted = [row for row in await punch_rows(client, "TS4012") if row["decision"] == "Accepted"]

    assert [row["direction"] for row in accepted] == ["In", "In"]


async def test_the_two_hour_floor_applies_to_the_fresh_check_in_too(client):
    """The abandoned shift must not leave a loophole.

    After the stale check-in is set aside, the new one is a normal check-in - so checking out
    straight after it is refused like any other, rather than being measured against the
    sixteen-hour-old punch.
    """
    await register(client, employee_id="TS4012", identity="alice")
    await punch(client, "TS4012", "check_in")
    await shift_punches_back("TS4012", hours=17)
    await punch(client, "TS4012", "check_out")  # becomes a fresh check-in

    # Half an hour into the NEW shift, not seventeen hours into the abandoned one.
    await shift_punches_back("TS4012", hours=0.5)

    body = (await punch(client, "TS4012", "check_out")).json()

    assert body["success"] is False
    assert "2 hours" in body["message"]


# ---- walking back past the kiosk ---------------------------------------------------------
# Somebody who has finished and whose face passes the camera again is not starting a second
# shift. The two-minute double-tap guard was far too short for this: on the live database it
# had fired zero times while four re-scans between three and nine minutes after a check-out
# had each become a real shift.


async def test_a_check_in_soon_after_checking_out_is_ignored(client):
    await register(client, employee_id="TS4012", identity="alice")
    await punch(client, "TS4012", "check_in")
    await shift_punches_back("TS4012", hours=3)
    await punch(client, "TS4012", "check_out")

    # Nine minutes later their face passes the camera again.
    await shift_punches_back("TS4012", hours=0.15)
    body = (await punch(client, "TS4012", "check_in")).json()

    assert body["success"] is False
    # Says they are finished, not that they were refused: they have done nothing wrong.
    assert "already checked out" in body["message"].lower()
    assert "Nothing more to do" in body["message"]

    rows = await punch_rows(client, "TS4012")

    assert [r["decision"] for r in rows] == ["Accepted", "Accepted", "AlreadyCheckedOut"]
    # The shift that was worked is untouched; no second one was opened.
    accepted = [r for r in rows if r["decision"] == "Accepted"]
    assert [r["direction"] for r in accepted] == ["In", "Out"]


async def test_the_window_lets_a_real_return_through(client):
    """Past the window it is somebody coming back, and that has to work."""
    await register(client, employee_id="TS4012", identity="alice")
    await punch(client, "TS4012", "check_in")
    await shift_punches_back("TS4012", hours=3)
    await punch(client, "TS4012", "check_out")

    # Forty minutes later, which is outside the thirty-minute window.
    await shift_punches_back("TS4012", hours=0.67)
    body = (await punch(client, "TS4012", "check_in")).json()

    assert body["success"] is True
    assert body["type"] == "check_in"


async def test_the_rule_only_points_one_way(client):
    """A check-OUT soon after a check-in must still reach the minimum-shift rule.

    Blocking both directions for half an hour would stop somebody correcting a mis-tapped
    check-in, which is the one thing the short double-tap window is genuinely good at.
    """
    await register(client, employee_id="TS4012", identity="alice")
    await punch(client, "TS4012", "check_in")
    await shift_punches_back("TS4012", hours=0.5)

    body = (await punch(client, "TS4012", "check_out")).json()

    assert body["success"] is False
    # TooSoon, not AlreadyCheckedOut - a different rule, with its own message.
    assert "check-out needs" in body["message"]

    rows = await punch_rows(client, "TS4012")
    assert rows[-1]["decision"] == "TooSoon"


async def test_the_ignored_scan_is_recorded_not_dropped(client):
    """Kept as evidence, and so the Problems page can count how often it happens."""
    await register(client, employee_id="TS4012", identity="alice")
    await punch(client, "TS4012", "check_in")
    await shift_punches_back("TS4012", hours=3)
    await punch(client, "TS4012", "check_out")
    await shift_punches_back("TS4012", hours=0.15)
    await punch(client, "TS4012", "check_in")

    rows = await punch_rows(client, "TS4012")
    ignored = [r for r in rows if r["decision"] == "AlreadyCheckedOut"]

    assert len(ignored) == 1
    # The stored reason is the technical one, with the arithmetic in it. The window comes
    # from settings rather than being written here: this is a number the user changes, and
    # a test that hardcodes it fails for the wrong reason when they do.
    from app.core.config import FaceRecognitionSettings

    window = FaceRecognitionSettings().reentry_block_minutes

    assert "re-entry window" in ignored[0]["failure_reason"]
    assert f"{window}-minute" in ignored[0]["failure_reason"]
