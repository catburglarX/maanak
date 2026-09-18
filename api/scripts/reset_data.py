"""Clear all application data, keeping the schema.

For repeatable verification runs. Refuses to run when MAANAK_ENV=production.

    docker compose run --rm migrate python scripts/reset_data.py
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, text

from app.config import get_settings

#: audit_events is append-only via trigger, so TRUNCATE is used rather than DELETE.
#: TRUNCATE is a DDL-level operation and is not blocked by a row trigger.
TABLES = (
    "audit_events",
    "state_transitions",
    "candidate_revisions",
    "report_documents",
    "reports",
    "case_events",
    "notices",
    "cases",
    "findings",
    "declaration_candidates",
    "ocr_results",
    "evidence_derivatives",
    "analysis_jobs",
    "evidence",
    "inspection_faces",
    "inspections",
    "complaint_attachments",
    "complaints",
    "rule_test_runs",
    "rule_reviews",
    "rule_versions",
    "notice_templates",
    "product_merge_records",
    "responsible_parties",
    "product_identifiers",
    "products",
    "password_reset_tokens",
    "login_attempts",
    "user_sessions",
    "users",
)


def main() -> int:
    settings = get_settings()
    if settings.is_production:
        print("refusing to clear data in production")
        return 1

    engine = create_engine(settings.sync_database_url)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
        for sequence in (
            "maanak_inspection_reference_seq",
            "maanak_complaint_reference_seq",
            "maanak_case_reference_seq",
            "maanak_notice_reference_seq",
            "maanak_report_reference_seq",
        ):
            connection.execute(text(f"ALTER SEQUENCE {sequence} RESTART WITH 1"))
    engine.dispose()

    # Rate-limit counters live in Redis and are keyed by IP. Repeated verification
    # runs from the same container would otherwise exhaust the sign-in limit, which
    # fails closed by design.
    cleared_keys = _clear_rate_limits()

    print(
        f"cleared {len(TABLES)} tables, reset 5 reference sequences, "
        f"cleared {cleared_keys} rate-limit counters"
    )
    return 0


def _clear_rate_limits() -> int:
    import redis

    try:
        client = redis.from_url(get_settings().redis_url, socket_connect_timeout=2)
        keys = list(client.scan_iter(match="maanak:rl:*", count=500))
        if keys:
            client.delete(*keys)
        client.close()
    except Exception as exc:
        print(f"note: rate-limit counters not cleared ({type(exc).__name__})")
        return 0
    return len(keys)


if __name__ == "__main__":
    sys.exit(main())
