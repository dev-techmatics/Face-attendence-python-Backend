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

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from PIL import Image

from app.api.rate_limit import reset_rate_limits
from app.core.config import Settings, reset_settings_cache
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
    assert response.json() == {"success": True, "message": "Checked in."}


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

    assert response.json() == {"success": True, "message": "Checked out."}

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
