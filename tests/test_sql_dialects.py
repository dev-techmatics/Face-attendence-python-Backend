"""Compiles the real queries for SQL Server as well as SQLite.

The suite runs on SQLite because it needs no server, but production is SQL Server — and the
two disagree about more than performance. `IsActive IS 1` is valid SQLite and a syntax error in
T-SQL, so a query written with `.is_(True)` passed every test and then failed on the first
real registration. These tests compile the statements for both dialects, which catches that
class of difference without a server to connect to.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import mssql, sqlite
from sqlalchemy.orm import selectinload

from app.core.enums import AttendanceDecision, FacePose, PersonStatus
from app.domain.models import AttendanceEvent, FaceTemplate, Person

DIALECTS = {"mssql": mssql.dialect(), "sqlite": sqlite.dialect()}

#: Forms that compile on SQLite but are invalid T-SQL. `IS <value>` is the one that bit us:
#: T-SQL has only `IS NULL` / `IS NOT NULL`.
FORBIDDEN = ("IS 1", "IS 0", "IS true", "IS false")


def statements():
    """One entry per query shape the repositories actually issue."""
    active = FaceTemplate.is_active == True  # noqa: E712

    return {
        "active_for_person": select(FaceTemplate)
        .where(FaceTemplate.person_id == 1, active)
        .order_by(FaceTemplate.pose),
        "active_for_pose": select(FaceTemplate).where(
            FaceTemplate.person_id == 1, FaceTemplate.pose == FacePose.Front, active
        ),
        "candidates": select(
            FaceTemplate.person_id,
            FaceTemplate.id,
            FaceTemplate.pose,
            FaceTemplate.template_data,
            FaceTemplate.image_object_key,
        )
        .join(Person, Person.id == FaceTemplate.person_id)
        .where(
            active,
            FaceTemplate.provider_id == "onnx",
            Person.tenant_id == "default",
            Person.status == PersonStatus.Active,
            FaceTemplate.person_id != 2,
        ),
        "image_hash_exists": select(func.count())
        .select_from(FaceTemplate)
        .where(
            FaceTemplate.person_id == 1,
            active,
            FaceTemplate.pose != FacePose.Front,
            FaceTemplate.image_sha256 == "abc",
        ),
        "max_version": select(func.max(FaceTemplate.version)).where(
            FaceTemplate.person_id == 1, FaceTemplate.pose == FacePose.Front
        ),
        "person_with_templates": select(Person)
        .where(Person.id == 1)
        .options(selectinload(Person.face_templates.and_(active))),
        "person_search": select(Person)
        .where(Person.tenant_id == "default", Person.full_name.like("%a%"))
        .order_by(Person.full_name)
        .offset(10)
        .limit(25),
        "attendance_window": select(AttendanceEvent)
        .where(
            AttendanceEvent.person_id == 1,
            AttendanceEvent.decision == AttendanceDecision.Accepted,
        )
        .order_by(AttendanceEvent.created_at_utc.desc())
        .limit(1),
        "expired_probes": select(AttendanceEvent.id, AttendanceEvent.probe_image_object_key)
        .where(AttendanceEvent.probe_image_object_key.is_not(None))
        .order_by(AttendanceEvent.created_at_utc)
        .limit(500),
    }


@pytest.mark.parametrize("dialect_name", sorted(DIALECTS))
@pytest.mark.parametrize("statement_name", sorted(statements()))
def test_every_query_compiles_for_both_dialects(statement_name, dialect_name):
    statement = statements()[statement_name]

    # Raises if the construct cannot be rendered for this backend at all.
    sql = str(statement.compile(dialect=DIALECTS[dialect_name]))

    assert sql


@pytest.mark.parametrize("statement_name", sorted(statements()))
def test_no_query_emits_a_form_sql_server_rejects(statement_name):
    sql = str(statements()[statement_name].compile(dialect=DIALECTS["mssql"]))

    for form in FORBIDDEN:
        assert form not in sql, (
            f"{statement_name} compiles to T-SQL containing {form!r}, which SQL Server "
            f"rejects:\n{sql}"
        )


def test_the_boolean_comparison_renders_as_a_equals_comparison():
    """Guards the specific fix, so nobody 'tidies' it back to .is_(True)."""
    sql = str(
        select(FaceTemplate.id)
        .where(FaceTemplate.is_active == True)  # noqa: E712
        .compile(dialect=DIALECTS["mssql"])
    )

    assert "IsActive] = 1" in sql


def test_is_true_would_have_been_caught():
    """The bug, reproduced: this is what the old code compiled to."""
    sql = str(
        select(FaceTemplate.id)
        .where(FaceTemplate.is_active.is_(True))
        .compile(dialect=DIALECTS["mssql"])
    )

    assert "IS 1" in sql, (
        "SQLAlchemy no longer renders .is_(True) as 'IS 1' on mssql; if so this guard and the "
        "comments explaining it can be simplified."
    )


def test_pagination_renders_without_limit_for_sql_server():
    # SQL Server has no LIMIT. SQLAlchemy renders paging either as OFFSET/FETCH or as a
    # ROW_NUMBER() window, depending on the statement; both are valid, and which one appears
    # is not this project's business. What matters is that LIMIT never reaches the server.
    sql = str(statements()["person_search"].compile(dialect=DIALECTS["mssql"]))

    assert "LIMIT" not in sql
    assert "OFFSET" in sql or "mssql_rn" in sql

    # SQLite keeps LIMIT, so the same statement is paged correctly on both.
    assert "LIMIT" in str(statements()["person_search"].compile(dialect=DIALECTS["sqlite"]))
