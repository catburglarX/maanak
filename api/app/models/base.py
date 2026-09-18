"""Declarative base and shared column mixins."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Integer, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Explicit naming convention so that Alembic autogenerate and hand-written
#: migrations produce identical constraint names.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """Created/updated timestamps maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def version_column() -> Mapped[int]:
    """Optimistic-locking counter.

    Models that use this must also set
    ``__mapper_args__ = {"version_id_col": <Model>.version}`` so that SQLAlchemy
    adds the previous value to the UPDATE WHERE clause and raises StaleDataError
    on a concurrent write instead of silently overwriting.
    """
    return mapped_column(Integer, nullable=False, default=1, server_default="1")


def enum_check(column: str, allowed: tuple[str, ...], name: str) -> CheckConstraint:
    """CHECK constraint restricting a text column to a fixed set of values."""
    quoted = ", ".join(f"'{value}'" for value in allowed)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)
