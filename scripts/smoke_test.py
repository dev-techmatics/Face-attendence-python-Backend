"""End-to-end smoke test against a running server.

Registers a person with three frames, marks attendance, and prints every decision, so you can
see what a real deployment is actually doing rather than inferring it from logs.

With no image arguments it generates flat-colour frames. Those carry no face, so they only get
past the **development** provider; against `onnx` or `aws` they are correctly refused with
`no_face_detected`, which is itself a useful thing to confirm. Pass real photographs to
exercise the real pipeline::

    python scripts/smoke_test.py --front alice-front.jpg --left alice-left.jpg --right alice-right.jpg

Usage::

    python scripts/smoke_test.py --base-url http://localhost:8000 --admin-key dev-admin-key
"""

from __future__ import annotations

import argparse
import io
import sys
import zlib
from pathlib import Path

import httpx
from PIL import Image


def generated(name: str) -> bytes:
    """A real JPEG whose bytes differ per name, so the duplicate-frame guard stays happy."""
    shade = 32 + (zlib.crc32(name.encode()) % 192)
    buffer = io.BytesIO()
    Image.new("RGB", (640, 640), (shade, shade, shade)).save(buffer, format="JPEG")
    return buffer.getvalue()


def upload(name: str, path: str | None) -> tuple[str, bytes, str]:
    if path:
        file = Path(path)
        return (file.name, file.read_bytes(), "image/jpeg")

    return (name, generated(name), "image/jpeg")


def show(label: str, response: httpx.Response, limit: int = 400) -> None:
    body = response.text.replace("\n", " ")[:limit]
    print(f"{label:<16} -> {response.status_code}  {body}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--admin-key", default="dev-admin-key")
    parser.add_argument("--device-key", default="dev-device-key")
    parser.add_argument("--external-id", default="SMOKE-1")
    parser.add_argument(
        "--identity",
        default="smoke",
        help="File-name stem used for generated frames; the development provider treats it "
        "as the person's identity.",
    )
    parser.add_argument("--front", help="Path to a real front-facing photograph.")
    parser.add_argument("--left", help="Path to a real left-turned photograph.")
    parser.add_argument("--right", help="Path to a real right-turned photograph.")
    parser.add_argument(
        "--probe",
        help="Path to the frame to punch in with. Defaults to the front photograph.",
    )
    arguments = parser.parse_args()

    admin = {"X-Api-Key": arguments.admin_key}
    device = {"X-Api-Key": arguments.device_key}

    front = upload(f"{arguments.identity}-front.jpg", arguments.front)
    left = upload(f"{arguments.identity}-left.jpg", arguments.left)
    right = upload(f"{arguments.identity}-right.jpg", arguments.right)
    probe = upload(f"{arguments.identity}-front.jpg", arguments.probe or arguments.front)

    with httpx.Client(base_url=arguments.base_url, timeout=120.0) as client:
        show("health", client.get("/health"))
        show("provider", client.get("/api/v1/diagnostics/provider", headers=admin))
        show(
            "analyze",
            client.post("/api/v1/diagnostics/analyze", headers=admin, files={"image": front}),
        )

        registration = client.post(
            "/api/v1/registrations",
            headers=admin,
            data={"external_id": arguments.external_id, "full_name": "Smoke Test"},
            files={"front": front, "left": left, "right": right},
        )
        show("registration", registration)

        if registration.status_code != 201:
            print("\nRegistration failed, so there is nothing to verify against.")
            return 1

        person_id = registration.json()["person"]["id"]

        show("enrollment", client.get(f"/api/v1/persons/{person_id}/enrollment", headers=admin))

        punch = client.post(
            "/api/v1/attendance/mark",
            headers=device,
            data={"person_id": str(person_id), "device_id": "smoke-kiosk"},
            files={"image": probe},
        )
        show("attendance 1:1", punch)

        show("register", client.get("/api/v1/attendance", headers=admin))

        decision = punch.json().get("decision") if punch.status_code == 200 else None
        print(f"\nFirst punch decision: {decision}")

        return 0 if decision == "Accepted" else 1


if __name__ == "__main__":
    sys.exit(main())
