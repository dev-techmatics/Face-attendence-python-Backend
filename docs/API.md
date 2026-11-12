# Face Attendance — kiosk API

What the iOS and Android apps talk to, and what changed on **10 November 2026**.

This document contains **no key**. The device key is sent separately, in `APP-TEAM-NOTE.txt`,
because it is a live credential.

---

## 1. What changed — the short version

Everything below already worked and is unchanged except these three things.

| | Change | Must the app change? |
| --- | --- | --- |
| `POST /v1/faces/verify` | **New endpoint.** "Is this face already registered?" | Optional, but it fixes the duplicate-registration complaint |
| `POST /v1/attendance/punch` | Reply now includes **`type`** — the direction actually recorded | **Yes. Not optional.** |
| `POST /v1/attendance/punch` | Three new refusals: too-soon check-out, abandoned shift, and a check-in right after a check-out | Yes — handle the new messages |

**The one you cannot skip** is `type`. The server can now record a *check-in* when the app
asked for a *check-out* (section 5.2). If the app keeps setting its own state from which
button the user pressed, it will show "checked out" over a record that says checked in, and
the attendance register will disagree with the phone. Read `type` and set state from that.

Nothing was removed. No field was renamed. No existing message changed its wording.

---

## 2. Connecting

| | |
| --- | --- |
| Base URL | `https://172.24.61.79:8443` |
| Auth | `Authorization: Bearer <device key>` on every call except `/v1/health` |
| Content type | `multipart/form-data` for every POST |
| Tenant | `X-Tenant-Id` header, optional — omit it and you get the default tenant |

### The port is part of the address

`172.24.61.79` **without a port is a different server.** The old .NET service still answers
on 443, speaks this same contract with the same wording, and accepts a *different* key — so
pointing at 443 with a correct key returns `401 This device isn't authorised` and looks
exactly like a bad key. This cost two afternoons before it was spotted.

One request tells the two apart:

```
GET /v1/health   →   {"success": true, "message": "ok", "employees": 5,
                      "server": "face-attendance-python"}
```

Only this service returns `"server"`. If that field is missing, you are on the wrong port.

### The certificate

Port 8443 presents a **self-signed** certificate, `CN=face-attendance.local`. The phones'
existing trust comes from the Caddy authority on the old service and covers **443 only**, so
8443 is not trusted until you install `server.crt` (sent with the handover note).

```
SHA256  01:EF:FC:91:5B:87:E4:4B:DC:DA:37:93:F8:BE:BA:6E:
        87:F0:7B:AC:6F:74:F7:52:A8:B5:5F:50:62:AA:6B:EA
```

On iOS, installing a certificate and **trusting** it are two separate steps. The second is
under *Settings → General → About → Certificate Trust Settings*, and it is the one people
miss — if TLS still fails after installing, that is almost always why.

### Reachability

The service answers the office LAN only (`172.24.61.0/24`). From anywhere else every request
returns **403**, whatever key is used. There is no rate limit on these endpoints.

---

## 3. The reply shape

Every `/v1` reply is flat JSON with at least `success` and `message`:

```json
{ "success": true,  "message": "Checked in." }
{ "success": false, "message": "No face found. Please look at the camera." }
```

**A refusal is HTTP 200.** The request was understood and answered; it just did not succeed.
Branch on `success`, not on the status code. Non-200 is reserved for the cases in section 7.

**`message` is written to be shown to the person standing at the camera**, verbatim. It says
what to do next rather than naming an error code. Please display it rather than mapping it to
your own strings — when a message changes here it is because the instruction changed.

---

## 4. Endpoints

### 4.1 `GET /v1/health`

No key required. Call it on launch to confirm the app is pointed at the right server.

```json
{ "success": true, "message": "ok", "employees": 5, "server": "face-attendance-python" }
```

| Field | Meaning |
| --- | --- |
| `employees` | how many people are fully registered |
| `server` | always `face-attendance-python`. Absent ⇒ wrong port (section 2) |

If the database is unreachable: `{"success": false, "message": "face service unreachable",
"employees": 0, "server": "face-attendance-python"}` — still HTTP 200.

---

### 4.2 `POST /v1/faces/verify` — **NEW**

**Is this face already registered?** Records nothing, changes nothing.

Call this on the register screen **before the user fills the form in**. It gives the same
verdict registration itself will, so nobody types their details and is then refused at the
end — which is the complaint that prompted it.

| Field | Required | Notes |
| --- | --- | --- |
| `face` | yes | one photo, see section 8 |
| `employee_id` | no | present ⇒ 1:1 check against that person; absent ⇒ search everybody |

Without `employee_id` it searches **everybody, including people whose records were removed**,
at the duplicate-registration threshold. With `employee_id` it is a 1:1 check against that one
person at the lower verification threshold — use that to confirm an identity, not to discover
one.

#### The reply has two separate flags. Please do not collapse them.

| Field | Meaning |
| --- | --- |
| `success` | the check **ran** |
| `registered` | the **answer** — absent entirely when `success` is false |

Not registered:

```json
{ "success": true, "registered": false,
  "message": "This face is not registered yet." }
```

Registered, and able to clock in:

```json
{ "success": true, "registered": true, "status": "active",
  "can_mark_attendance": true,
  "employee_id": "TS4012", "name": "Alice Example",
  "message": "Already registered as Alice Example. Just put your face to the camera to clock in." }
```

Registered, but the record was removed:

```json
{ "success": true, "registered": true, "status": "removed",
  "can_mark_attendance": false,
  "message": "This face belongs to a record that was removed. Ask your admin to put that record back instead of registering again." }
```

The check could not run:

```json
{ "success": false, "message": "No face found. Please look at the camera." }
```

| Field | Values |
| --- | --- |
| `status` | `active`, `suspended`, `removed` |
| `can_mark_attendance` | `true` only when active **and** fully registered |
| `employee_id`, `name` | present only when `can_mark_attendance` is true, or when you sent `employee_id` |

#### Two things to get right

**On `success: false`, do not offer to register.** There is no `registered` field, because the
answer is not known. A camera or model failure read as "this face is new" is exactly how a
second record for the same person gets created — the thing this endpoint exists to prevent.
Show the message and let them retry.

**When `status` is `removed` or `suspended`, do not send them to the register screen.** An
admin has to restore the record. Registering again gives one person two records with their
hours split between them. `employee_id` and `name` are deliberately withheld in that case —
the person at the camera is being told "no", and the name is not theirs to be shown.

---

### 4.3 `POST /v1/faces/enroll`

Unchanged. Creates the person and stores their face.

| Field | Required | Notes |
| --- | --- | --- |
| `employee_id` | yes | letters, digits, `.`, `-`, `_`; uppercased by the server |
| `full_name` | yes | 2–80 characters |
| `face_front` | yes | **decides whether registration happens at all** |
| `face_left` | no | skipped with a warning if unusable |
| `face_right` | no | skipped with a warning if unusable |

Success: `{"success": true, "message": "Face registered."}`

The side photos are a bonus — a strong turn can hide the face, so an unusable side is skipped
rather than failing the registration. **One exception:** if a side photo matches a *different*
person, the whole registration is refused and rolled back. That is a real identity conflict,
not a bad photo.

If the front photo cannot be used, nothing is created — the employee ID is not left occupied
by an unusable record.

Common refusals (all HTTP 200):

| Message | Cause |
| --- | --- |
| `ID TS4012 is already registered. Ask the admin to reset it.` | the ID is taken |
| `Employee ID can use letters, numbers, dot, dash or underscore only.` | bad ID format |
| `Please enter the full name.` | name shorter than 2 or longer than 80 |
| `This face is already registered under another ID.` | the face belongs to someone else |
| `This face belongs to a record that was removed. Ask your admin to put that record back instead of registering again.` | the face belongs to an archived person |
| `No face found in the front photo. Please try again.` | no face detected |
| `More than one face in the photo. Only the person registering should be in frame.` | two or more faces |
| `Those photos are the same picture. Please take a separate photo for each angle.` | the same bytes sent twice |
| `Please look straight at the camera for the front photo.` | pose does not match |

Calling `/v1/faces/verify` first turns most of these into a message shown *before* the form.

---

### 4.4 `POST /v1/faces/identify`

Unchanged. **Who is this?** Records nothing.

| Field | Required |
| --- | --- |
| `face` | yes |

```json
{ "success": true, "message": "Matched.",
  "employee_id": "TS4012", "name": "Alice Example" }
```

No match, or a match too close to call between two people:

```json
{ "success": false, "message": "We couldn't match your face. New here? Tap Register." }
```

Only **active, registered** people are searched — unlike `/v1/faces/verify`, which also finds
removed ones. That difference is deliberate: this answers "who may clock in", and a removed
person may not.

---

### 4.5 `POST /v1/attendance/punch` — **CHANGED**

Records a check-in or check-out.

| Field | Required | Notes |
| --- | --- | --- |
| `employee_id` | yes | uppercased by the server |
| `type` | yes | `check_in` or `check_out` — exactly these |
| `captured_at` | yes | ISO 8601. No timezone ⇒ treated as UTC |
| `face` | yes | one photo |
| `latitude` | no | decimal degrees |
| `longitude` | no | decimal degrees |

#### Success — read `type` from the reply

```json
{ "success": true, "type": "check_in", "message": "Checked in." }
```

**`type` is the direction the server actually stored**, and is the field to set the app's
state from. It is normally the same as what you sent. When it is not, `message` explains why:

```json
{ "success": true, "type": "check_in",
  "message": "Checked in. Your last check-in was 17 hours ago and was never closed. This is a new check-in." }
```

#### Refusals (HTTP 200, `success: false`)

| Message | What happened |
| --- | --- |
| `Checked in 35 minutes ago, and a check-out needs at least 2 hours. You are still checked in.` | **New.** Too soon to check out — section 5.1. The numbers vary |
| `You already checked out at 17:02. Nothing more to do.` | **New.** They finished and their face passed the camera again — section 5.3. The time varies |
| `Your attendance was already recorded a moment ago.` | a second punch within 2 minutes |
| `We couldn't match your face. New here? Tap Register.` | the face did not match this employee ID |
| `This employee isn't registered. Please register again.` | no such employee ID, or never finished registering |
| `No face found. Please look at the camera.` | no face in the photo |
| `The photo was too dark or blurry. Please try again.` | unusable photo |
| `Unknown attendance type.` | `type` was not `check_in` or `check_out` |
| `Invalid time. Please try again.` | `captured_at` could not be parsed |
| `Face service is not responding. Please try again.` | recognition engine failure |

**Every attempt is recorded, including the failures.** They are the evidence when somebody
says the machine did not recognise them, so a refusal is not a wasted call.

#### About `captured_at`

It is stored, but it is **not** the time the attendance is recorded at — the server stamps its
own clock, and that is what every screen and every report shows. `captured_at` exists so that
if punches are ever queued offline the real time is recoverable. Send the honest capture time;
do not try to backdate a punch with it.

---

## 5. The rules the server now enforces

All three numbers are **configuration here, not code**. If they do not match how people actually
work, say so and they change with a service restart — no app release.

### 5.1 A check-out needs at least 2 hours

A `check_out` sooner than two hours after the check-in it would close is refused. **The person
stays checked in**, so the shift can still be closed properly later. Nothing is lost.

The message carries the real numbers, because "too soon" on its own just invites an immediate
retry. Show it as sent.

Note the layering with the existing double-tap guard: a repeat punch within **2 minutes**
answers `Your attendance was already recorded a moment ago` instead. So the two-hour rule is
only ever seen between 2 minutes and 2 hours.

### 5.2 A check-in left open 16 hours is abandoned

Nobody worked sixteen hours and forgot to go home — they forgot to check out. So the stale
check-in is set aside and **the next punch starts a new check-in, even if the app asked for a
check-out**.

This is the only case where the server overrules the app's choice, and it is why `type` exists
in the reply.

The abandoned check-in is left unpaired rather than closed with an invented check-out. A
fabricated Out would put sixteen hours nobody worked into the month's figures; the admin
register flags it as "Punch missing" for a person to judge.

### 5.3 A check-in within 15 minutes of a check-out is ignored

Somebody who has finished and walks back past the kiosk is not starting a second shift, so
their face landing on the screen again records nothing.

The reply is a refusal, but please show it as the reassurance it is — it names the time they
left and says there is nothing more to do. They have done nothing wrong, and a message that
reads like an error makes people try again:

```json
{ "success": false,
  "message": "You already checked out at 17:02. Nothing more to do." }
```

This runs **before** everything else, and only in that one direction: a check-in after a
check-out. A check-out after a check-in is untouched and still answers under section 5.1.

The attempt is recorded, as it always is, so nothing disappears from the register.

### 5.4 What this means for the app's state machine

```
verify / identify  →  punch(check_in)   →  reply type=check_in   →  show "checked in"
                      punch(check_out)  →  reply type=check_out  →  show "checked out"
                      punch(check_out)  →  reply type=check_in   →  show "checked in"   ← 5.2
                      punch(check_out)  →  success=false         →  still checked in    ← 5.1
                      punch(check_in)   →  success=false         →  still checked out   ← 5.3
```

The rule is the same in all five lines: **on success, trust `type`; on failure, change
nothing.** The last one is the newest and the easiest to get wrong: a refused check-in
must leave them checked out, not flip the app into a checked-in state it then cannot
close.

---

## 6. Suggested screen flows

### Registering somebody

1. Take the front photo.
2. `POST /v1/faces/verify` with just `face`.
3. `success: false` → show `message`, offer retry. **Do not continue to the form.**
4. `registered: true`, `status: active` → show `message`; send them to the clock-in screen.
5. `registered: true`, `status: removed` or `suspended` → show `message`; stop. An admin must
   act.
6. `registered: false` → show the form, then `POST /v1/faces/enroll`.

### Clocking in or out

1. Take the photo.
2. `POST /v1/faces/identify` to show who the camera sees (optional — `punch` identifies too).
3. `POST /v1/attendance/punch` with the employee ID and the button the user pressed.
4. On `success: true`, set the app's state from **`type`** and show `message`.
5. On `success: false`, show `message` and leave the app's state as it was.

---

## 7. Status codes

| Code | When | Body |
| --- | --- | --- |
| 200 | the request was answered — **including every refusal** | `{success, message, …}` |
| 400 | a required field was missing | `Some details were missing. Please try again.` |
| 401 | missing or unrecognised key, **or you are on port 443** | `This device isn't authorised. Contact your admin.` |
| 403 | the request came from outside the office network | — |
| 404 | no such address | `Not Found` |
| 405 | wrong HTTP method | `Method Not Allowed` |
| 413 | photo larger than 8 MB | `The photo was too large. Please try again.` |
| 415 | the upload was not an image | `That file isn't a photo. Please try again.` |
| 503 | the recognition engine is down | `Face service is not responding. Please try again.` |

Every one of these carries the flat `{success, message}` body, including 404 and 405.

---

## 8. Photo requirements

| | |
| --- | --- |
| Formats | JPEG, PNG, WebP |
| Maximum size | 8 MB |
| Minimum image side | 240 px |
| Minimum face side | 80 px — i.e. not too far from the camera |
| Faces in frame | exactly one |

Face the camera squarely for a front photo. For registration the side photos may be turned,
but a turn strong enough to hide the face is rejected for that pose only.

---

## 9. Checking your integration

Confirm the key and the port in one request. **400 means the key was accepted** and a field is
missing; **401 means it was not**:

```
curl -k -X POST https://172.24.61.79:8443/v1/attendance/punch -H "Authorization: Bearer KEY"
```

Confirm which server answered:

```
curl -k https://172.24.61.79:8443/v1/health
```

Check a face without recording anything:

```
curl -k -X POST https://172.24.61.79:8443/v1/faces/verify -H "Authorization: Bearer KEY" -F "face=@photo.jpg"
```

Anything unexpected: send the exact URL called and the exact reply, and it can be traced in
the server log from this end.
