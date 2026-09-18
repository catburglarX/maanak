"""audit append-only enforcement and reference sequences

Revision ID: 0002_audit_and_sequences
Revises: 0001_initial_schema
Created: 2026-09-17

Two things the ORM cannot express:

1. ``audit_events`` must be append-only. A trigger rejects UPDATE and DELETE, so
   even a compromised application account cannot rewrite history. Combined with
   the per-event hash chain this makes tampering detectable and blocked.

2. Human-facing references (INSP-..., CMP-..., CASE-..., RPT-...) are drawn from
   PostgreSQL sequences. A sequence is transactional-safe under concurrency, which
   a SELECT max()+1 read is not.

Explicit DDL only. Do not import app.models here.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_audit_and_sequences"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REFERENCE_SEQUENCES = (
    "maanak_inspection_reference_seq",
    "maanak_complaint_reference_seq",
    "maanak_case_reference_seq",
    "maanak_notice_reference_seq",
    "maanak_report_reference_seq",
)


def upgrade() -> None:
    # --- Append-only audit ------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION maanak_audit_events_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_events is append-only; % is not permitted', TG_OP
                USING ERRCODE = '42501';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_block_update
        BEFORE UPDATE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION maanak_audit_events_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_block_delete
        BEFORE DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION maanak_audit_events_immutable();
        """
    )

    # --- Reference sequences ----------------------------------------------
    for sequence_name in REFERENCE_SEQUENCES:
        op.execute(f"CREATE SEQUENCE IF NOT EXISTS {sequence_name} AS bigint START WITH 1")

    # --- Notices are frozen once issued ----------------------------------
    # A frozen notice may still record delivery, response and withdrawal, but the
    # served text, its hash and the template version can never change.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION maanak_notices_frozen_body()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.is_frozen AND (
                   NEW.rendered_body IS DISTINCT FROM OLD.rendered_body
                OR NEW.body_sha256 IS DISTINCT FROM OLD.body_sha256
                OR NEW.template_code IS DISTINCT FROM OLD.template_code
                OR NEW.template_version IS DISTINCT FROM OLD.template_version
                OR NEW.issued_at IS DISTINCT FROM OLD.issued_at
            ) THEN
                RAISE EXCEPTION
                    'notice % is frozen; served text cannot be altered', OLD.reference
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER notices_protect_frozen_body
        BEFORE UPDATE ON notices
        FOR EACH ROW EXECUTE FUNCTION maanak_notices_frozen_body();
        """
    )

    # --- Report snapshots are immutable ----------------------------------
    # Withdrawal and supersession are recorded in separate columns; the snapshot
    # and its hash are fixed at issue time.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION maanak_reports_frozen_snapshot()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.snapshot IS DISTINCT FROM OLD.snapshot
               OR NEW.snapshot_sha256 IS DISTINCT FROM OLD.snapshot_sha256
               OR NEW.issued_at IS DISTINCT FROM OLD.issued_at
               OR NEW.reference IS DISTINCT FROM OLD.reference
               OR NEW.verification_code IS DISTINCT FROM OLD.verification_code
            THEN
                RAISE EXCEPTION
                    'report % snapshot is immutable', OLD.reference
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER reports_protect_snapshot
        BEFORE UPDATE ON reports
        FOR EACH ROW EXECUTE FUNCTION maanak_reports_frozen_snapshot();
        """
    )

    # --- Evidence originals are immutable --------------------------------
    # Analysis results, quality findings and retention flags are expected to
    # change. File identity and storage location must not.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION maanak_evidence_identity_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.sha256 IS DISTINCT FROM OLD.sha256
               OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
               OR NEW.storage_bucket IS DISTINCT FROM OLD.storage_bucket
               OR NEW.storage_key IS DISTINCT FROM OLD.storage_key
               OR NEW.inspection_id IS DISTINCT FROM OLD.inspection_id
               OR NEW.server_received_at IS DISTINCT FROM OLD.server_received_at
            THEN
                RAISE EXCEPTION
                    'evidence identity is immutable (evidence %)', OLD.id
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER evidence_protect_identity
        BEFORE UPDATE ON evidence
        FOR EACH ROW EXECUTE FUNCTION maanak_evidence_identity_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS evidence_protect_identity ON evidence")
    op.execute("DROP FUNCTION IF EXISTS maanak_evidence_identity_immutable()")
    op.execute("DROP TRIGGER IF EXISTS reports_protect_snapshot ON reports")
    op.execute("DROP FUNCTION IF EXISTS maanak_reports_frozen_snapshot()")
    op.execute("DROP TRIGGER IF EXISTS notices_protect_frozen_body ON notices")
    op.execute("DROP FUNCTION IF EXISTS maanak_notices_frozen_body()")
    for sequence_name in REFERENCE_SEQUENCES:
        op.execute(f"DROP SEQUENCE IF EXISTS {sequence_name}")
    op.execute("DROP TRIGGER IF EXISTS audit_events_block_delete ON audit_events")
    op.execute("DROP TRIGGER IF EXISTS audit_events_block_update ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS maanak_audit_events_immutable()")
