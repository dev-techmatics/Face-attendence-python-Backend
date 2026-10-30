"""Declarative base, shared by the ORM models and Alembic."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


#: First id handed out in every table. Five digits keeps ids a uniform width for a long time.
#: Expect occasional gaps: a rolled-back registration still consumes a number, and SQL Server
#: may skip ahead after a restart. Never rely on the numbers being consecutive.
IDENTITY_SEED = 10001
