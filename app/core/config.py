"""Configuration, with startup validation.

Settings come from three places, later ones winning:

1. ``appsettings.json`` / ``appsettings.{Environment}.json`` next to the project root. The
   same PascalCase, colon-nested layout the original .NET service used, so an existing
   deployment's config file keeps working unchanged.
2. A ``.env`` file.
3. Environment variables, prefixed ``FA_`` and nested with ``__``, e.g.
   ``FA_FACE__VERIFICATION_THRESHOLD=0.45``.

A threshold typo such as 42 instead of 0.42 would silently reject every employee, so the
process refuses to start rather than failing quietly all day.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from app.core.enums import FacePose

#: Repository root: the directory holding ``app/``.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_POSES: tuple[FacePose, ...] = (FacePose.Front, FacePose.Left, FacePose.Right)
_DEFAULT_CONTENT_TYPES: tuple[str, ...] = ("image/jpeg", "image/jpg", "image/png", "image/webp")


class OnnxProviderSettings(BaseSettings):
    #: SCRFD face-detection model (boxes + 5 landmarks).
    detector_model_path: str = "models/scrfd_detector.onnx"

    #: ArcFace recognition model (112x112 input, 512-d output).
    recognition_model_path: str = "models/arcface_recognizer.onnx"

    #: Recorded inside provider_id. Bump it whenever you replace a model file.
    model_version: str = "v1"

    detector_confidence_threshold: float = 0.5
    detector_nms_threshold: float = 0.4

    #: Square size the detector input is letterboxed to.
    detector_input_size: int = 640

    #: Threads per inference session. 0 lets onnxruntime decide.
    intra_op_threads: int = 0

    #: Concurrent inference permits. Caps CPU burn during the shift-change rush.
    max_concurrency: int = 4

    #: Set when the recognition model expects BGR channel order instead of RGB. Some
    #: ONNX-zoo ArcFace conversions do. A wrong value does not crash, it just lowers every
    #: score, so verify against a known pair after swapping models.
    recognition_input_bgr: bool = False

    #: Set when frames arrive mirrored, as a front-facing phone camera preview usually does.
    #: This flips the sign of the yaw estimate; get it wrong and every left capture is
    #: rejected for being a right one.
    mirrored_input: bool = False

    model_config = SettingsConfigDict(protected_namespaces=())


class AwsProviderSettings(BaseSettings):
    region: str = "ap-south-1"

    #: Rekognition collection id. Created on startup when missing.
    collection_id: str = "face-attendance"

    #: The 0-100 similarity floor handed to Rekognition itself.
    search_threshold_percent: float = 80.0

    max_faces: int = 5


class S3StorageSettings(BaseSettings):
    bucket_name: str = ""
    region: str = "ap-south-1"
    key_prefix: str = "faces/"

    #: Biometric images should not sit in a bucket unencrypted.
    use_server_side_encryption: bool = True
    kms_key_id: str | None = None


class StorageSettings(BaseSettings):
    #: "local" or "s3".
    provider: str = "local"

    #: Root directory for the local store. Relative paths resolve from the project root.
    local_root_path: str = "storage/faces"

    s3: S3StorageSettings = Field(default_factory=S3StorageSettings)


class FaceRecognitionSettings(BaseSettings):
    #: "onnx" (self-hosted), "aws" (Rekognition), or "development" (deterministic fake).
    provider: str = "onnx"

    #: Poses the capture flow must collect before a person counts as enrolled.
    required_poses: list[FacePose] = Field(default_factory=lambda: list(_DEFAULT_POSES))

    # ---- Decision thresholds ---------------------------------------------------------
    # Cosine similarity on L2-normalised ArcFace embeddings. Calibrate against your own
    # population before go-live. Rekognition reports 0-100 and its provider rescales to
    # 0-1, so these numbers stay comparable across engines.

    #: 1:1 check, when the device already knows who is claiming to be there.
    verification_threshold: float = 0.42

    #: 1:N search. Stricter on purpose: more candidates, more chances to be wrong.
    identification_threshold: float = 0.52

    #: Minimum lead the winner must have over the runner-up person in a 1:N search.
    #: Guards against look-alikes and siblings.
    identification_margin: float = 0.06

    #: Score above which a new registration is treated as an already-enrolled person.
    duplicate_enrollment_threshold: float = 0.60

    #: Floor for "the three poses are the same person". Front-to-profile similarity is
    #: genuinely low even for one face, so this is a sanity check, not a tight match.
    min_enrollment_pair_similarity: float = 0.20

    reject_duplicate_enrollments: bool = True
    validate_enrollment_consistency: bool = True

    #: Reject the same byte-identical frame submitted for two different poses.
    reject_duplicate_frames: bool = True

    #: Require the detected yaw to agree with the requested pose.
    enforce_pose_check: bool = True

    #: Yaw magnitude, in degrees, that a left/right capture must reach.
    min_profile_yaw_degrees: float = 12.0

    #: Yaw magnitude a frontal capture must stay under.
    max_frontal_yaw_degrees: float = 18.0

    # ---- Input validation ------------------------------------------------------------

    min_detection_score: float = 0.60
    min_quality_score: float = 0.35

    #: Shortest acceptable image side, in pixels.
    min_image_dimension: int = 240

    #: Smallest acceptable detected face side, in pixels. Too small means too far away.
    min_face_dimension: int = 80

    max_image_bytes: int = 8 * 1024 * 1024

    allowed_content_types: list[str] = Field(default_factory=lambda: list(_DEFAULT_CONTENT_TYPES))

    # ---- Attendance rules ------------------------------------------------------------

    #: Any repeat punch inside this window is collapsed into the first one, whichever
    #: direction it would have been. Set to 0 to allow back-to-back punches.
    duplicate_punch_window_minutes: int = 2

    #: Infer In/Out from the previous event instead of trusting the client.
    infer_direction: bool = True

    top_n: int = 5

    # ---- Retention -------------------------------------------------------------------

    #: Keep the attendance probe frame. Needed for disputes; costs storage.
    store_probe_images: bool = True

    #: Days to keep probe frames. 0 disables the cleanup job.
    probe_image_retention_days: int = 30

    onnx: OnnxProviderSettings = Field(default_factory=OnnxProviderSettings)
    aws: AwsProviderSettings = Field(default_factory=AwsProviderSettings)

    model_config = SettingsConfigDict(protected_namespaces=())

    @field_validator("required_poses", mode="after")
    @classmethod
    def _poses_default_and_dedupe(cls, value: list[FacePose]) -> list[FacePose]:
        if not value:
            return list(_DEFAULT_POSES)
        return list(dict.fromkeys(value))

    @field_validator("allowed_content_types", mode="after")
    @classmethod
    def _content_types_default_and_dedupe(cls, value: list[str]) -> list[str]:
        cleaned = [t.strip().lower() for t in value if t and t.strip()]
        return list(dict.fromkeys(cleaned)) if cleaned else list(_DEFAULT_CONTENT_TYPES)

    @model_validator(mode="after")
    def _validate(self) -> "FaceRecognitionSettings":
        failures: list[str] = []

        for name in (
            "verification_threshold",
            "identification_threshold",
            "duplicate_enrollment_threshold",
            "min_enrollment_pair_similarity",
            "min_quality_score",
            "min_detection_score",
        ):
            value = getattr(self, name)
            if value < 0.0 or value > 1.0:
                failures.append(f"{name} is {value}; it must sit between 0 and 1.")

        if self.identification_threshold < self.verification_threshold:
            failures.append(
                "identification_threshold is lower than verification_threshold. A 1:N search "
                "compares against everybody, so it needs the stricter bar, not the looser one."
            )

        if self.min_image_dimension < 64:
            failures.append("min_image_dimension below 64px cannot hold a recognisable face.")

        if self.min_face_dimension > self.min_image_dimension:
            failures.append(
                "min_face_dimension exceeds min_image_dimension, so no image could ever pass."
            )

        if self.max_image_bytes < 32 * 1024:
            failures.append("max_image_bytes below 32KB would reject ordinary camera output.")

        if not self.required_poses:
            failures.append("required_poses is empty, so nobody could ever become enrolled.")

        if self.provider not in ("onnx", "aws", "development"):
            failures.append(
                f"Unknown provider '{self.provider}'. Expected onnx, aws or development."
            )

        if failures:
            raise ValueError("; ".join(failures))

        return self


class ApiKeySettings(BaseSettings):
    """Keys that may do everything, and keys issued to kiosks.

    A device key is the one most likely to leak, so it must not be able to enrol a new face
    or read a stored image.
    """

    admin: list[str] = Field(default_factory=list)
    device: list[str] = Field(default_factory=list)


class DatabaseSettings(BaseSettings):
    #: SQLAlchemy URL. Defaults to a file-backed SQLite database so a fresh clone runs with
    #: no server to install; point it at SQL Server for a real deployment, e.g.
    #: ``mssql+aioodbc://@localhost\\SQLEXPRESS/FaceAttendance?driver=ODBC+Driver+18+for+SQL+Server&trusted_connection=yes&TrustServerCertificate=yes``
    url: str = "sqlite+aiosqlite:///./face_attendance.db"

    #: Applied at startup in development only. In production this belongs in the deployment
    #: pipeline: two instances starting at once would otherwise race through the same change.
    migrate_on_startup: bool = False

    echo: bool = False

    command_timeout_seconds: int = 30


class Settings(BaseSettings):
    environment: str = "Production"

    default_tenant_id: str = "default"

    #: No origins configured means no browser is expected to call this directly. Leaving it
    #: open would let any website post to the attendance endpoint with a stolen key.
    cors_allowed_origins: list[str] = Field(default_factory=list)

    #: CIDR ranges allowed to reach the API at all. Empty disables the check, which is right
    #: for a test run and wrong for a deployment: biometric data must not leave the office, so
    #: the production environment file sets this explicitly.
    trusted_networks: list[str] = Field(default_factory=list)

    #: Per-device fixed window on the attendance endpoint.
    attendance_rate_limit_per_minute: int = 60

    #: Kestrel defaulted to 30MB, which is far more than three JPEG frames need.
    max_multipart_bytes: int = 32 * 1024 * 1024

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    api_keys: ApiKeySettings = Field(default_factory=ApiKeySettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    face: FaceRecognitionSettings = Field(default_factory=FaceRecognitionSettings)

    model_config = SettingsConfigDict(
        env_prefix="FA_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order the sources: explicit arguments, then the environment, then the JSON files.

        The ``appsettings*.json`` source goes last so that an environment variable overrides
        the file, which is the precedence operators expect and the one the README documents.
        """
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            AppSettingsSource(settings_cls),
        )

    @property
    def is_development(self) -> bool:
        return self.environment.strip().lower() == "development"


# ---- appsettings.json compatibility ------------------------------------------------------

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

#: .NET section names mapped onto this model's field names where the two differ.
_SECTION_ALIASES = {
    "face_recognition": "face",
    "connection_strings": "_connection_strings",
    "cors": "_cors",
    "logging": None,
    "allowed_hosts": None,
}


def _to_snake(name: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", name).replace("-", "_").replace(" ", "_").lower()


def _normalise(node: Any) -> Any:
    """Recursively rewrite PascalCase JSON keys to the snake_case field names."""
    if isinstance(node, dict):
        return {_to_snake(key): _normalise(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_normalise(item) for item in node]
    return node


def _read_appsettings(environment: str) -> dict[str, Any]:
    """Load ``appsettings.json`` and its environment overlay, if either exists."""
    merged: dict[str, Any] = {}

    for name in ("appsettings.json", f"appsettings.{environment}.json"):
        path = PROJECT_ROOT / name
        if not path.is_file():
            continue

        with path.open("r", encoding="utf-8-sig") as handle:
            overlay = _normalise(json.load(handle))

        _deep_merge(merged, overlay)

    return merged


def _deep_merge(target: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _flatten_dotnet_shape(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate the .NET section layout into this model's keyword arguments."""
    kwargs: dict[str, Any] = {}

    for key, value in raw.items():
        mapped = _SECTION_ALIASES.get(key, key)

        if mapped is None:
            continue

        if mapped == "_connection_strings" and isinstance(value, dict):
            # ConnectionStrings:Default was a raw ADO.NET string; it is not a SQLAlchemy URL,
            # so it is only honoured when it already looks like one.
            default = value.get("default")
            if isinstance(default, str) and "://" in default:
                kwargs.setdefault("database", {})["url"] = default
            continue

        if mapped == "_cors" and isinstance(value, dict):
            kwargs["cors_allowed_origins"] = value.get("allowed_origins", [])
            continue

        kwargs[mapped] = value

    return kwargs


class AppSettingsSource(PydanticBaseSettingsSource):
    """Reads ``appsettings.json`` and its environment overlay as a settings source."""

    def get_field_value(self, field, field_name):  # pragma: no cover - not used
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        values = _flatten_dotnet_shape(_read_appsettings(current_environment()))
        values.setdefault("environment", current_environment())
        return values


def current_environment() -> str:
    """The configured environment name, from ``FA_ENVIRONMENT`` or the ASP.NET variable."""
    return (
        os.environ.get("FA_ENVIRONMENT")
        or os.environ.get("ASPNETCORE_ENVIRONMENT")
        or "Production"
    )


def env_file() -> str:
    """Which dotenv file to read.

    ``FA_ENV_FILE`` lets a deployment point at ``.env.production`` without exporting every
    variable by hand, and keeps the service and Alembic reading the same file.
    """
    return os.environ.get("FA_ENV_FILE", ".env")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build the settings once per process.

    Validation runs here, so a bad threshold stops the deploy rather than surfacing as every
    employee being rejected at the morning rush.
    """
    return Settings(_env_file=env_file())


def reset_settings_cache() -> None:
    """Drop the memoised settings. Tests use this after changing the environment."""
    get_settings.cache_clear()


def resolve_path(value: str) -> Path:
    """Resolve a configured path, treating relative values as project-root relative."""
    path = Path(value)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
