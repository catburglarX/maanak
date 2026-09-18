"""Verify the migrated schema in a live PostgreSQL database.

Checks that the migration produced what the models describe and that the
database-level protections actually reject the operations they are meant to
reject. Run inside the api image with DATABASE_URL pointing at a migrated
database:

    docker compose run --rm migrate python scripts/verify_schema.py
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import create_engine, inspect, text

from app.config import get_settings
from app.models import Base

failures: list[str] = []
checks = 0


def check(condition: bool, description: str) -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok    {description}")
    else:
        failures.append(description)
        print(f"  FAIL  {description}")


def main() -> int:
    engine = create_engine(get_settings().sync_database_url)
    inspector = inspect(engine)

    print("schema shape")
    actual = set(inspector.get_table_names())
    expected = set(Base.metadata.tables)
    missing = expected - actual
    check(not missing, f"all {len(expected)} model tables exist (missing: {sorted(missing)})")
    check("alembic_version" in actual, "alembic_version table present")

    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        check(revision == "0002_audit_and_sequences", f"head revision applied (got {revision})")

        print("indexes and constraints")
        index_count = connection.execute(
            text("SELECT count(*) FROM pg_indexes WHERE schemaname = 'public'")
        ).scalar_one()
        check(index_count >= 130, f"index count {index_count} >= 130")

        check_count = connection.execute(
            text(
                "SELECT count(*) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE c.contype = 'c' AND t.relnamespace = 'public'::regnamespace"
            )
        ).scalar_one()
        check(check_count >= 25, f"CHECK constraint count {check_count} >= 25")

        fk_count = connection.execute(
            text(
                "SELECT count(*) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE c.contype = 'f' AND t.relnamespace = 'public'::regnamespace"
            )
        ).scalar_one()
        check(fk_count >= 45, f"foreign key count {fk_count} >= 45")

        for sequence in (
            "maanak_inspection_reference_seq",
            "maanak_complaint_reference_seq",
            "maanak_case_reference_seq",
            "maanak_notice_reference_seq",
            "maanak_report_reference_seq",
        ):
            exists = connection.execute(
                text("SELECT count(*) FROM pg_class WHERE relname = :n AND relkind = 'S'"),
                {"n": sequence},
            ).scalar_one()
            check(exists == 1, f"sequence {sequence} exists")

        print("case-insensitive email uniqueness")
        definition = connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_users_email_lower'")
        ).scalar()
        check(
            definition is not None and "lower(" in definition.lower(),
            f"ix_users_email_lower is a functional index ({definition})",
        )

    print("database-enforced immutability")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_events "
                "(id, chain_key, sequence, event_hash, action, entity_type, "
                " actor_is_public, old_values, new_values, recorded_at) "
                "VALUES (:id, 'verify', 1, :h, 'schema.verification', 'system', "
                " false, '{}'::jsonb, '{}'::jsonb, :now)"
            ),
            {"id": str(uuid.uuid4()), "h": "0" * 64, "now": datetime.now(UTC)},
        )

    for operation, statement in (
        ("UPDATE", "UPDATE audit_events SET action = 'tampered' WHERE chain_key = 'verify'"),
        ("DELETE", "DELETE FROM audit_events WHERE chain_key = 'verify'"),
    ):
        rejected = False
        detail = ""
        try:
            with engine.begin() as connection:
                connection.execute(text(statement))
        except Exception as exc:
            rejected = "append-only" in str(exc)
            detail = str(exc).splitlines()[0][:90]
        check(rejected, f"audit_events rejects {operation} ({detail})")

    engine.dispose()

    print()
    if failures:
        print(f"{len(failures)} of {checks} checks FAILED")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"all {checks} schema checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
