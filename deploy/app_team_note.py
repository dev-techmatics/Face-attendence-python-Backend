"""Writes a handover note for the app team with the current kiosk key filled in.

Saves the round trip where somebody pastes the wrong port, or a key with a line break in the
middle of it. Run it, open the file, send the contents.

    python deploy\\app_team_note.py

The note contains a live credential, so it is gitignored and should be sent the same way you
would send a password - not pasted into a public channel or an issue tracker.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env.production"
NOTE = ROOT / "deploy" / "APP-TEAM-NOTE.txt"

#: Where the service actually answers. The port is the whole point of the note: the older
#: .NET service is still on 443 and answers the same contract with the same wording, so an
#: app pointed at the wrong one gets a plausible reply and no clue that it is the wrong server.
BASE_URL = "https://172.24.61.79:8443"

TEMPLATE = """Face Attendance backend - settings for the kiosk app
Written {written}

1. The address, which is the thing that was wrong
-------------------------------------------------
    {base_url}

Note the port: 8443, not 443.

https://172.24.61.79 with no port is the OLD service, which is still running on this
machine and still has its own separate key. That is why the new key came back 401 there
and the old one looked like it was working: two different servers, same address, same
wording in every reply. Nothing was wrong with the key.

To check which one you have reached, call:

    GET {base_url}/v1/health

The new service names itself:

    {{"success":true,"message":"ok","employees":2,"server":"face-attendance-python"}}

The old one has no "server" field. If you do not see it, you are on 443.

2. The device key
-----------------
    {device_key}

Send it as:

    Authorization: Bearer {device_key}

It is all letters, digits and dashes - no characters that need escaping and none that can
be confused for each other. It replaces every key issued before today; the previous ones
no longer work.

3. What has not changed
-----------------------
The endpoints, the fields and the wording of every reply are the same as before:

    GET  /v1/health
    POST /v1/faces/enroll      employee_id, full_name, face_front (+ optional face_left, face_right)
    POST /v1/faces/identify    face
    POST /v1/attendance/punch

So the app needs three things only: the address above, the key above, and useMockAPI=false.
No code changes.

4. The certificate
------------------
The kiosk phones already trust it. A laptop browser opening the address will warn, because
the certificate is signed by our own authority rather than a public one - that is expected
and does not affect the app.

5. If something is refused
--------------------------
    401 "This device isn't authorised"   wrong key, or you are on port 443
    403                                 the request came from outside the office network;
                                        the service answers the office LAN only
    400 "Some details were missing"      the key was accepted and a field is missing

Anything else, send us the exact URL you called and the reply, and we will trace it here.
"""


def main() -> int:
    if not ENV.is_file():
        print(f"  {ENV} is missing - cannot read the current key.")
        return 1

    text = ENV.read_text(encoding="utf-8")
    found = re.search(r'FA_API_KEYS__DEVICE=\["([^"]+)"', text)

    if found is None:
        print("  No device key is configured in .env.production.")
        return 1

    NOTE.write_text(
        TEMPLATE.format(
            written=date.today().strftime("%d %B %Y"),
            base_url=BASE_URL,
            device_key=found.group(1),
        ),
        encoding="utf-8",
    )

    print(f"\n  Written to {NOTE}")
    print("  It contains the live device key: send it like a password, not in a public channel.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
