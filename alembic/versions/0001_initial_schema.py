"""Initial schema: People, FaceTemplates, AttendanceEvents.

Mirrors the original EF Core migration, including the identity seed of 10001 and the filtered
unique index that keeps one active template per person and pose.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: First id handed out in every table. Five digits keeps ids a uniform width for a long time.
IDENTITY_SEED = 10001


def _identity(dialect: str) -> dict:
    """SQL Server starts its IDENTITY at 10001; other dialects just autoincrement."""
    if dialect == "mssql":
        return {"mssql_identity_start": IDENTITY_SEED, "mssql_identity_increment": 1}
    return {}


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    identity = _identity(dialect)

    op.create_table(
        "People",
        sa.Column("Id", sa.Integer(), autoincrement=True, nullable=False, **identity),
        sa.Column("TenantId", sa.String(length=64), nullable=False),
        sa.Column("ExternalId", sa.String(length=64), nullable=False),
        sa.Column("FullName", sa.String(length=200), nullable=False),
        sa.Column("Email", sa.String(length=256), nullable=True),
        sa.Column("Department", sa.String(length=128), nullable=True),
        sa.Column("Status", sa.Integer(), nullable=False),
        sa.Column("EnrollmentStatus", sa.Integer(), nullable=False),
        sa.Column("CreatedAtUtc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("UpdatedAtUtc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("EnrolledAtUtc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("RowVersion", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("Id"),
    )

    # The business key the capture screens send. Unique per tenant so a retried registration
    # is a conflict rather than a second identity.
    op.create_index(
        "UX_People_Tenant_ExternalId", "People", ["TenantId", "ExternalId"], unique=True
    )

    # Covers the 1:N candidate fetch, which filters on exactly these columns.
    op.create_index(
        "IX_People_Tenant_Enrollment_Status",
        "People",
        ["TenantId", "EnrollmentStatus", "Status"],
    )

    op.create_table(
        "FaceTemplates",
        sa.Column("Id", sa.Integer(), autoincrement=True, nullable=False, **identity),
        sa.Column("PersonId", sa.Integer(), nullable=False),
        sa.Column("Pose", sa.Integer(), nullable=False),
        sa.Column("ProviderId", sa.String(length=128), nullable=False),
        sa.Column("Dimension", sa.Integer(), nullable=False),
        # A 512-d float32 embedding is 2048 bytes. 8000 is the largest a SQL Server
        # varbinary can be; past that it becomes varbinary(max), which pushes the payload
        # off-row and makes the candidate scan noticeably slower.
        sa.Column("TemplateData", sa.LargeBinary(length=8000), nullable=False),
        sa.Column("QualityScore", sa.Float(), nullable=False),
        sa.Column("DetectionScore", sa.Float(), nullable=False),
        sa.Column("ImageObjectKey", sa.String(length=512), nullable=False),
        sa.Column("ImageContentType", sa.String(length=64), nullable=False),
        sa.Column("ImageSizeBytes", sa.BigInteger(), nullable=False),
        sa.Column("ImageSha256", sa.String(length=64), nullable=False),
        sa.Column("ImageWidth", sa.Integer(), nullable=False),
        sa.Column("ImageHeight", sa.Integer(), nullable=False),
        sa.Column("IsActive", sa.Boolean(), nullable=False),
        sa.Column("Version", sa.Integer(), nullable=False),
        sa.Column("CreatedAtUtc", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["PersonId"], ["People.Id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("Id"),
    )

    # The query behind every verification: active templates for a person.
    op.create_index(
        "IX_FaceTemplates_Person_Active_Pose",
        "FaceTemplates",
        ["PersonId", "IsActive", "Pose"],
    )

    # Swapping the recognition model has to find its orphans quickly.
    op.create_index(
        "IX_FaceTemplates_Provider_Active", "FaceTemplates", ["ProviderId", "IsActive"]
    )

    # Backs the "same frame re-sent for another pose" check.
    op.create_index(
        "IX_FaceTemplates_Person_ImageHash", "FaceTemplates", ["PersonId", "ImageSha256"]
    )

    # A person must not have two live templates for one pose. The enrollment service already
    # retires the previous row, but a database-level guarantee is what keeps a concurrent
    # double upload from producing two "active" fronts. Filtered-index syntax is dialect
    # specific, so it is only applied where it is known to work.
    if dialect == "mssql":
        op.execute(
            "CREATE UNIQUE INDEX UX_FaceTemplates_Person_Pose_Active "
            "ON FaceTemplates (PersonId, Pose) WHERE IsActive = 1"
        )
    elif dialect in ("sqlite", "postgresql"):
        op.execute(
            'CREATE UNIQUE INDEX UX_FaceTemplates_Person_Pose_Active '
            'ON "FaceTemplates" ("PersonId", "Pose") WHERE "IsActive" = 1'
        )

    op.create_table(
        "AttendanceEvents",
        sa.Column("Id", sa.Integer(), autoincrement=True, nullable=False, **identity),
        sa.Column("TenantId", sa.String(length=64), nullable=False),
        sa.Column("PersonId", sa.Integer(), nullable=True),
        sa.Column("Direction", sa.Integer(), nullable=False),
        sa.Column("Decision", sa.Integer(), nullable=False),
        sa.Column("MatchScore", sa.Float(), nullable=False),
        sa.Column("Threshold", sa.Float(), nullable=False),
        sa.Column("MatchedPose", sa.Integer(), nullable=True),
        sa.Column("MatchedTemplateId", sa.Integer(), nullable=True),
        sa.Column("ProviderId", sa.String(length=128), nullable=False),
        sa.Column("ProbeImageObjectKey", sa.String(length=512), nullable=True),
        sa.Column("FailureReason", sa.String(length=1024), nullable=True),
        sa.Column("DeviceId", sa.String(length=128), nullable=True),
        sa.Column("Latitude", sa.Float(), nullable=True),
        sa.Column("Longitude", sa.Float(), nullable=True),
        sa.Column("CapturedAtUtc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("CreatedAtUtc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ProcessingMs", sa.Integer(), nullable=False),
        # Attendance history outlives the person record on purpose: deleting an employee must
        # not silently rewrite last quarter of attendance.
        # NO ACTION rather than RESTRICT: SQL Server has no RESTRICT keyword, and NO ACTION
        # has the behaviour wanted here - the delete is rejected, not cascaded.
        sa.ForeignKeyConstraint(["PersonId"], ["People.Id"], ondelete="NO ACTION"),
        sa.PrimaryKeyConstraint("Id"),
    )

    # The report query: one person, one date range, newest first.
    op.create_index(
        "IX_AttendanceEvents_Person_CreatedAt",
        "AttendanceEvents",
        ["PersonId", "CreatedAtUtc"],
    )

    # The tenant-wide daily register.
    op.create_index(
        "IX_AttendanceEvents_Tenant_CreatedAt",
        "AttendanceEvents",
        ["TenantId", "CreatedAtUtc"],
    )

    # Lets the retention job find probe frames to delete without a table scan.
    op.create_index("IX_AttendanceEvents_CreatedAt", "AttendanceEvents", ["CreatedAtUtc"])


def downgrade() -> None:
    op.drop_index("IX_AttendanceEvents_CreatedAt", table_name="AttendanceEvents")
    op.drop_index("IX_AttendanceEvents_Tenant_CreatedAt", table_name="AttendanceEvents")
    op.drop_index("IX_AttendanceEvents_Person_CreatedAt", table_name="AttendanceEvents")
    op.drop_table("AttendanceEvents")

    op.execute("DROP INDEX IF EXISTS UX_FaceTemplates_Person_Pose_Active")
    op.drop_index("IX_FaceTemplates_Person_ImageHash", table_name="FaceTemplates")
    op.drop_index("IX_FaceTemplates_Provider_Active", table_name="FaceTemplates")
    op.drop_index("IX_FaceTemplates_Person_Active_Pose", table_name="FaceTemplates")
    op.drop_table("FaceTemplates")

    op.drop_index("IX_People_Tenant_Enrollment_Status", table_name="People")
    op.drop_index("UX_People_Tenant_ExternalId", table_name="People")
    op.drop_table("People")
