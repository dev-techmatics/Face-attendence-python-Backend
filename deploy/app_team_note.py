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

4. What IS new, and one of them will change a reply you already handle
---------------------------------------------------------------------
a) POST /v1/faces/verify    face  (+ optional employee_id)

   "Is this face already registered?" Records nothing. Worth calling on the register screen
   BEFORE the user fills the form in: it gives the same verdict registration itself will, so
   nobody types everything and then gets refused at the end.

   Without employee_id it searches everybody, including people whose records were removed.
   With employee_id it is a 1:1 check against that one person instead.

   The reply has two separate flags, and please do not collapse them:

       success     the check ran
       registered  the answer

       {{"success": true, "registered": false, "message": "This face is not registered yet."}}
       {{"success": true, "registered": true, "status": "active",
        "can_mark_attendance": true, "employee_id": "...", "name": "...",
        "message": "Already registered as ... Just put your face to the camera to clock in."}}

   On success:false there is NO "registered" field - the check failed and the answer is not
   known. Please show the message and let them retry rather than offering to register,
   otherwise a camera or model hiccup creates exactly the duplicate record this prevents.

   When registered is true, read "status": "active" means just clock in, while "removed" or
   "suspended" means an admin has to restore the record - that person must not be sent round
   the register screen again, or they end up with two records and their hours split between
   them. For those, employee_id and name are deliberately absent.

b) Two shift rules on /v1/attendance/punch, which can refuse or redirect a punch

   * A check_out less than 2 hours after the check-in it would close is refused:

         {{"success": false, "message": "Checked in 35 minutes ago, and a check-out needs at
          least 2 hours. You are still checked in."}}

     They stay checked in, so the shift can still be closed later. Nothing is lost.

   * A check-in left open more than 14 hours is treated as abandoned. The next punch starts
     a NEW check-in even if the app asked for a check_out - somebody who never checked out
     yesterday is arriving, not leaving.

   * A check_in while they are ALREADY checked in is recorded as a check_out, once the shift
     has run past two hours. Somebody standing at the camera who is already in is leaving;
     recording a second check-in instead gave one person two in a row with nothing between
     them, and two check-ins can never be paired, so that day could never be paid.

   * A check_in within 15 minutes of a check_out is ignored - somebody who has finished and
     walks back past the kiosk is not starting a second shift:

         {{"success": false, "message": "You already checked out at 17:02. Nothing more to
          do."}}

     Please show that as the reassurance it is rather than as an error. They have done
     nothing wrong, and a message that reads like a failure makes people try again.

   Because of that second rule, a successful punch now tells you what was actually recorded:

       {{"success": true, "type": "check_in", "message": "Checked in. Your last check-in was
        17 hours ago and was never closed. This is a new check-in."}}

   "type" is "check_in" or "check_out" and is the direction the server stored. It is normally
   the same as what you sent. Please set the app's own in/out state from it rather than from
   what the button did, or the app will show "checked out" over a record that says checked
   in, and the register will disagree with the phone.

   Both limits are configuration, not code - say the word if 2 hours or 16 hours are wrong
   for how people actually work and we will change the numbers.

5. The certificate - it does have to be installed
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

6. If something is refused
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
