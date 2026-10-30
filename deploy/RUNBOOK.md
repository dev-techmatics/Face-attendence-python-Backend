# Face Attendance backend — deployment runbook

This machine (`QA-PLAYWRIGHT`, office LAN `172.24.61.0/24`) runs the Python Face Attendance
backend. Everything here is free: SQL Server **Express**, a self-signed certificate, and a
scheduled task. No cloud service, no licence to buy.

| | |
| --- | --- |
| Base URL | `https://172.24.61.79:8443` |
| Application | `E:\Fa-py` |
| Database | `.\SQLEXPRESS`, database `FaceAttendance`, Windows integrated auth |
| Image store | `E:\Fa-py\data\faces` |
| Logs | `E:\Fa-py\data\logs\server.log` |
| Config | `E:\Fa-py\.env.production` (holds API keys — not in git) |
| API keys | `E:\Fa-py\deploy\API-KEYS.txt` (not in git) |
| Recognition | ONNX, SCRFD `det_500m` + ArcFace `w600k_mbf`, 512-d |

## Why these choices

**Port 8443, not 443.** 443 on this machine already belongs to the Caddy container of the
*other* face-attendance deployment (`D:\face-attendance` + `D:\compreface`, which the iOS app
points at), and 80 is held by Windows' own HTTP.sys. Taking 443 would have meant stopping a
running service, so this one sits beside it. If you want this backend to *replace* CompreFace,
that is a deliberate switch-over, not something to do by accident — see "Moving to 443" below.

**`.\SQLEXPRESS`, not the default instance.** The default instance (`QA-PLAYWRIGHT`) is
**Developer Edition**: free, but licensed for development and test only, so using it to run a
real attendance system would breach its licence. It is also Acumatica's. `.\SQLEXPRESS` is
Express Edition — free for production, capped at 10 GB per database, which is far more than
this needs: a template is 2 KB, so 50 staff × 3 poses is 300 KB, and the photographs live on
disk rather than in the database.

**A self-signed certificate.** No public CA will issue for a private IP and there is no
budget for a domain. The warning a browser shows is the untrusted issuer, not a broken
connection — the traffic is TLS 1.3 either way, and that matters because the API key travels
in a header where anyone on the LAN could otherwise read it. Install
`deploy\certs\server.crt` into *Trusted Root Certification Authorities* on the kiosks to
silence the warning.

**An application-level network allowlist.** The standing requirement is that this is reachable
only from the company network. Windows Firewall is **disabled** on the office adapter here, so
the firewall cannot enforce it; `FA_TRUSTED_NETWORKS` does. Verified: a request arriving on the
Tailscale or VirtualBox interface gets `403 untrusted_network`, while the office LAN reaches
authentication. `/health` stays open from anywhere so monitoring does not need an allowlist
entry — it reveals nothing but liveness.

## Day-to-day

Start, stop, restart:

```bash
E:\Fa-py\deploy\start-server.cmd
```

```bash
E:\Fa-py\deploy\stop-server.cmd
```

Is it up?

```bash
curl -k https://172.24.61.79:8443/health
```

What engine and thresholds are live:

```bash
curl -k -H "X-Api-Key: <admin key>" https://172.24.61.79:8443/api/v1/diagnostics/provider
```

Logs:

```bash
powershell -Command "Get-Content E:\Fa-py\data\logs\server.log -Tail 40 -Wait"
```

## Still to do — needs Administrator

Autostart is **not** installed. Run this **as Administrator**, once:

```bash
E:\Fa-py\deploy\install-autostart.cmd
```

Elevated it registers an at-boot task running as SYSTEM, so the backend comes up after an
unattended reboot. Run without elevation and you get a per-user at-logon task instead — which
means the kiosks stay down until somebody signs in to this PC, and auto sign-in is off here
(deliberately). Until this is done, **the service does not survive a reboot.**

Two more things worth doing while elevated:

- **Reserve the IP.** `172.24.61.79` is DHCP. If the lease moves, every kiosk loses the
  backend and the certificate's IP entry stops matching. Set a reservation on the router.
- **Consider re-enabling Windows Firewall** on the office adapter and allowing only 8443 from
  `172.24.61.0/24`. The app-level allowlist already covers this, but defence in depth is
  cheap and a firewall rule protects ports the application does not know about.

## Updating the code

```bash
cd /d E:\Fa-py && git pull && .venv\Scripts\python.exe -m pytest -q
```

```bash
E:\Fa-py\deploy\migrate.cmd
```

```bash
E:\Fa-py\deploy\stop-server.cmd && E:\Fa-py\deploy\start-server.cmd
```

Migrations are deliberately **not** run at startup: two instances starting at once would race
each other through the same schema change. `migrate.cmd` is the only thing that changes the
schema.

## Rotating an API key

Both lists accept several keys, so this is a zero-downtime operation:

1. Add the new key alongside the old one in `.env.production`.
2. Restart. Move the kiosks across.
3. Remove the old key. Restart again.

Device keys can only mark attendance. Admin keys can read stored face photographs — treat them
accordingly and never put one on a kiosk.

## Backups

Nothing is backed up yet. What matters, in order:

1. `FaceAttendance` database — the attendance register is the record of who was present, and
   it is not re-derivable.
2. `E:\Fa-py\data\faces\enrollments` — losing these means re-enrolling everybody in person.
3. `.env.production` and `deploy\certs` — losing the keys locks out every kiosk.

Probe frames under `data\faces\probes` are expendable; they self-delete after 30 days.

A SQL Express instance has no Agent to schedule a backup job, so this needs a scheduled task
running `sqlcmd ... BACKUP DATABASE`. Not set up — raise it when you want it.

## Moving to 443 (only if replacing CompreFace)

443 is held by the Docker/Caddy stack of the older deployment. To hand it over:

1. Stop that stack: `cd D:\face-attendance && docker compose down`.
2. Change `--port 8443` to `--port 443` in `start-server.cmd`.
3. Repoint the iOS app's base URL and re-enrol: CompreFace templates are **not**
   interchangeable with ArcFace embeddings, so every person must be captured again.

Step 3 is the expensive one. Do not start it on a Friday.

## Data protection

Face templates and the stored captures are biometric personal data under India's DPDP Act and
GDPR. Everything stays on this machine: the ONNX provider sends nothing to any third party,
which is the main reason it is the configured engine rather than AWS Rekognition. The volume
holding `E:\Fa-py\data` is not encrypted — worth fixing with BitLocker if the machine could
ever leave the office.

## The kiosk app's own API (`/v1`)

The iOS app was built against the earlier CompreFace service and speaks a different dialect:
`Bearer` tokens, four addresses under `/v1`, and a flat `{"success": …, "message": …}` reply
with HTTP 200 even for a refusal. `app/api/routers/compat_v1.py` translates, so the app does
not have to be rewritten:

| App calls | Translated to |
| --- | --- |
| `GET /v1/health` | a headcount of enrolled people |
| `POST /v1/faces/enroll` | create person + capture each pose |
| `POST /v1/faces/identify` | a 1:N match that records nothing |
| `POST /v1/attendance/punch` | mark attendance, 1:1 against the claimed id |

Two settings exist for the app's sake, both in `.env.production`:

- `FA_FACE__REQUIRED_POSES=["Front"]` — the app offers the two side photos but does not
  guarantee them. Sides are still stored and still used for matching when they arrive. Raise
  this to all three once the app insists on three captures; three angles is the more accurate
  enrolment, it just cannot be a precondition today.
- `FA_FACE__INFER_DIRECTION=false` — the app's screen has already told the user whether this
  is a check-in or a check-out, so its value is honoured instead of being inferred.

### Switching the kiosks over

Not done, deliberately. `deploy\Caddyfile.cutover` is the prepared change — one
`reverse_proxy` line. Caddy keeps serving 443 with the certificate the phones already trust,
so **no phone needs touching and the app's base URL does not change**.

The blocker is not technical: **everyone must be re-registered first**, because CompreFace
templates and ArcFace embeddings cannot be converted. Plan that, then apply the cutover.

Note that `--proxy-headers` is **not** enabled on the server today, and must be switched on in
`start-server.cmd` at the same time as the cutover — not before. Until Caddy is in front,
requests arrive straight from LAN clients, and trusting `X-Forwarded-For` from them would let
any device on the office network claim any address and walk through the allowlist.

## Seeing who is registered

There is no web page for this yet. Three ways to look, easiest first.

**1. The report script.** Double-click `deploy\who-is-registered.cmd`, or:

```bash
E:\Fa-py\.venv\Scripts\python.exe E:\Fa-py\deploy\report.py --today
```

Other options: `--days 7`, `--person TS4012`. It is read-only and cannot change anything.

**2. The API**, if you want JSON for another system:

```bash
curl -k -H "X-Api-Key: <admin key>" https://172.24.61.79:8443/api/v1/persons
```

`/api/v1/attendance` is the register, and takes `fromUtc`, `toUtc`, `personId` and `decision`.

**3. The database**, with SQL Server Management Studio or Azure Data Studio: connect to
`.\SQLEXPRESS` with Windows authentication, database `FaceAttendance`. Three tables — `People`,
`FaceTemplates`, `AttendanceEvents`.

The registration photographs are on disk at `E:\Fa-py\data\faces\enrollments\default\<person
id>\`. The attendance photos are under `probes\` and delete themselves after 30 days.

Note that failed attempts are kept deliberately. They are the evidence when somebody says the
machine did not recognise them.

## The dashboard

Two audiences, two doors, one page each:

| Who | Where | Signs in with |
| --- | --- | --- |
| A member of staff | `https://172.24.61.79:8443/me` | a photo of themselves |
| HR / admin | `https://172.24.61.79:8443/admin` | the dashboard password |

**Staff sign in with their face, not a password.** For forty or fifty people, passwords mean
forty or fifty handovers and a reset request every time somebody forgets. The recognition
engine is already trusted to decide who is standing at the kiosk, so it decides who is looking
at the page. The photo is taken with an ordinary camera input, so it needs no JavaScript and no
camera API. Looking at your hours records nothing — it is not a punch.

What that costs: a photograph of a colleague would let someone read that colleague's hours.
The page is read-only, shows one person's own record, and only works on the office network, so
the worst case is one colleague seeing another's working hours. If that is not acceptable, a
PIN alongside the photo is a small change — ask.

Staff see: in or out now, hours this week and this month, late arrivals, days not present, and
the last fortnight day by day. Hours come from matching in/out pairs, so a break punched out
for is not counted, and a punch with no matching half is flagged rather than guessed at.

HR sees: Today (who is in, refreshing itself), Register (every attempt, with an Excel
download), People (who has not finished registering), Problems (failures grouped with a likely
cause, and attempts by hour), Monthly (hours per person, with an Excel download).

Both are off unless `FA_ADMIN_UI_PASSWORD` is set, and the service refuses to start if
`FA_SESSION_SECRET` is too short to be safe. The password is in `deploy\API-KEYS.txt`.

### The certificate warning

A browser opening `https://172.24.61.79:8443` shows a warning, because the certificate is
self-signed. The kiosk iPhones do not see this — they trust Caddy's root, which was installed
when they were set up — but a laptop or an unprepared phone will.

Two ways out, in order of preference:

1. **Put the dashboard behind Caddy** at the same time as the kiosk cutover
   (`deploy\Caddyfile.cutover`). Caddy already serves a certificate the kiosk phones trust.
2. **Install Caddy's root certificate** on whichever devices will open the dashboard. It is
   `D:\face-attendance\face-attendance-root.crt`, and the steps are in
   `D:\face-attendance\SETUP-WINDOWS-SERVER.md` (install the profile, then turn on the
   full-trust switch — both parts are needed).

Clicking through the warning works but teaches people to ignore certificate warnings, which is
a habit worth not creating.

For one PC in the meantime, `deploy\trust-certificate.cmd` adds the certificate to the trusted
list for whoever runs it. No administrator rights, nobody else on the machine is affected, and
`deploy\untrust-certificate.cmd` reverses it. Chrome has to be closed completely and reopened
afterwards, not just the tab.

It is safe here for a specific reason worth understanding rather than taking on trust: the
certificate is self-signed and is **not** a certificate authority, so it can only ever vouch
for this one server. Installing a real CA certificate would mean something very different - it
could vouch for any site at all.

Verify before trusting. The thumbprint should be:

```
86C8A13ECCEBDBE19416DB9B873D86A9735B6FD3
```

This fixes one PC only. Every phone or laptop that opens the dashboard needs the same thing,
which is the argument for routing the dashboard through Caddy instead.

### The dashboard password

Short on purpose: it is typed every day. That is only safe because repeated wrong guesses are
locked out — five tries from one address, then a fifteen-minute pause. Everybody in the office
is on the network that can reach this page, so "only the office can see it" is not on its own
a defence against somebody working through a word list.

To change it:

```
E:\Fa-py\deploy\change-dashboard-password.cmd
```

It asks twice, writes the new value, and updates `deploy\API-KEYS.txt` so the file people
actually read does not go stale. Add `--suggest` to be offered a short memorable one. Restart
the service afterwards; anyone already signed in stays signed in.

A lockout clears on its own after fifteen minutes, and a restart clears all of them — useful
if somebody locks themselves out just before they need the page.

Staff never need any of this. They sign in at `/me` with a photo.
