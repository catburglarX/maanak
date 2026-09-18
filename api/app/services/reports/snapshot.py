"""Report snapshots.

A report is a frozen copy of everything the conclusion rested on. At issue time the
inspection, the product, the evidence with its hashes, every reviewed reading, every
finding with its cited rule version and calculation, and the officer's decision are
serialised into one structure and hashed.

Documents are rendered from that structure, never from live tables. So a later edit to
a product record, or a new version of a rule, cannot change what an issued report says
or what its hash verifies.

The hash covers the snapshot only. Document bytes have their own hashes, recorded per
format, because a PDF and a DOCX of the same snapshot are different files.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...domain.enums import ReportState, ReviewState
from ...errors import ConflictError, GuardFailedError, NotFoundError
from ...models.evidence import Evidence
from ...models.finding import CandidateRevision, DeclarationCandidate, Finding
from ...models.inspection import Inspection, InspectionFace, StateTransition
from ...models.product import Product
from ...models.report import Report
from ...models.rule import RuleVersion
from ...models.user import User
from ...observability import get_logger
from ..canonical import CANONICAL_ALGORITHM, canonical_sha256, json_safe
from ..phrasing import counted, verb
from ..references import REPORT_PREFIX, next_reference

logger = get_logger(__name__)

SNAPSHOT_VERSION = "1"


@dataclass
class IssuedReport:
    report: Report
    snapshot: dict[str, Any]


async def build_snapshot(
    db: AsyncSession, *, inspection: Inspection, issuing_workspace: str
) -> dict[str, Any]:
    """Assemble the frozen record. Reads only; writes nothing."""
    product = await db.scalar(
        select(Product)
        .options(
            selectinload(Product.identifiers),
            selectinload(Product.responsible_parties),
        )
        .where(Product.id == inspection.product_id)
        .execution_options(populate_existing=True)
    )
    if product is None:
        raise NotFoundError("The product record for this inspection is missing.")

    evidence_rows = list(
        await db.scalars(
            select(Evidence)
            .where(Evidence.inspection_id == inspection.id)
            .order_by(Evidence.face.asc(), Evidence.sequence.asc())
        )
    )
    faces = list(
        await db.scalars(
            select(InspectionFace).where(InspectionFace.inspection_id == inspection.id)
        )
    )
    candidates = list(
        await db.scalars(
            select(DeclarationCandidate)
            .where(DeclarationCandidate.inspection_id == inspection.id)
            .order_by(DeclarationCandidate.declaration_type.asc())
        )
    )
    findings = list(
        await db.scalars(
            select(Finding)
            .where(Finding.inspection_id == inspection.id, Finding.is_current.is_(True))
            .order_by(Finding.created_at.asc())
        )
    )
    transitions = list(
        await db.scalars(
            select(StateTransition)
            .where(
                StateTransition.entity_type == "inspection",
                StateTransition.entity_id == inspection.id,
            )
            .order_by(StateTransition.occurred_at.asc())
        )
    )

    people: dict[str, dict[str, Any]] = {}
    for user_id in {
        inspection.created_by_id,
        inspection.assigned_officer_id,
        inspection.reviewer_id,
        inspection.decided_by_id,
    }:
        if user_id is None:
            continue
        account = await db.get(User, user_id)
        if account is not None:
            people[str(user_id)] = {
                "name": account.name,
                "email": account.email,
                "role": account.role,
                "designation": account.designation,
                "jurisdiction_code": account.jurisdiction_code,
                "jurisdiction_name": account.jurisdiction_name,
            }

    rule_versions: dict[str, dict[str, Any]] = {}
    for finding in findings:
        key = str(finding.rule_version_id)
        if key in rule_versions:
            continue
        rule = await db.get(RuleVersion, finding.rule_version_id)
        if rule is None:
            continue
        rule_versions[key] = {
            "code": rule.code,
            "version": rule.version,
            "title": rule.title,
            "citation": rule.citation,
            "source_url": rule.source_url,
            "source_document_title": rule.source_document_title,
            "gazette_reference": rule.gazette_reference,
            "effective_from": rule.effective_from,
            "effective_to": rule.effective_to,
            "status_at_issue": rule.status,
            "plain_explanation": rule.plain_explanation,
            "interpretation_note": rule.interpretation_note,
            "uncertainty_note": rule.uncertainty_note,
            "legal_authority_confirmed": rule.legal_authority_confirmed,
            "legal_authority_note": rule.legal_authority_note,
            "approved_at": rule.approved_at,
            "approver_id": str(rule.approver_id) if rule.approver_id else None,
        }

    revision_counts: dict[str, int] = {}
    for candidate in candidates:
        total = len(
            list(
                await db.scalars(
                    select(CandidateRevision.id).where(
                        CandidateRevision.candidate_id == candidate.id
                    )
                )
            )
        )
        revision_counts[str(candidate.id)] = total

    snapshot = {
        "snapshot_version": SNAPSHOT_VERSION,
        "issuing_workspace": issuing_workspace,
        "inspection": {
            "id": str(inspection.id),
            "reference": inspection.reference,
            "state": inspection.state,
            "source": inspection.source,
            "inspection_date": inspection.inspection_date,
            "jurisdiction_code": inspection.jurisdiction_code,
            "jurisdiction_name": inspection.jurisdiction_name,
            "premises_name": inspection.premises_name,
            "premises_address": inspection.premises_address,
            "marketplace_name": inspection.marketplace_name,
            "listing_url": inspection.listing_url,
            "batch_reference": inspection.batch_reference,
            "complaint_id": str(inspection.complaint_id) if inspection.complaint_id else None,
            "decision": inspection.decision,
            "decision_note": inspection.decision_note,
            "decided_at": inspection.decided_at,
            "decided_by_id": str(inspection.decided_by_id) if inspection.decided_by_id else None,
            "checks_executed_at": inspection.checks_executed_at,
            "rules_evaluated_count": inspection.rules_evaluated_count,
            "context": inspection.context or {},
            "version_at_issue": inspection.version,
        },
        "product": {
            "id": str(product.id),
            "brand": product.brand,
            "name": product.name,
            "common_generic_name": product.common_generic_name,
            "commodity_category": product.commodity_category,
            "is_food": product.is_food,
            "package_type": product.package_type,
            "quantity_kind": product.quantity_kind,
            "declared_net_quantity": product.declared_net_quantity,
            "declared_net_quantity_unit": product.declared_net_quantity_unit,
            "net_quantity_base": product.net_quantity_base,
            "is_imported": product.is_imported,
            "country_of_origin": product.country_of_origin,
            "is_multipiece": product.is_multipiece,
            "pieces_per_package": product.pieces_per_package,
            "identifiers": [
                {
                    "scheme": item.scheme,
                    "value": item.value,
                    "check_digit_valid": item.check_digit_valid,
                    "officer_confirmed": item.officer_confirmed,
                }
                for item in product.identifiers
            ],
            "responsible_parties": [
                {
                    "party_role": item.party_role,
                    "legal_name": item.legal_name,
                    "address_line": item.address_line,
                    "locality": item.locality,
                    "state": item.state,
                    "pin_code": item.pin_code,
                    "country": item.country,
                    "consumer_care_email": item.consumer_care_email,
                    "consumer_care_phone": item.consumer_care_phone,
                }
                for item in product.responsible_parties
            ],
        },
        "package_faces": [
            {
                "face": item.face,
                "capture_state": item.capture_state,
                "is_required": item.is_required,
                "reason": item.reason,
            }
            for item in sorted(faces, key=lambda row: row.face)
        ],
        "evidence": [
            {
                "id": str(item.id),
                "face": item.face,
                "sequence": item.sequence,
                "original_filename": item.original_filename,
                "detected_mime_type": item.detected_mime_type,
                "size_bytes": item.size_bytes,
                "hash_algorithm": item.hash_algorithm,
                "sha256": item.sha256,
                "original_width": item.original_width,
                "original_height": item.original_height,
                "storage_bucket": item.storage_bucket,
                "storage_key": item.storage_key,
                "uploaded_by_id": str(item.uploaded_by_id) if item.uploaded_by_id else None,
                "server_received_at": item.server_received_at,
                "device_make": item.device_make,
                "device_model": item.device_model,
                "quality_verdict": (item.quality or {}).get("verdict"),
                "quality_actions": (item.quality or {}).get("actions", []),
                "quality_override_reason": item.quality_override_reason,
                "analysis_state": item.analysis_state,
                "barcodes": item.barcodes or [],
            }
            for item in evidence_rows
        ],
        "readings": [
            {
                "id": str(item.id),
                "declaration_type": item.declaration_type,
                "evidence_id": str(item.evidence_id) if item.evidence_id else None,
                "machine_state": item.machine_state,
                "machine_reading": item.normalised_value or {},
                "machine_text": item.matched_text,
                "machine_confidence": item.machine_confidence,
                "parser": f"{item.parser_name} v{item.parser_version}",
                "region": item.region or [],
                "review_state": item.review_state,
                "officer_corrected_value": item.corrected_value or {},
                "correction_reason": item.correction_reason,
                "review_note": item.review_note,
                "reviewed_by_id": str(item.reviewed_by_id) if item.reviewed_by_id else None,
                "reviewed_at": item.reviewed_at,
                "revision_count": revision_counts.get(str(item.id), 0),
                "used_for_legal_tests": item.is_usable_for_rules,
            }
            for item in candidates
        ],
        "findings": [
            {
                "id": str(item.id),
                "declaration_type": item.declaration_type,
                "engine_outcome": item.outcome,
                "officer_outcome": item.officer_outcome,
                "effective_outcome": item.effective_outcome,
                "explanation": item.explanation,
                "expected_value": item.expected_value,
                "observed_value": item.observed_value,
                "calculation": item.calculation or [],
                "test_inputs": item.test_inputs or {},
                "selection_reason": item.selection_reason or {},
                "rule_version_id": str(item.rule_version_id),
                "officer_note": item.officer_note,
                "engine_version": item.engine_version,
            }
            for item in findings
        ],
        "rule_versions": rule_versions,
        "people": people,
        "timeline": [
            {
                "from_state": item.from_state,
                "to_state": item.to_state,
                "actor_id": str(item.actor_id) if item.actor_id else None,
                "actor_role": item.actor_role,
                "reason": item.reason,
                "occurred_at": item.occurred_at,
            }
            for item in transitions
        ],
        "limits": [
            "This report records what was read from the photographs supplied and the "
            "tests applied to them. It is not a statement about the physical contents "
            "of the package: net quantity was not verified by weighing or measuring.",
            "Rule versions cited here carry their own confirmation status. Where "
            "'legal authority confirmed' is false, the interpretation has been approved "
            "for use inside this workspace but has not been endorsed by a statutory "
            "authority.",
            "Optical character recognition can misread print. Every reading used in a "
            "legal test was reviewed by the named officer, and corrections are shown "
            "alongside the original machine reading.",
        ],
    }
    return json_safe(snapshot)


async def issue_report(
    db: AsyncSession,
    *,
    inspection: Inspection,
    issued_by_id: uuid.UUID,
    issuing_workspace: str,
) -> IssuedReport:
    """Freeze the inspection into a report and record its hash.

    Preconditions are enforced: a decision with a reasoned note must exist, no reading
    may still be pending, and the checks must have been run since the last change.
    """
    if inspection.decision is None or not inspection.decision_note:
        raise GuardFailedError(
            "Record the decision and its reason before issuing a report.",
            code="decision_required",
        )
    if inspection.checks_executed_at is None:
        raise GuardFailedError(
            "The legal checks have not been run since the last change. Run them before "
            "issuing a report.",
            code="checks_not_current",
        )

    pending = list(
        await db.scalars(
            select(DeclarationCandidate.id).where(
                DeclarationCandidate.inspection_id == inspection.id,
                DeclarationCandidate.review_state == ReviewState.PENDING,
            )
        )
    )
    if pending:
        raise GuardFailedError(
            f"{counted(len(pending), 'machine reading')} "
            f"{verb(len(pending), 'is', 'are')} still unreviewed.",
            code="review_incomplete",
        )

    existing = list(
        await db.scalars(
            select(Report).where(
                Report.inspection_id == inspection.id, Report.state == ReportState.ISSUED
            )
        )
    )
    revision = 1
    if existing:
        raise ConflictError(
            "A report has already been issued for this inspection. Withdraw it before "
            "issuing a replacement.",
            code="report_already_issued",
            details={"existing_report_reference": existing[0].reference},
        )
    previous = list(
        await db.scalars(select(Report.revision).where(Report.inspection_id == inspection.id))
    )
    if previous:
        revision = max(previous) + 1

    snapshot = await build_snapshot(db, inspection=inspection, issuing_workspace=issuing_workspace)
    digest = canonical_sha256(snapshot)

    from ...security.tokens import generate_verification_code

    reference = await next_reference(db, REPORT_PREFIX)
    code = generate_verification_code()

    report = Report(
        id=uuid.uuid4(),
        reference=reference,
        inspection_id=inspection.id,
        revision=revision,
        state=ReportState.ISSUED,
        snapshot=snapshot,
        snapshot_sha256=digest,
        snapshot_algorithm=CANONICAL_ALGORITHM,
        issued_at=datetime.now(UTC),
        issued_by_id=issued_by_id,
        issuing_workspace=issuing_workspace,
        jurisdiction_code=inspection.jurisdiction_code,
        signature_status="none",
        verification_code=code,
    )
    db.add(report)
    await db.flush()

    logger.info(
        "report_issued",
        report=reference,
        inspection=inspection.reference,
        snapshot_sha256=digest[:16],
        revision=revision,
    )
    return IssuedReport(report=report, snapshot=snapshot)


def verify_snapshot(report: Report) -> dict[str, Any]:
    """Recompute the snapshot hash and compare it with the stored value."""
    recomputed = canonical_sha256(report.snapshot)
    intact = recomputed == report.snapshot_sha256
    return {
        "intact": intact,
        "recorded_sha256": report.snapshot_sha256,
        "recomputed_sha256": recomputed,
        "algorithm": report.snapshot_algorithm,
        "detail": (
            "The stored report content matches the hash recorded when it was issued."
            if intact
            else "The stored report content does not match its recorded hash. "
            "Treat this report as unreliable and investigate."
        ),
    }


def public_summary(report: Report) -> dict[str, Any]:
    """What a member of the public may see when verifying a report.

    Deliberately minimal. It confirms that a report with this reference was issued,
    when, by which workspace, and whether it is still current. It reveals nothing
    about the product, the premises, the officer or the findings, because a report
    reference is not an authorisation to read the case.
    """
    snapshot = report.snapshot or {}
    inspection = snapshot.get("inspection", {})
    verification = verify_snapshot(report)
    return {
        "report_reference": report.reference,
        "verification_code": report.verification_code,
        "issued_at": report.issued_at,
        "issuing_workspace": report.issuing_workspace,
        "jurisdiction": inspection.get("jurisdiction_name"),
        "state": report.state,
        "revision": report.revision,
        "document_hash": report.snapshot_sha256,
        "hash_algorithm": report.snapshot_algorithm,
        "content_intact": verification["intact"],
        "signature_status": report.signature_status,
        "withdrawn_at": report.withdrawn_at,
        "superseded": report.superseded_by_id is not None,
        "note": (
            "This page confirms that a report with this reference was issued by the "
            "workspace named above and that its stored content still matches the hash "
            "recorded at issue. It does not disclose the inspection itself."
        ),
        "not_a_government_service": (
            "Maanak is not a government service and this verification is not a "
            "government certification."
        ),
    }
