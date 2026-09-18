"""Inspection lifecycle.

State changes go through :func:`transition`, which checks four things in order:

1. the caller's version matches the stored version (optimistic locking);
2. the pair of states is a declared edge in the state machine;
3. the caller's role is permitted on that edge;
4. every guard on that edge passes.

Only then is the state written, and a ``StateTransition`` row plus an audit event are
appended. Nothing else in the codebase assigns ``Inspection.state`` directly, apart
from :func:`apply_transition_unchecked`, which exists solely for the worker to record
that analysis finished and which still writes the history rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.enums import (
    DECIDED_STATES,
    FaceCaptureState,
    InspectionState,
    PackageFace,
    ReviewState,
    Role,
)
from ..domain.states import INSPECTION_MACHINE, Transition
from ..errors import (
    ConflictError,
    GuardFailedError,
    InvalidTransitionError,
    NotFoundError,
    StaleRecordError,
    ValidationError,
)
from ..models.evidence import Evidence
from ..models.finding import DeclarationCandidate, Finding
from ..models.inspection import Inspection, InspectionFace, StateTransition
from ..models.product import Product
from ..models.report import Report
from ..observability import current_request_id, get_logger
from ..security.permissions import JurisdictionScope
from . import audit as audit_service
from . import references
from .phrasing import counted, verb

logger = get_logger(__name__)

#: Faces requested by default for a new inspection. An officer can mark any of them
#: not applicable with a reason; the checklist records that rather than hiding it.
DEFAULT_REQUIRED_FACES: tuple[PackageFace, ...] = (
    PackageFace.PRINCIPAL_DISPLAY_PANEL,
    PackageFace.DECLARATION_PANEL,
)

#: Additional faces suggested but not required.
DEFAULT_OPTIONAL_FACES: tuple[PackageFace, ...] = (
    PackageFace.BACK_PANEL,
    PackageFace.MRP_CLOSE_UP,
    PackageFace.NET_QUANTITY_CLOSE_UP,
    PackageFace.DATE_MARKING_CLOSE_UP,
    PackageFace.BARCODE_AREA,
)


@dataclass
class GuardOutcome:
    passed: bool
    reason: str | None = None


async def create_inspection(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    product: Product,
    created_by_id: uuid.UUID,
    jurisdiction_code: str,
    jurisdiction_name: str,
    inspection_date: date,
    source: str,
    premises_name: str | None = None,
    premises_address: str | None = None,
    marketplace_name: str | None = None,
    listing_url: str | None = None,
    batch_reference: str | None = None,
    complaint_id: uuid.UUID | None = None,
    extra_context: dict[str, Any] | None = None,
) -> Inspection:
    """Open an inspection in the draft state with its face checklist."""
    if inspection_date > date.today():
        raise ValidationError(
            "The inspection date cannot be in the future.",
            details={"field": "inspection_date"},
        )

    reference = await references.next_reference(db, references.INSPECTION_PREFIX)
    inspection = Inspection(
        id=uuid.uuid4(),
        reference=reference,
        product_id=product.id,
        state=InspectionState.DRAFT,
        jurisdiction_code=jurisdiction_code,
        jurisdiction_name=jurisdiction_name,
        created_by_id=created_by_id,
        assigned_officer_id=created_by_id,
        source=source,
        inspection_date=inspection_date,
        premises_name=premises_name,
        premises_address=premises_address,
        marketplace_name=marketplace_name,
        listing_url=listing_url,
        batch_reference=batch_reference,
        complaint_id=complaint_id,
        context=extra_context or {},
    )
    db.add(inspection)
    await db.flush()

    for face in DEFAULT_REQUIRED_FACES:
        db.add(
            InspectionFace(
                id=uuid.uuid4(),
                inspection_id=inspection.id,
                face=face,
                capture_state=FaceCaptureState.REQUIRED,
                is_required=True,
            )
        )
    for face in DEFAULT_OPTIONAL_FACES:
        db.add(
            InspectionFace(
                id=uuid.uuid4(),
                inspection_id=inspection.id,
                face=face,
                capture_state=FaceCaptureState.REQUIRED,
                is_required=False,
            )
        )
    await db.flush()

    await _record_history(
        db,
        inspection=inspection,
        from_state=None,
        to_state=InspectionState.DRAFT,
        actor_id=created_by_id,
        actor_role=context.actor_role,
        reason="Inspection opened.",
    )
    await audit_service.record(
        db,
        context,
        action="inspection.created",
        entity_type="inspection",
        entity_id=inspection.id,
        entity_version=inspection.version,
        new_values={
            "reference": inspection.reference,
            "product_id": str(product.id),
            "jurisdiction_code": jurisdiction_code,
            "inspection_date": inspection_date.isoformat(),
            "source": source,
        },
    )
    return inspection


async def get_for_actor(
    db: AsyncSession, inspection_id: uuid.UUID, scope: JurisdictionScope
) -> Inspection:
    """Load an inspection, enforcing jurisdiction.

    An out-of-scope record raises ``NotFoundError``, so a guessed identifier cannot
    confirm that a record exists.
    """
    inspection = await db.get(Inspection, inspection_id)
    if inspection is None:
        raise NotFoundError("That inspection was not found.")
    scope.require(inspection.jurisdiction_code)
    return inspection


def check_version(inspection: Inspection, expected_version: int) -> None:
    if inspection.version != expected_version:
        raise StaleRecordError(
            "This inspection changed after you loaded it. Reload and try again.",
            details={"your_version": expected_version, "current_version": inspection.version},
        )


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------
async def evaluate_guard(db: AsyncSession, guard: str, inspection: Inspection) -> GuardOutcome:
    """Evaluate one named guard against the current record."""
    if guard == "product_identified":
        product = await db.get(Product, inspection.product_id)
        if product is None:
            return GuardOutcome(False, "No product record is attached to this inspection.")
        if not product.brand or not product.name:
            return GuardOutcome(
                False, "The product needs a brand and a name before capture begins."
            )
        return GuardOutcome(True)

    if guard == "has_evidence":
        count = await db.scalar(
            select(func.count())
            .select_from(Evidence)
            .where(Evidence.inspection_id == inspection.id)
        )
        if not count:
            return GuardOutcome(False, "No evidence has been uploaded yet.")
        return GuardOutcome(True)

    if guard == "all_candidates_reviewed":
        pending = await db.scalar(
            select(func.count())
            .select_from(DeclarationCandidate)
            .where(
                DeclarationCandidate.inspection_id == inspection.id,
                DeclarationCandidate.review_state == ReviewState.PENDING,
            )
        )
        if pending:
            return GuardOutcome(
                False,
                f"{counted(pending, 'machine reading')} still "
                f"{verb(pending, 'needs', 'need')} to be confirmed, corrected or "
                "rejected.",
            )
        return GuardOutcome(True)

    if guard == "all_checks_executed":
        if inspection.checks_executed_at is None:
            return GuardOutcome(
                False,
                "The legal checks have not been run since the last change. Run the "
                "checks before recording a decision.",
            )
        count = await db.scalar(
            select(func.count())
            .select_from(Finding)
            .where(Finding.inspection_id == inspection.id, Finding.is_current.is_(True))
        )
        if not count:
            return GuardOutcome(
                False,
                "No finding has been produced. Either no approved rule applies to this "
                "package, or the checks have not been run.",
            )
        return GuardOutcome(True)

    if guard == "decision_recorded":
        if inspection.decision is None or not inspection.decision_note:
            return GuardOutcome(
                False, "A decision and a reasoned note must be recorded before a report is issued."
            )
        return GuardOutcome(True)

    if guard == "report_issued":
        count = await db.scalar(
            select(func.count()).select_from(Report).where(Report.inspection_id == inspection.id)
        )
        if not count:
            return GuardOutcome(False, "No report has been issued for this inspection.")
        return GuardOutcome(True)

    # An unknown guard blocks rather than passes: failing closed is the only safe
    # behaviour when the rule cannot be evaluated.
    return GuardOutcome(False, f"The precondition {guard!r} could not be evaluated.")


async def available_transitions(
    db: AsyncSession, inspection: Inspection, role: Role
) -> list[dict[str, Any]]:
    """Transitions the caller could attempt, with any blocking reasons.

    The interface uses this to show why an action is unavailable rather than simply
    hiding the button.
    """
    results: list[dict[str, Any]] = []
    for transition in INSPECTION_MACHINE.allowed_for(inspection.state, role):
        blocked: list[str] = []
        for guard in transition.guards:
            outcome = await evaluate_guard(db, guard, inspection)
            if not outcome.passed and outcome.reason:
                blocked.append(outcome.reason)
        results.append(
            {
                "target_state": transition.target,
                "label": transition.label,
                "reason_required": transition.reason_required,
                "blocked_by": blocked,
            }
        )
    return results


async def transition(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    inspection: Inspection,
    target_state: str,
    role: Role,
    actor_id: uuid.UUID,
    expected_version: int,
    reason: str | None = None,
) -> Inspection:
    """Move an inspection to a new state, enforcing every rule."""
    check_version(inspection, expected_version)

    edge: Transition | None = INSPECTION_MACHINE.find(inspection.state, target_state)
    if edge is None:
        allowed = INSPECTION_MACHINE.targets_from(inspection.state)
        raise InvalidTransitionError(
            f"An inspection in state {inspection.state!r} cannot move to " f"{target_state!r}.",
            details={"current_state": inspection.state, "allowed_states": list(allowed)},
        )

    if role not in edge.roles:
        from ..errors import PermissionDeniedError

        raise PermissionDeniedError(
            f"Your role cannot {edge.label.lower()}.",
            details={
                "required_roles": sorted(item.value for item in edge.roles),
                "your_role": role.value,
            },
        )

    if edge.reason_required and not (reason and reason.strip()):
        raise ValidationError(
            f"A reason is required to {edge.label.lower()}.", details={"field": "reason"}
        )

    for guard in edge.guards:
        outcome = await evaluate_guard(db, guard, inspection)
        if not outcome.passed:
            raise GuardFailedError(
                outcome.reason or "A required step has not been completed.",
                details={"guard": guard, "target_state": target_state},
            )

    previous_state = inspection.state
    inspection.state = target_state
    if target_state == InspectionState.ARCHIVED:
        inspection.archived_at = datetime.now(UTC)
    await db.flush()

    await _record_history(
        db,
        inspection=inspection,
        from_state=previous_state,
        to_state=target_state,
        actor_id=actor_id,
        actor_role=role.value,
        reason=reason,
    )
    await audit_service.record(
        db,
        context,
        action="inspection.state_changed",
        entity_type="inspection",
        entity_id=inspection.id,
        entity_version=inspection.version,
        old_values={"state": previous_state},
        new_values={"state": target_state},
        reason=reason,
    )
    logger.info(
        "inspection_state_changed",
        inspection=inspection.reference,
        from_state=previous_state,
        to_state=target_state,
        actor_role=role.value,
    )
    return inspection


async def record_decision(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    inspection: Inspection,
    decision: str,
    note: str,
    role: Role,
    actor_id: uuid.UUID,
    expected_version: int,
) -> Inspection:
    """Record the reasoned decision and move to the matching state.

    The decision and the state change happen together: an inspection cannot sit in a
    decided state without a note, and cannot carry a note without the state.
    """
    if decision not in {state.value for state in DECIDED_STATES}:
        raise ValidationError(
            "The decision must be compliant, violation found, or unable to determine.",
            details={"field": "decision"},
        )
    if len(note.strip()) < 20:
        raise ValidationError(
            "Give a reason of at least 20 characters explaining the decision.",
            details={"field": "note"},
        )

    inspection.decision = decision
    inspection.decision_note = note.strip()
    inspection.decided_by_id = actor_id
    inspection.decided_at = datetime.now(UTC)
    inspection.reviewer_id = actor_id

    return await transition(
        db,
        context,
        inspection=inspection,
        target_state=decision,
        role=role,
        actor_id=actor_id,
        expected_version=expected_version,
        reason=note.strip(),
    )


async def apply_transition_unchecked(
    db: AsyncSession,
    *,
    inspection: Inspection,
    target: str,
    reason: str,
    actor_id: uuid.UUID | None,
    actor_role: str,
) -> None:
    """System transition used by the worker.

    Still validated against the state machine and still written to history; it
    simply has no human role or version to check, because the actor is the pipeline
    reporting that analysis finished.
    """
    edge = INSPECTION_MACHINE.find(inspection.state, target)
    if edge is None:
        logger.info(
            "system_transition_skipped",
            inspection=inspection.reference,
            from_state=inspection.state,
            to_state=target,
        )
        return

    previous_state = inspection.state
    inspection.state = target
    await db.flush()
    await _record_history(
        db,
        inspection=inspection,
        from_state=previous_state,
        to_state=target,
        actor_id=actor_id,
        actor_role=actor_role,
        reason=reason,
    )
    await audit_service.record(
        db,
        audit_service.AuditContext(actor_role=actor_role),
        action="inspection.state_changed",
        entity_type="inspection",
        entity_id=inspection.id,
        entity_version=inspection.version,
        old_values={"state": previous_state},
        new_values={"state": target},
        reason=reason,
    )


async def _record_history(
    db: AsyncSession,
    *,
    inspection: Inspection,
    from_state: str | None,
    to_state: str,
    actor_id: uuid.UUID | None,
    actor_role: str | None,
    reason: str | None,
) -> None:
    db.add(
        StateTransition(
            id=uuid.uuid4(),
            entity_type="inspection",
            entity_id=inspection.id,
            from_state=from_state,
            to_state=to_state,
            actor_id=actor_id,
            actor_role=actor_role,
            jurisdiction_code=inspection.jurisdiction_code,
            reason=reason,
            request_id=current_request_id(),
            entity_version=inspection.version,
            occurred_at=datetime.now(UTC),
        )
    )
    await db.flush()


async def set_face_state(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    inspection: Inspection,
    face: str,
    capture_state: str,
    reason: str | None,
    actor_id: uuid.UUID,
) -> InspectionFace:
    """Record why an expected face was not captured.

    A face marked absent, not applicable or impossible to capture must carry a
    reason. That reason is what allows a later finding to say "the panel was not
    photographed" rather than "the declaration is missing".
    """
    row = await db.scalar(
        select(InspectionFace).where(
            InspectionFace.inspection_id == inspection.id, InspectionFace.face == face
        )
    )
    if row is None:
        row = InspectionFace(
            id=uuid.uuid4(),
            inspection_id=inspection.id,
            face=face,
            capture_state=FaceCaptureState.REQUIRED,
            is_required=False,
        )
        db.add(row)
        await db.flush()

    needs_reason = capture_state in {
        FaceCaptureState.ABSENT,
        FaceCaptureState.NOT_APPLICABLE,
        FaceCaptureState.UNABLE_TO_CAPTURE,
        FaceCaptureState.ADDITIONAL_IMAGE_REQUIRED,
    }
    if needs_reason and not (reason and reason.strip()):
        raise ValidationError(
            f"Give a reason for recording this face as {capture_state.replace('_', ' ')}.",
            details={"field": "reason"},
        )

    previous = row.capture_state
    row.capture_state = capture_state
    row.reason = reason.strip() if reason else None
    row.updated_by_id = actor_id
    await db.flush()

    await audit_service.record(
        db,
        context,
        action="inspection.face_state_changed",
        entity_type="inspection_face",
        entity_id=row.id,
        old_values={"capture_state": previous},
        new_values={"capture_state": capture_state, "face": face},
        reason=reason,
    )
    return row


async def coverage_summary(db: AsyncSession, inspection_id: uuid.UUID) -> dict[str, Any]:
    """What has been captured and what is still outstanding."""
    faces = list(
        await db.scalars(
            select(InspectionFace).where(InspectionFace.inspection_id == inspection_id)
        )
    )
    evidence_counts: dict[str, int] = {}
    for face_value in await db.scalars(
        select(Evidence.face).where(Evidence.inspection_id == inspection_id)
    ):
        evidence_counts[face_value] = evidence_counts.get(face_value, 0) + 1

    outstanding = [
        row.face
        for row in faces
        if row.is_required
        and row.capture_state
        in {FaceCaptureState.REQUIRED, FaceCaptureState.ADDITIONAL_IMAGE_REQUIRED}
    ]
    return {
        "faces": [
            {
                "face": row.face,
                "capture_state": row.capture_state,
                "is_required": row.is_required,
                "reason": row.reason,
                "evidence_count": evidence_counts.get(row.face, 0),
            }
            for row in faces
        ],
        "required_outstanding": outstanding,
        "complete": not outstanding,
    }


async def ensure_not_frozen(inspection: Inspection) -> None:
    if inspection.is_frozen:
        raise ConflictError(
            "This inspection is closed to changes because a report has been issued.",
            code="inspection_frozen",
            details={"state": inspection.state},
        )
