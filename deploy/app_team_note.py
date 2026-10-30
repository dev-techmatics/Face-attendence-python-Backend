"""Writes a handover note for the app team with the current kiosk key filled in.

Saves the round trip where somebody pastes the wrong port, or a key with a line break in the
middle of it. Run it, open the file, send the contents.

    python deploy\\app_team_note.py

The note contains a live credential, so it is gitignored and should be sent the same way you
would send a password - not pasted into a public channel or an issue tracker.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env.production"
CERT = ROOT / "deploy" / "certs" / "server.crt"
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

4. The certificate - it does have to be installed
-------------------------------------------------
An earlier version of this note said the phones already trust it. That was wrong, and you
were right to correct it. The trust the phones have comes from the Caddy authority used by
the old service, and it covers port 443 only. Port 8443 is this service answering TLS
directly, with its own self-signed certificate, which nothing trusts until you install it.

    subject      {subject}
    issuer       same - it is self-signed
    valid        {not_before} to {not_after}
    covers       {sans}

    SHA256       {fingerprint}

That fingerprint is the certificate this service presents on 8443 right now; it is the one
to check against whatever you have bundled. The file itself (server.crt) comes with this
note. It is the public half only - the private key never leaves the server, and nobody
should ever send you one.

On iPhone: install the .crt, then switch it on under
Settings > General > About > Certificate Trust Settings. iOS installs a certificate and
trusts it in two separate steps, and the second one is easy to miss - if TLS still fails
after installing, that is almost always why.

Longer term we can move this service behind Caddy on 443, where the certificate is already
trusted and nothing needs installing on any device. That is a bigger change, because every
person has to be registered again, so it is a decision rather than a quick fix.

5. If something is refused
--------------------------
    401 "This device isn't authorised"   wrong key, or you are on port 443. Every key
                                        issued before {rotated} was
                                        replaced and no longer works - check yours
                                        character for character against section 2
                                        before looking any further.
    403                                 the request came from outside the office network;
                                        the service answers the office LAN only
    400 "Some details were missing"      the key was accepted and a field is missing

To separate "bad key" from everything else in one request, send the key with no body at all.
400 means the key was accepted. 401 means it was not:

    curl -k -X POST {base_url}/v1/attendance/punch -H "Authorization: Bearer KEY" -H "Content-Type: application/json" -d "{{}}"

(put the key from section 2 where KEY is)

Anything else, send us the exact URL you called and the reply, and we will trace it here.
"""


def spaced_fingerprint(digest: bytes) -> str:
    """``01:EF:FC:...`` - the form every tool prints, so it can be compared by eye."""
    return ":".join(f"{byte:02X}" for byte in digest)


def certificate_facts() -> dict[str, str]:
    """Read them from the certificate itself, so the note cannot drift from what is served."""
    certificate = x509.load_pem_x509_certificate(CERT.read_bytes())

    try:
        names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        covers = ", ".join(str(name.value) for name in names)
    except x509.ExtensionNotFound:
        covers = "no subject alternative names - this certificate will fail hostname checks"

    # The *_utc properties are the current spelling; the older ones are deprecated.
    starts = getattr(certificate, "not_valid_before_utc", None) or certificate.not_valid_before
    ends = getattr(certificate, "not_valid_after_utc", None) or certificate.not_valid_after

    return {
        "subject": certificate.subject.rfc4514_string(),
        "not_before": starts.strftime("%d %B %Y"),
        "not_after": ends.strftime("%d %B %Y"),
        "sans": covers,
        "fingerprint": spaced_fingerprint(certificate.fingerprint(hashes.SHA256())),
    }


def main() -> int:
    if not ENV.is_file():
        print(f"  {ENV} is missing - cannot read the current key.")
        return 1

    if not CERT.is_file():
        print(f"  {CERT} is missing - cannot describe the certificate.")
        return 1

    text = ENV.read_text(encoding="utf-8")
    found = re.search(r'FA_API_KEYS__DEVICE=\["([^"]+)"', text)

    if found is None:
        print("  No device key is configured in .env.production.")
        return 1

    facts = certificate_facts()

    NOTE.write_text(
        TEMPLATE.format(
            written=date.today().strftime("%d %B %Y"),
            base_url=BASE_URL,
            device_key=found.group(1),
            # When the key in use was issued, taken from the file new_keys.py writes, so the
            # note tells the reader which older keys to stop trying.
            rotated=datetime.fromtimestamp(ENV.stat().st_mtime).strftime("%d %B %Y, %H:%M"),
            **facts,
        ),
        encoding="utf-8",
    )

    print("")
    print(f"  Written to {NOTE}")
    print(f"  Certificate fingerprint: {facts['fingerprint']}")
    print("  It contains the live device key: send it like a password, not in a public channel.")
    print(f"  Send {CERT} alongside it - that is the public certificate, never server.key.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
