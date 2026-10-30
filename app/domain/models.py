"""The three persisted entities.

Column lengths, indexes and delete behaviour mirror the original EF Core configuration, so
the same schema serves both implementations.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    AttendanceDecision,
    AttendanceDirection,
    EnrollmentStatus,
    FacePose,
    PersonStatus,
)
from app.db.base import Base
from app.db.types import UtcDateTime

#: A 512-d float32 embedding is 2048 bytes. 8000 is the largest a SQL Server ``varbinary``
#: can be; past that the column becomes ``varbinary(max)``, which pushes the payload off-row
#: and makes the candidate scan noticeably slower. That is what the original asked for in a
#: comment but did not get: it specified 8192, so EF Core emitted ``varbinary(max)`` anyway.
#: 8000 still leaves room for a 2000-dimension model and for provider-managed references.
MAX_TEMPLATE_BYTES = 8000


class Person(Base):
    """A person who can be registered and later recognised.

    :attr:`external_id` is the id your front end already knows (employee code, student roll
    number, ...) and is what the capture screens send for all three pose uploads.
    """

    __tablename__ = "People"
    __table_args__ = (
        # The business key the capture screens send. Unique per tenant so a retried
        # registration is a conflict rather than a second identity.
        UniqueConstraint("TenantId", "ExternalId", name="UX_People_Tenant_ExternalId"),
        # Covers the 1:N candidate fetch, which filters on exactly these columns.
        Index("IX_People_Tenant_Enrollment_Status", "TenantId", "EnrollmentStatus", "Status"),
    )

    id: Mapped[int] = mapped_column("Id", Integer, primary_key=True, autoincrement=True)

    #: Tenant / organisation scope. Keeps one deployment usable by several sites.
    tenant_id: Mapped[str] = mapped_column("TenantId", String(64), nullable=False, default="default")

    #: Stable business identifier, unique per tenant.
    external_id: Mapped[str] = mapped_column("ExternalId", String(64), nullable=False)

    full_name: Mapped[str] = mapped_column("FullName", String(200), nullable=False)
    email: Mapped[str | None] = mapped_column("Email", String(256))
    department: Mapped[str | None] = mapped_column("Department", String(128))

    status: Mapped[PersonStatus] = mapped_column(
        "Status", Integer, nullable=False, default=PersonStatus.Active
    )
    enrollment_status: Mapped[EnrollmentStatus] = mapped_column(
        "EnrollmentStatus", Integer, nullable=False, default=EnrollmentStatus.NotEnrolled
    )

    created_at_utc: Mapped[datetime] = mapped_column(
        "CreatedAtUtc", UtcDateTime, nullable=False
    )
    updated_at_utc: Mapped[datetime] = mapped_column(
        "UpdatedAtUtc", UtcDateTime, nullable=False
    )
    enrolled_at_utc: Mapped[datetime | None] = mapped_column(
        "EnrolledAtUtc", UtcDateTime
    )

    #: Optimistic concurrency guard. SQL Server's own ``rowversion`` is not portable, so this
    #: is a plain counter that SQLAlchemy bumps and checks on every UPDATE instead.
    row_version: Mapped[int] = mapped_column("RowVersion", Integer, nullable=False, default=1)

    # lazy="raise" throughout: under asyncio a lazy load from an un-awaited attribute is a
    # runtime error anyway, so it is better to fail loudly at the access than to emit IO from
    # somewhere unexpected. Callers that want the templates ask for them with selectinload.
    face_templates: Mapped[list["FaceTemplate"]] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )

    # Attendance history outlives the person record on purpose: deleting an employee must not
    # silently rewrite last quarter of attendance. passive_deletes="all" leaves the decision
    # to the database, whose foreign key is ON DELETE NO ACTION - the delete is rejected.
    attendance_events: Mapped[list["AttendanceEvent"]] = relationship(
        back_populates="person",
        passive_deletes="all",
        lazy="raise",
    )

    __mapper_args__ = {"version_id_col": row_version}

    @property
    def can_mark_attendance(self) -> bool:
        return (
            self.status == PersonStatus.Active
            and self.enrollment_status == EnrollmentStatus.Enrolled
        )


class FaceTemplate(Base):
    """One registered face capture for one person at one pose.

    :attr:`template_data` is the biometric payload, and its meaning depends on
    :attr:`provider_id`: for the local ONNX provider it is a 512-dimension float32 embedding;
    for AWS Rekognition it is the provider-side face identifier.

    Templates are therefore **not portable between providers or model versions**. The
    provider id is stored on every row precisely so that swapping the engine marks the old
    enrolments ``Stale`` instead of silently comparing incompatible vectors and rejecting
    everybody.
    """

    __tablename__ = "FaceTemplates"
    __table_args__ = (
        # The query behind every verification: active templates for a person.
        Index("IX_FaceTemplates_Person_Active_Pose", "PersonId", "IsActive", "Pose"),
        # Swapping the recognition model has to find its orphans quickly.
        Index("IX_FaceTemplates_Provider_Active", "ProviderId", "IsActive"),
        # Backs the "same frame re-sent for another pose" check.
        Index("IX_FaceTemplates_Person_ImageHash", "PersonId", "ImageSha256"),
    )

    id: Mapped[int] = mapped_column("Id", Integer, primary_key=True, autoincrement=True)

    person_id: Mapped[int] = mapped_column(
        "PersonId", Integer, ForeignKey("People.Id", ondelete="CASCADE"), nullable=False
    )
    person: Mapped[Person | None] = relationship(back_populates="face_templates", lazy="raise")

    pose: Mapped[FacePose] = mapped_column("Pose", Integer, nullable=False)

    #: Recognition engine + model version that produced :attr:`template_data`.
    provider_id: Mapped[str] = mapped_column("ProviderId", String(128), nullable=False)

    #: Embedding length, or 0 for provider-managed templates.
    dimension: Mapped[int] = mapped_column("Dimension", Integer, nullable=False, default=0)

    #: Embedding bytes (float32, little endian, L2-normalised) or provider reference.
    template_data: Mapped[bytes] = mapped_column(
        "TemplateData", LargeBinary(MAX_TEMPLATE_BYTES), nullable=False
    )

    quality_score: Mapped[float] = mapped_column("QualityScore", Float, nullable=False, default=0.0)
    detection_score: Mapped[float] = mapped_column(
        "DetectionScore", Float, nullable=False, default=0.0
    )

    #: Key of the stored source image in the configured image store.
    image_object_key: Mapped[str] = mapped_column("ImageObjectKey", String(512), nullable=False)
    image_content_type: Mapped[str] = mapped_column("ImageContentType", String(64), nullable=False)
    image_size_bytes: Mapped[int] = mapped_column(
        "ImageSizeBytes", BigInteger, nullable=False, default=0
    )

    #: Hex SHA-256 of the uploaded bytes. Used to spot the same frame re-sent for 3 poses.
    image_sha256: Mapped[str] = mapped_column("ImageSha256", String(64), nullable=False)

    image_width: Mapped[int] = mapped_column("ImageWidth", Integer, nullable=False, default=0)
    image_height: Mapped[int] = mapped_column("ImageHeight", Integer, nullable=False, default=0)

    #: False once replaced by a re-capture. Superseded rows are kept for audit.
    is_active: Mapped[bool] = mapped_column("IsActive", Boolean, nullable=False, default=True)

    #: Increments each time this pose is re-captured for the person.
    version: Mapped[int] = mapped_column("Version", Integer, nullable=False, default=1)

    created_at_utc: Mapped[datetime] = mapped_column(
        "CreatedAtUtc", UtcDateTime, nullable=False
    )


class AttendanceEvent(Base):
    """Audit record of a single attendance attempt - accepted or not.

    Written for every call so that disputes ("the machine did not recognise me") can be
    investigated after the fact.
    """

    __tablename__ = "AttendanceEvents"
    __table_args__ = (
        # The report query: one person, one date range, newest first.
        Index("IX_AttendanceEvents_Person_CreatedAt", "PersonId", "CreatedAtUtc"),
        # The tenant-wide daily register.
        Index("IX_AttendanceEvents_Tenant_CreatedAt", "TenantId", "CreatedAtUtc"),
        # Lets the retention job find probe frames to delete without a table scan.
        Index("IX_AttendanceEvents_CreatedAt", "CreatedAtUtc"),
    )

    id: Mapped[int] = mapped_column("Id", Integer, primary_key=True, autoincrement=True)

    tenant_id: Mapped[str] = mapped_column("TenantId", String(64), nullable=False, default="default")

    #: Null when a 1:N identification found nobody.
    person_id: Mapped[int | None] = mapped_column(
        "PersonId", Integer, ForeignKey("People.Id", ondelete="NO ACTION")
    )
    person: Mapped[Person | None] = relationship(back_populates="attendance_events", lazy="raise")

    direction: Mapped[AttendanceDirection] = mapped_column("Direction", Integer, nullable=False)
    decision: Mapped[AttendanceDecision] = mapped_column("Decision", Integer, nullable=False)

    #: Similarity of the best candidate, in the provider's own scale.
    match_score: Mapped[float] = mapped_column("MatchScore", Float, nullable=False, default=0.0)

    #: Threshold in force at decision time. Stored so later tuning stays explainable.
    threshold: Mapped[float] = mapped_column("Threshold", Float, nullable=False, default=0.0)

    matched_pose: Mapped[FacePose | None] = mapped_column("MatchedPose", Integer)
    matched_template_id: Mapped[int | None] = mapped_column("MatchedTemplateId", Integer)

    provider_id: Mapped[str] = mapped_column("ProviderId", String(128), nullable=False, default="")

    #: Stored probe frame, when probe retention is enabled.
    probe_image_object_key: Mapped[str | None] = mapped_column("ProbeImageObjectKey", String(512))

    failure_reason: Mapped[str | None] = mapped_column("FailureReason", String(1024))
    device_id: Mapped[str | None] = mapped_column("DeviceId", String(128))

    latitude: Mapped[float | None] = mapped_column("Latitude", Float)
    longitude: Mapped[float | None] = mapped_column("Longitude", Float)

    #: When the device captured the frame (client clock, untrusted).
    captured_at_utc: Mapped[datetime | None] = mapped_column(
        "CapturedAtUtc", UtcDateTime
    )

    #: When the server recorded the event (authoritative).
    created_at_utc: Mapped[datetime] = mapped_column(
        "CreatedAtUtc", UtcDateTime, nullable=False
    )

    processing_ms: Mapped[int] = mapped_column("ProcessingMs", Integer, nullable=False, default=0)
