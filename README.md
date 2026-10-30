# Face Attendance API (Python)

A Python port of the .NET `Face-attendence-Backend` service. Same domain model, same HTTP
surface, same decision rules — FastAPI and SQLAlchemy in place of ASP.NET Core and EF Core,
onnxruntime + Pillow + NumPy in place of Microsoft.ML.OnnxRuntime + SkiaSharp.

Registration collects a **front, left and right** capture per person. Attendance matches a
live frame against them, either **1:1** when the device already knows who is claiming to be
there, or **1:N** across the tenant.

## Quick start

```bash
python -m venv .venv && .venv\Scripts\activate
```

```bash
pip install -r requirements.txt
```

```bash
copy .env.example .env
```

The shipped `.env.example` selects the **development** recognition provider, which needs no
model file, and a SQLite database, which needs no server. That is enough to exercise the whole
API:

```bash
uvicorn app.main:app --reload
```

Interactive docs are then at <http://localhost:8000/docs> (development only).

```bash
python scripts/smoke_test.py
```

> The development provider **identifies files, not faces**: `alice-front.jpg`,
> `alice-left.jpg` and `alice-right.jpg` enrol as one person and verify against each other,
> while `bob-front.jpg` will not match. Selecting it outside development is refused at
> startup. Treat any attendance row it produces as test data, never as evidence that somebody
> was present.

## Running it for real

### 1. Download the models

```bash
python scripts/download_models.py
```

This takes an InsightFace `buffalo` bundle and extracts the two models this service needs into
`models/`, under stable names so the configuration does not change with the pack:

| Pack | Download | Detector | Recogniser | |
| --- | --- | --- | --- | --- |
| `buffalo_l` (default) | ~288 MB | `det_10g` | `w600k_r50` | more accurate, slower per frame |
| `buffalo_s` | ~128 MB | `det_500m` | `w600k_mbf` | fast, good enough for a kiosk |

```bash
python scripts/download_models.py --pack buffalo_s
```

A detector **with keypoints is mandatory** — both of these have them. Without landmarks a face
cannot be aligned, and an unaligned crop costs far more accuracy than any threshold tuning
recovers; the detector refuses to start if the model emits no landmark head.

Then set `FA_FACE__PROVIDER=onnx`. Swapping either model file invalidates every stored
template, so bump `FaceRecognition:Onnx:ModelVersion` when you do: the provider id changes and
existing enrolments are marked `Stale` instead of being compared against vectors from a
different model. `models/MODELS.txt` records which pack is in place.

### 2. Point it at a real database

SQLite is fine for one machine. For anything shared, use SQL Server:

```bash
pip install -r requirements-mssql.txt
```

```bash
alembic upgrade head
```

with

```
FA_DATABASE__URL=mssql+aioodbc://@localhost\SQLEXPRESS/FaceAttendance?driver=ODBC+Driver+18+for+SQL+Server&trusted_connection=yes&TrustServerCertificate=yes
```

`alembic upgrade head` is how production gets its schema. `FA_DATABASE__MIGRATE_ON_STARTUP`
exists for development only — two instances starting at once would otherwise race each other
through the same schema change.

### 3. Issue real API keys

```
FA_API_KEYS__ADMIN=["<long-random-string>"]
FA_API_KEYS__DEVICE=["<another-long-random-string>"]
```

Admin keys may do everything, including reading stored face photographs. Device keys may only
mark attendance — they are the keys most likely to be extracted from a kiosk, so they must not
be able to enrol a face or read an image. Keys are compared in fixed time.

## Configuration

Three sources, later ones winning:

1. `appsettings.json` and `appsettings.{Environment}.json` — the same PascalCase, nested layout
   the .NET service used, so an existing deployment's config file keeps working unchanged.
2. `.env`
3. Environment variables, prefixed `FA_` and nested with `__`.

So `FaceRecognition:VerificationThreshold` in the JSON file is
`FA_FACE__VERIFICATION_THRESHOLD` in the environment.

Settings are validated at import time. A threshold typo such as `42` instead of `0.42` would
silently reject every employee, so the process refuses to start rather than failing quietly
all day.

### The numbers that matter

| Setting | Default | What it governs |
| --- | --- | --- |
| `verification_threshold` | 0.42 | 1:1 check, when the device knows who this is |
| `identification_threshold` | 0.52 | 1:N search — stricter on purpose: more candidates, more chances to be wrong |
| `identification_margin` | 0.06 | Minimum lead over the runner-up person in a 1:N search; guards against look-alikes |
| `duplicate_enrollment_threshold` | 0.60 | Above this, a new registration is treated as an already-enrolled person |
| `min_enrollment_pair_similarity` | 0.20 | Floor for "the three poses are the same person" |
| `duplicate_punch_window_minutes` | 2 | Repeat punches inside this window collapse into the first |
| `probe_image_retention_days` | 30 | Attendance frames are deleted after this; 0 disables the sweep |

Calibrate the thresholds against your own population before go-live. Rekognition reports
0–100 and its provider rescales to 0–1, so these numbers stay comparable across engines.

## Recognition providers

| Provider | Template stored | Offline comparison | Notes |
| --- | --- | --- | --- |
| `onnx` | 512-d float32 embedding | yes | Self-hosted. No cloud account, no per-call cost, no face image leaving the building. |
| `aws` | Rekognition face id | **no** | `pip install -r requirements-aws.txt`. Duplicate-identity and pose-consistency checks become no-ops, and every frame is sent to AWS. |
| `development` | deterministic fake | yes | Matches on file name. Refused outside development. |

Templates are **not portable between providers or model versions**. The provider id is stored
on every row precisely so that swapping the engine marks old enrolments `Stale` instead of
silently comparing incompatible vectors and rejecting everybody.

## Endpoints

All of them need an `X-Api-Key` header. `X-Tenant-Id` selects the organisation; single-site
deployments can ignore it and everything lands in the default tenant.

| Method | Path | Key | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health` | none | Liveness plus a database check |
| `POST` | `/api/v1/persons` | admin | Create a person |
| `GET` | `/api/v1/persons` | admin | Search, paginated |
| `GET` | `/api/v1/persons/{id}` | admin | One person |
| `GET` | `/api/v1/persons/by-external-id/{externalId}` | admin | Look up by your own id |
| `GET` | `/api/v1/persons/{id}/enrollment` | admin | Which poses exist, which are missing, how similar they are |
| `DELETE` | `/api/v1/persons/{id}/biometrics` | admin | Erase every template and image — what an erasure request needs |
| `POST` | `/api/v1/registrations` | admin | Create the person and store all three poses in one call |
| `POST` | `/api/v1/persons/{id}/faces` | admin | Store one pose; re-capture supersedes |
| `POST` | `/api/v1/persons/{id}/faces/batch` | admin | Store all three for an existing person |
| `DELETE` | `/api/v1/persons/{id}/faces/{pose}` | admin | Retire one pose |
| `GET` | `/api/v1/persons/{id}/faces/{pose}/image` | admin | Download a stored capture (never cached) |
| `POST` | `/api/v1/attendance/mark` | device | Verify a frame and record the punch |
| `POST` | `/api/v1/attendance/preview` | admin | Score a frame without recording anything |
| `GET` | `/api/v1/attendance` | admin | The register, including rejected attempts |
| `GET` | `/api/v1/diagnostics/provider` | admin | Which engine is live and what it decides by |
| `POST` | `/api/v1/diagnostics/analyze` | admin | What the detector makes of one frame |

### Registering someone

```bash
curl -X POST http://localhost:8000/api/v1/registrations -H "X-Api-Key: dev-admin-key" -F external_id=EMP-1 -F full_name="Alice Example" -F front=@alice-front.jpg -F left=@alice-left.jpg -F right=@alice-right.jpg
```

### Marking attendance

```bash
curl -X POST http://localhost:8000/api/v1/attendance/mark -H "X-Api-Key: dev-device-key" -H "X-Device-Id: kiosk-1" -F person_id=10001 -F image=@live-frame.jpg
```

Send `person_id` or `external_id` whenever the device can obtain it — a badge tap, a PIN, a
name picker. That makes it a 1:1 verification, which is faster and markedly more accurate,
because its error rate does not grow with headcount.

**A non-match is still a `200`** with `success: false` and a decision code
(`Rejected`, `Duplicate`, `NotEnrolled`, `NoFaceDetected`, `LowQuality`, `ProviderError`): the
request was processed correctly, it simply did not match. `4xx` is reserved for malformed
requests, and carries a stable machine-readable `code` plus `hints` so a capture screen can
branch without parsing prose.

## What the rules actually enforce

- **Three poses, and they must really be three poses.** The yaw of each capture has to agree
  with the pose that was asked for, or three frontal snapshots would sail through as a
  complete three-angle enrolment and quietly discard the angular coverage.
- **No two slots holding the same picture.** Copying one file into all three is the most
  common registration mistake, so the batch is refused up front, naming the slots that clash.
- **No second identity for one face.** Without that check one person can hold two ids and
  cover for an absent colleague.
- **Incoherent sets do not complete.** Three different faces under one id is a complete set
  but an incoherent one, so it stays `InProgress` with a warning.
- **Double taps collapse before a direction is chosen.** Checking afterwards would compare
  against the inferred direction — the opposite of the punch just made — so a second tap two
  seconds later would sail through as an `Out` and silently end the person's shift.
- **Every attempt is persisted, failures included.** Those failures are the audit trail you
  need when somebody says the machine did not recognise them.
- **Probe frames expire.** Useful for a few weeks to settle a disputed punch, a liability
  after that. Only the image is deleted; the attendance row stays, so the register remains
  complete.

## Tests

```bash
pytest
```

The enrolment rules run against a real in-memory SQLite database and a scripted provider, so
the repository queries are covered rather than mocked away. `tests/test_api.py` drives the
whole app over HTTP with the development provider.

`tests/test_onnx_provider.py` exercises the real pipeline — detection, landmark alignment,
embedding, and the match decision — and is the only thing that catches a wrong channel order,
a mis-decoded anchor grid or an inverted warp matrix, none of which crash; they just quietly
destroy accuracy. It needs the models plus two photographs in `tests/fixtures/`:

| File | What it needs to be |
| --- | --- |
| `portrait.jpg` | one clear face, reasonably frontal |
| `group.jpg` | more than one face, none of them the person in `portrait.jpg` |

Both are gitignored, since face photographs are personal data and not ours to redistribute.
The tests skip cleanly when either the models or the fixtures are missing, so a fresh clone
still runs green.

## Project layout

```
app/
  core/          settings, enums, errors, value objects, vector maths, API-key comparison
  domain/        SQLAlchemy models
  db/            engine and session factory
  repositories/  the queries
  services/      enrolment and attendance rules, upload validation
  providers/     onnx (SCRFD + ArcFace), aws (Rekognition), development (fake)
  storage/       local filesystem and S3 image stores, object-key layout
  api/           routers, schemas, dependencies, problem responses, rate limit
  jobs/          probe-image retention sweep
alembic/         migrations
scripts/         model download, smoke test
```

## Notes on the port

- **Image pipeline.** SkiaSharp became Pillow + NumPy. The letterbox, the landmark-driven
  similarity warp, the Laplacian-variance blur measure and the luma weights are the same
  arithmetic; Pillow's affine transform takes the inverse matrix, which is computed in
  `app/providers/onnx/image_ops.py`.
- **Concurrency.** `SemaphoreSlim` became an `asyncio.Semaphore`, with inference pushed to a
  worker thread. onnxruntime releases the GIL, so the cap still does its job of stopping a
  shift-change rush from spawning unbounded parallel inferences.
- **Optimistic concurrency.** SQL Server's `rowversion` is not portable, so `RowVersion` is a
  plain counter that SQLAlchemy bumps and checks on every `UPDATE`.
- **Timestamps.** `DateTimeOffset` carries its offset through any round-trip; most databases
  do not, and SQLite hands back a naive value. Every timestamp column therefore goes through
  `app/db/types.py`, which stores UTC and re-attaches it on read — otherwise a client parsing
  `2026-10-30T05:39:23` would read it as local time and silently shift every attendance record
  by the viewer's offset.
- **Rate limiting** is in-process, so behind a load balancer each instance enforces its own
  window. Move the counters to Redis if you need a cluster-wide limit.
- **The `_src/` directory** holds the original C# service, kept for reference and excluded
  from git.

## Operational cautions

Face templates and stored captures are biometric personal data under GDPR, India's DPDP Act
and most US state biometric statutes. The S3 store encrypts at rest by default; the local
store does not, so put the volume on an encrypted disk. Serve the API over TLS only — an API
key in a header is worth exactly as much as the transport carrying it.
