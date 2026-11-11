"""Hand edits to attendance: an append-only audit table, and a marker on the row.

The register is evidence, and the moment an admin can change a punch the only thing keeping
it evidence is a complete account of the change. So every edit writes a row here, nothing
ever updates or deletes one, and the attendance row itself carries a flag so that every
screen showing a punch can say it was touched without having to join.

Revision ID: 0002_attendance_edits
Revises: 0001_initial_schema
Create Date: 2026-11-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_attendance_edits"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Matches the other tables, so ids stay a uniform width across the schema.
IDENTITY_SEED = 10001


# Spelled exactly as 0001 spells its timestamps. SQLAlchemy's SQL Server dialect already
# renders DateTime(timezone=True) as datetimeoffset, which is the one type that round-trips
# an offset; naming the dialect type by hand only adds a way for the two migrations to
# disagree.
TIMESTAMP = sa.DateTime(timezone=True)


def upgrade() -> None:
    bind = op.get_bind()
    mssql = bind.dialect.name == "mssql"

    op.add_column(
        "AttendanceEvents",
        sa.Column("EditedAtUtc", TIMESTAMP, nullable=True),
    )

    op.create_table(
        "AttendanceEdits",
        sa.Column(
            "Id",
            sa.Integer(),
            sa.Identity(start=IDENTITY_SEED, increment=1) if mssql else None,
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("TenantId", sa.String(64), nullable=False, server_default="default"),
        sa.Column("AttendanceEventId", sa.Integer(), nullable=False),
        sa.Column("Field", sa.String(32), nullable=False),
        sa.Column("OldValue", sa.String(128), nullable=True),
        sa.Column("NewValue", sa.String(128), nullable=True),
        sa.Column("Reason", sa.String(512), nullable=False),
        sa.Column("ChangedByRole", sa.String(32), nullable=False),
        sa.Column("ChangedBy", sa.String(128), nullable=True),
        sa.Column("ChangedFrom", sa.String(64), nullable=True),
        sa.Column("ChangedAtUtc", TIMESTAMP, nullable=False),
        # NO ACTION, like the attendance-to-person link: an audit row outliving its subject
        # is better than a delete that silently carries off the record of the change.
        sa.ForeignKeyConstraint(
            ["AttendanceEventId"],
            ["AttendanceEvents.Id"],
            name="FK_AttendanceEdits_AttendanceEvents",
            ondelete="NO ACTION",
        ),
    )

    # The two questions asked of this table: "what was done to this punch" and "what has been
    # changed lately".
    op.create_index(
        "IX_AttendanceEdits_Event", "AttendanceEdits", ["AttendanceEventId"]
    )
    op.create_index(
        "IX_AttendanceEdits_ChangedAt",
        "AttendanceEdits",
        ["TenantId", "ChangedAtUtc"],
    )


def downgrade() -> None:
    op.drop_index("IX_AttendanceEdits_ChangedAt", table_name="AttendanceEdits")
    op.drop_index("IX_AttendanceEdits_Event", table_name="AttendanceEdits")
    op.drop_table("AttendanceEdits")
    op.drop_column("AttendanceEvents", "EditedAtUtc")
