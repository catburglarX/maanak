"""Rule engine.

Runs the approved, applicable rule versions against one inspection and writes a
``Finding`` for each. Every finding records the rule version it cites, why that
version applied, the inputs it consumed and the arithmetic it performed.

Only officer-reviewed candidates are used as inputs. A candidate still pending
review, or rejected by the officer, contributes nothing, so the engine can never
reach a conclusion from an unreviewed machine reading.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.enums import (
    DeclarationType,
    FaceCaptureState,
    LegalOutcome,
    MachineState,
    ReviewState,
    RuleStatus,
)
from ...models.finding import DeclarationCandidate, Finding
from ...models.inspection import Inspection, InspectionFace
from ...models.product import Product
from ...models.rule import RuleVersion
from ...observability import get_logger
from ..phrasing import counted, verb
from .checks import ENGINE_VERSION, CheckInput, run_check
from .selection import SelectionContext
from .selection import select as select_applicable_rules

logger = get_logger(__name__)


@dataclass
class EngineResult:
    """Outcome of one evaluation run."""

    findings_written: int
    rules_considered: int
    rules_applied: int
    outcomes: dict[str, int] = field(default_factory=dict)
    selected: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    unreviewed_candidates: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "findings_written": self.findings_written,
            "rules_considered": self.rules_considered,
            "rules_applied": self.rules_applied,
            "outcomes": self.outcomes,
            "selected_rules": self.selected,
            "rejected_rules": self.rejected,
            "unreviewed_candidates": self.unreviewed_candidates,
            "notes": self.notes,
        }

    @property
    def has_violation(self) -> bool:
        return self.outcomes.get(LegalOutcome.NON_COMPLIANT.value, 0) > 0

    @property
    def has_undetermined(self) -> bool:
        return (
            self.outcomes.get(LegalOutcome.UNABLE_TO_DETERMINE.value, 0) > 0
            or self.outcomes.get(LegalOutcome.ADDITIONAL_EVIDENCE_REQUIRED.value, 0) > 0
        )


async def build_check_input(
    db: AsyncSession, inspection: Inspection, product: Product
) -> tuple[CheckInput, SelectionContext, int]:
    """Assemble engine inputs from reviewed candidates and the inspection record.

    Returns ``(check_input, selection_context, unreviewed_count)``.
    """
    candidates = list(
        await db.scalars(
            select(DeclarationCandidate).where(DeclarationCandidate.inspection_id == inspection.id)
        )
    )

    values: dict[str, dict[str, Any]] = {}
    label_only: dict[str, bool] = {}
    unreviewed = 0

    for candidate in candidates:
        declaration = candidate.declaration_type
        if candidate.review_state == ReviewState.PENDING:
            unreviewed += 1
            continue
        if candidate.review_state in {ReviewState.REJECTED, ReviewState.NOT_APPLICABLE}:
            continue

        effective = candidate.effective_value
        if effective:
            # Prefer the highest-confidence confirmed reading when a declaration
            # appears more than once.
            existing = values.get(declaration)
            if existing is None or (candidate.machine_confidence or 0) >= existing.get(
                "_confidence", 0
            ):
                stored = dict(effective)
                stored["_confidence"] = candidate.machine_confidence or 0
                stored["_candidate_id"] = str(candidate.id)
                values[declaration] = stored
        elif candidate.machine_state in {
            MachineState.UNREADABLE,
            MachineState.AMBIGUOUS,
            MachineState.PROCESSING_FAILED,
        }:
            label_only[declaration] = True

    faces = list(
        await db.scalars(
            select(InspectionFace).where(InspectionFace.inspection_id == inspection.id)
        )
    )
    outstanding = [
        face.face
        for face in faces
        if face.is_required
        and face.capture_state
        in {FaceCaptureState.REQUIRED, FaceCaptureState.ADDITIONAL_IMAGE_REQUIRED}
    ]
    evidence_complete = not outstanding

    quantity = values.get(str(DeclarationType.NET_QUANTITY))
    quantity_base: Decimal | None = None
    quantity_kind: str | None = None
    if quantity:
        try:
            quantity_base = Decimal(str(quantity["base_amount"]))
            quantity_kind = str(quantity.get("dimension"))
        except (KeyError, TypeError, ValueError):
            quantity_base = None
    elif product.net_quantity_base is not None:
        # Fall back to the quantity recorded on the product record.
        quantity_base = Decimal(str(product.net_quantity_base))
        quantity_kind = product.quantity_kind

    context_payload: dict[str, Any] = {
        "is_imported": product.is_imported,
        "is_multipiece": product.is_multipiece,
        "commodity_category": product.commodity_category,
        "package_type": product.package_type,
        "character_height_measurements": (inspection.context or {}).get(
            "character_height_measurements", {}
        ),
        "mrp_context_text": (inspection.context or {}).get("mrp_context_text", ""),
    }

    check_input = CheckInput(
        values=values,
        label_seen_without_value=label_only,
        context=context_payload,
        evidence_complete=evidence_complete,
        missing_faces=outstanding,
    )
    selection_context = SelectionContext(
        inspection_date=inspection.inspection_date,
        commodity_category=product.commodity_category,
        package_type=product.package_type,
        quantity_kind=quantity_kind,
        quantity_base=quantity_base,
        is_imported=product.is_imported,
        is_ecommerce=inspection.source == "ecommerce_listing",
        is_multipiece=product.is_multipiece,
        claimed_exceptions=tuple((inspection.context or {}).get("claimed_exceptions", [])),
    )
    return check_input, selection_context, unreviewed


async def load_effective_rules(db: AsyncSession, on: date) -> list[RuleVersion]:
    """Every approved or active rule version that could apply on a date.

    The date filter is applied in SQL; the finer scope tests happen in Python where
    they can be explained.
    """
    return list(
        await db.scalars(
            select(RuleVersion)
            .where(
                RuleVersion.status.in_([RuleStatus.APPROVED.value, RuleStatus.ACTIVE.value]),
                RuleVersion.effective_from <= on,
            )
            .order_by(RuleVersion.code.asc(), RuleVersion.version.desc())
        )
    )


async def evaluate_inspection(
    db: AsyncSession,
    *,
    inspection: Inspection,
    product: Product,
    actor_id: uuid.UUID | None = None,
) -> EngineResult:
    """Run every applicable rule and replace this inspection's current findings.

    Previous findings are marked superseded rather than deleted, so an earlier
    conclusion remains visible in the record.
    """
    check_input, selection_context, unreviewed = await build_check_input(db, inspection, product)
    candidates = await load_effective_rules(db, inspection.inspection_date)
    selected, rejected = select_applicable_rules(candidates, selection_context)

    result = EngineResult(
        findings_written=0,
        rules_considered=len(candidates),
        rules_applied=len(selected),
        selected=[item.as_dict() for item in selected],
        rejected=[item.as_dict() for item in rejected],
        unreviewed_candidates=unreviewed,
    )

    if unreviewed:
        result.notes.append(
            f"{counted(unreviewed, 'machine reading')} "
            f"{verb(unreviewed, 'is', 'are')} still awaiting officer review and did "
            "not reach the rule engine."
        )
    if not candidates:
        result.notes.append(
            "No approved rule version is in force for the inspection date, so no "
            "legal test could be applied."
        )
    if check_input.missing_faces:
        result.notes.append(
            "Required package faces outstanding: " + ", ".join(check_input.missing_faces)
        )

    # Supersede the previous run's findings.
    await db.execute(
        update(Finding)
        .where(Finding.inspection_id == inspection.id, Finding.is_current.is_(True))
        .values(is_current=False)
    )

    candidate_index = await _candidate_index(db, inspection.id)

    for applicability in selected:
        rule = applicability.rule_version
        payload = CheckInput(
            values=check_input.values,
            label_seen_without_value=check_input.label_seen_without_value,
            context=check_input.context,
            parameters=dict(rule.test_specification or {}),
            evidence_complete=check_input.evidence_complete,
            missing_faces=check_input.missing_faces,
        )
        missing_inputs = [
            str(item) for item in (rule.required_inputs or []) if not payload.has(str(item))
        ]

        outcome = run_check(rule.test_kind, payload)

        # A rule that names required inputs it did not receive can only ever be
        # undetermined, whatever the check returned.
        if missing_inputs and outcome.outcome == LegalOutcome.NON_COMPLIANT:
            declared_absent = payload.evidence_complete and not any(
                payload.label_only(item) for item in missing_inputs
            )
            if not declared_absent:
                outcome = type(outcome)(
                    outcome=LegalOutcome.UNABLE_TO_DETERMINE,
                    explanation=(
                        "This test needs "
                        + ", ".join(item.replace("_", " ") for item in missing_inputs)
                        + ", which is not established from the reviewed evidence. "
                        + outcome.explanation
                    ),
                    calculation=outcome.calculation,
                    test_inputs=outcome.test_inputs,
                    expected_value=outcome.expected_value,
                    observed_value=outcome.observed_value,
                    focus_declaration=outcome.focus_declaration,
                )

        focus = outcome.focus_declaration
        finding = Finding(
            id=uuid.uuid4(),
            inspection_id=inspection.id,
            rule_version_id=rule.id,
            candidate_id=candidate_index.get(focus) if focus else None,
            declaration_type=focus,
            outcome=outcome.outcome.value,
            explanation=outcome.explanation,
            selection_reason={
                "context": selection_context.as_dict(),
                "reasons": applicability.reasons,
                "citation": rule.citation,
                "rule_code": rule.code,
                "rule_version": rule.version,
                "effective_from": rule.effective_from.isoformat(),
                "effective_to": rule.effective_to.isoformat() if rule.effective_to else None,
                "legal_authority_confirmed": rule.legal_authority_confirmed,
            },
            test_inputs={
                **outcome.test_inputs,
                "required_inputs": list(rule.required_inputs or []),
                "missing_inputs": missing_inputs,
                "test_kind": rule.test_kind,
            },
            calculation=outcome.calculation,
            expected_value=(outcome.expected_value or "")[:240] or None,
            observed_value=(outcome.observed_value or "")[:240] or None,
            engine_version=ENGINE_VERSION,
            is_current=True,
        )
        db.add(finding)
        result.findings_written += 1
        result.outcomes[outcome.outcome.value] = result.outcomes.get(outcome.outcome.value, 0) + 1

    inspection.checks_executed_at = datetime.now(UTC)
    inspection.rules_evaluated_count = len(selected)
    await db.flush()

    logger.info(
        "rules_evaluated",
        inspection=inspection.reference,
        rules_considered=result.rules_considered,
        rules_applied=result.rules_applied,
        findings=result.findings_written,
        outcomes=result.outcomes,
    )
    return result


async def _candidate_index(db: AsyncSession, inspection_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Best reviewed candidate per declaration type, for linking findings."""
    rows = list(
        await db.scalars(
            select(DeclarationCandidate)
            .where(DeclarationCandidate.inspection_id == inspection_id)
            .order_by(DeclarationCandidate.machine_confidence.desc().nullslast())
        )
    )
    index: dict[str, uuid.UUID] = {}
    for row in rows:
        if row.declaration_type not in index and row.is_usable_for_rules:
            index[row.declaration_type] = row.id
    for row in rows:
        index.setdefault(row.declaration_type, row.id)
    return index


def summarise_outcomes(findings: list[Finding]) -> dict[str, Any]:
    """Counts and a suggested decision for the reviewer.

    The suggestion is advice, not a decision. The reviewer records the decision and
    must supply a reason, and the state machine enforces that.
    """
    counts: dict[str, int] = {}
    for finding in findings:
        outcome = finding.effective_outcome
        counts[outcome] = counts.get(outcome, 0) + 1

    if counts.get(LegalOutcome.NON_COMPLIANT.value):
        suggestion = LegalOutcome.NON_COMPLIANT.value
        failed = counts[LegalOutcome.NON_COMPLIANT.value]
        rationale = f"{counted(failed, 'test')} returned non-compliant."
    elif counts.get(LegalOutcome.ADDITIONAL_EVIDENCE_REQUIRED.value):
        suggestion = LegalOutcome.ADDITIONAL_EVIDENCE_REQUIRED.value
        rationale = "Some tests need more evidence before they can be decided."
    elif counts.get(LegalOutcome.UNABLE_TO_DETERMINE.value):
        suggestion = LegalOutcome.UNABLE_TO_DETERMINE.value
        rationale = "Some tests could not be decided from the evidence supplied."
    elif counts.get(LegalOutcome.COMPLIANT.value):
        suggestion = LegalOutcome.COMPLIANT.value
        rationale = "Every applicable test returned compliant."
    else:
        suggestion = LegalOutcome.UNABLE_TO_DETERMINE.value
        rationale = "No applicable test produced a result."

    return {
        "counts": counts,
        "suggested_outcome": suggestion,
        "rationale": rationale,
        "note": (
            "These are the deterministic tests. The decision on record is the one an "
            "authorised officer gives a reason for."
        ),
    }
