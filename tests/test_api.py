"""End-to-end HTTP tests against the real app, using the development provider.

The development provider matches on upload file name, so ``alice-front.jpg`` /
``alice-left.jpg`` / ``alice-right.jpg`` enrol as one person and verify against each other.
That is enough to cover routing, authentication, multipart handling and the decision codes
without any model file.
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

ADMIN = {"X-Api-Key": "test-admin-key"}
DEVICE = {"X-Api-Key": "test-device-key"}

pytestmark = pytest.mark.asyncio


def photo(shade: int = 128, width: int = 640, height: int = 640) -> bytes:
    """A real JPEG, so signature sniffing and decoding both succeed."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (shade, shade, shade)).save(buffer, format="JPEG")
    return buffer.getvalue()


def upload(name: str) -> tuple[str, bytes, str]:
    """An upload whose bytes are distinct per file name.

    The duplicate-frame guard compares content hashes, so three slots carrying the identical
    picture are refused by design - which means the fixtures have to differ for real.
    """
    shade = 32 + (zlib.crc32(name.encode()) % 192)
    return (name, photo(shade), "image/jpeg")


@pytest_asyncio.fixture
async def client(tmp_path) -> AsyncClient:
    reset_settings_cache()
    reset_rate_limits()

    settings = Settings(
        environment="Development",
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "migrate_on_startup": True},
        api_keys={"admin": ["test-admin-key"], "device": ["test-device-key"]},
        storage={"provider": "local", "local_root_path": str(tmp_path / "faces")},
        face={"provider": "development", "store_probe_images": True, "enforce_pose_check": True},
    )

    app = create_app(settings)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        # The transport runs the lifespan lazily, so hit health first to force startup.
        async with app.router.lifespan_context(app):
            yield http_client


async def test_health_is_anonymous(client):
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["database"] == "healthy"


async def test_endpoints_require_an_api_key(client):
    response = await client.get("/api/v1/persons")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_a_device_key_cannot_enrol(client):
    # A device key is the one most likely to leak, so it must not be able to enrol a face.
    response = await client.post(
        "/api/v1/persons",
        headers=DEVICE,
        json={"external_id": "EMP-1", "full_name": "Alice"},
    )

    assert response.status_code == 403


async def test_create_person_then_fetch_it(client):
    created = await client.post(
        "/api/v1/persons",
        headers=ADMIN,
        json={"external_id": "EMP-1", "full_name": "Alice", "email": "alice@example.com"},
    )

    assert created.status_code == 201
    body = created.json()
    assert body["enrollment_status"] == "NotEnrolled"

    fetched = await client.get(f"/api/v1/persons/{body['id']}", headers=ADMIN)

    assert fetched.status_code == 200
    assert fetched.json()["external_id"] == "EMP-1"

    by_external = await client.get("/api/v1/persons/by-external-id/EMP-1", headers=ADMIN)
    assert by_external.json()["id"] == body["id"]


async def test_duplicate_external_id_is_a_conflict(client):
    payload = {"external_id": "EMP-2", "full_name": "Bob"}

    assert (await client.post("/api/v1/persons", headers=ADMIN, json=payload)).status_code == 201

    clash = await client.post("/api/v1/persons", headers=ADMIN, json=payload)

    assert clash.status_code == 409
    assert clash.json()["code"] == "external_id_exists"


async def test_register_all_three_poses_and_mark_attendance(client):
    registration = await client.post(
        "/api/v1/registrations",
        headers=ADMIN,
        data={"external_id": "EMP-10", "full_name": "Alice Example"},
        files={
            "front": upload("alice-front.jpg"),
            "left": upload("alice-left.jpg"),
            "right": upload("alice-right.jpg"),
        },
    )

    assert registration.status_code == 201, registration.text
    body = registration.json()
    assert body["enrollment_completed"] is True
    assert body["enrollment_status"] == "Enrolled"
    assert [c["pose"] for c in body["captures"]] == ["Front", "Left", "Right"]

    person_id = body["person"]["id"]

    enrollment = await client.get(f"/api/v1/persons/{person_id}/enrollment", headers=ADMIN)
    assert enrollment.status_code == 200
    assert len(enrollment.json()["poses"]) == 3
    assert enrollment.json()["missing_poses"] == []

    # 1:1 verification: the device says who this is, the face confirms it.
    punch = await client.post(
        "/api/v1/attendance/mark",
        headers=DEVICE,
        data={"person_id": str(person_id), "device_id": "kiosk-1"},
        files={"image": upload("alice-front.jpg")},
    )

    assert punch.status_code == 200, punch.text
    assert punch.json()["success"] is True
    assert punch.json()["decision"] == "Accepted"
    assert punch.json()["direction"] == "In"


async def test_a_second_punch_inside_the_window_is_collapsed(client):
    await client.post(
        "/api/v1/registrations",
        headers=ADMIN,
        data={"external_id": "EMP-11", "full_name": "Bob Example"},
        files={
            "front": upload("bob-front.jpg"),
            "left": upload("bob-left.jpg"),
            "right": upload("bob-right.jpg"),
        },
    )

    first = await client.post(
        "/api/v1/attendance/mark",
        headers=DEVICE,
        data={"external_id": "EMP-11"},
        files={"image": upload("bob-front.jpg")},
    )
    assert first.json()["decision"] == "Accepted"

    second = await client.post(
        "/api/v1/attendance/mark",
        headers=DEVICE,
        data={"external_id": "EMP-11"},
        files={"image": upload("bob-front.jpg")},
    )

    # Collapsed rather than recorded as an Out, which would silently end the person's shift.
    assert second.status_code == 200
    assert second.json()["decision"] == "Duplicate"
    assert second.json()["success"] is False


async def test_a_stranger_is_rejected_not_matched(client):
    await client.post(
        "/api/v1/registrations",
        headers=ADMIN,
        data={"external_id": "EMP-12", "full_name": "Carol Example"},
        files={
            "front": upload("carol-front.jpg"),
            "left": upload("carol-left.jpg"),
            "right": upload("carol-right.jpg"),
        },
    )

    response = await client.post(
        "/api/v1/attendance/mark",
        headers=DEVICE,
        data={"external_id": "EMP-12"},
        files={"image": upload("stranger-front.jpg")},
    )

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["decision"] == "Rejected"


async def test_the_same_picture_in_every_slot_is_refused_with_both_names(client):
    same = upload("alice-front.jpg")

    response = await client.post(
        "/api/v1/registrations",
        headers=ADMIN,
        data={"external_id": "EMP-13", "full_name": "Copied"},
        files={"front": same, "left": same, "right": same},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "duplicate_frame"
    assert "Front, Left and Right" in response.json()["detail"]


async def test_a_text_file_labelled_as_a_jpeg_is_refused(client):
    response = await client.post(
        "/api/v1/diagnostics/analyze",
        headers=ADMIN,
        files={"image": ("shell.jpg", b"<?php echo 1; ?>" + bytes(64), "image/jpeg")},
    )

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_format"


async def test_provider_diagnostics_report_the_live_engine(client):
    response = await client.get("/api/v1/diagnostics/provider", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["provider_id"] == "development:filename-stub:v1"
    assert body["required_poses"] == ["Front", "Left", "Right"]
    assert body["can_compare_templates_offline"] is True


async def test_pose_image_download_is_admin_only_and_uncacheable(client):
    registration = await client.post(
        "/api/v1/registrations",
        headers=ADMIN,
        data={"external_id": "EMP-14", "full_name": "Dave Example"},
        files={
            "front": upload("dave-front.jpg"),
            "left": upload("dave-left.jpg"),
            "right": upload("dave-right.jpg"),
        },
    )
    person_id = registration.json()["person"]["id"]

    denied = await client.get(
        f"/api/v1/persons/{person_id}/faces/Front/image", headers=DEVICE
    )
    assert denied.status_code == 403

    allowed = await client.get(
        f"/api/v1/persons/{person_id}/faces/Front/image", headers=ADMIN
    )

    assert allowed.status_code == 200
    assert allowed.headers["content-type"] == "image/jpeg"
    assert "no-store" in allowed.headers["cache-control"]


async def test_purging_biometrics_leaves_the_person_but_removes_the_faces(client):
    registration = await client.post(
        "/api/v1/registrations",
        headers=ADMIN,
        data={"external_id": "EMP-15", "full_name": "Erin Example"},
        files={
            "front": upload("erin-front.jpg"),
            "left": upload("erin-left.jpg"),
            "right": upload("erin-right.jpg"),
        },
    )
    person_id = registration.json()["person"]["id"]

    purged = await client.delete(f"/api/v1/persons/{person_id}/biometrics", headers=ADMIN)
    assert purged.status_code == 204

    person = await client.get(f"/api/v1/persons/{person_id}", headers=ADMIN)
    assert person.json()["enrollment_status"] == "NotEnrolled"

    enrollment = await client.get(f"/api/v1/persons/{person_id}/enrollment", headers=ADMIN)
    assert enrollment.json()["poses"] == []

    image = await client.get(f"/api/v1/persons/{person_id}/faces/Front/image", headers=ADMIN)
    assert image.status_code == 404


async def test_the_register_records_failures_too(client):
    await client.post(
        "/api/v1/registrations",
        headers=ADMIN,
        data={"external_id": "EMP-16", "full_name": "Frank Example"},
        files={
            "front": upload("frank-front.jpg"),
            "left": upload("frank-left.jpg"),
            "right": upload("frank-right.jpg"),
        },
    )

    await client.post(
        "/api/v1/attendance/mark",
        headers=DEVICE,
        data={"external_id": "EMP-16"},
        files={"image": upload("someone-else.jpg")},
    )

    register = await client.get("/api/v1/attendance", headers=ADMIN)

    assert register.status_code == 200
    body = register.json()
    assert body["total"] >= 1
    # The rejected attempt is the audit trail for "the machine did not recognise me".
    assert any(e["decision"] == "Rejected" for e in body["items"])


async def test_search_paginates(client):
    for i in range(3):
        await client.post(
            "/api/v1/persons",
            headers=ADMIN,
            json={"external_id": f"EMP-2{i}", "full_name": f"Person {i}"},
        )

    response = await client.get("/api/v1/persons?page=1&page_size=2", headers=ADMIN)

    body = response.json()
    assert len(body["items"]) == 2
    assert body["total"] == 3
    assert body["total_pages"] == 2


async def test_another_tenant_cannot_see_the_person(client):
    created = await client.post(
        "/api/v1/persons",
        headers=ADMIN,
        json={"external_id": "EMP-30", "full_name": "Scoped"},
    )
    person_id = created.json()["id"]

    response = await client.get(
        f"/api/v1/persons/{person_id}", headers={**ADMIN, "X-Tenant-Id": "other-site"}
    )

    # Reported as not-found rather than forbidden: confirming the id exists elsewhere would
    # already leak more than the caller is entitled to know.
    assert response.status_code == 404


async def test_deleting_a_pose_unenrols_the_person(client):
    registration = await client.post(
        "/api/v1/registrations",
        headers=ADMIN,
        data={"external_id": "EMP-31", "full_name": "Grace Example"},
        files={
            "front": upload("grace-front.jpg"),
            "left": upload("grace-left.jpg"),
            "right": upload("grace-right.jpg"),
        },
    )
    person_id = registration.json()["person"]["id"]

    deleted = await client.delete(
        f"/api/v1/persons/{person_id}/faces/Left", headers=ADMIN
    )
    assert deleted.status_code == 204

    enrollment = await client.get(f"/api/v1/persons/{person_id}/enrollment", headers=ADMIN)

    assert enrollment.json()["enrollment_status"] == "InProgress"
    assert enrollment.json()["missing_poses"] == ["Left"]


async def test_attendance_is_rate_limited_per_device(client):
    # Each call costs an inference, and a kiosk looping on frames is the normal failure mode.
    from app.api.rate_limit import _counters

    _counters["device:spammer"] = (__import__("time").monotonic(), 60)

    response = await client.post(
        "/api/v1/attendance/mark",
        headers={**DEVICE, "X-Device-Id": "spammer"},
        files={"image": upload("alice-front.jpg")},
    )

    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"
    assert "Retry-After" in response.headers
