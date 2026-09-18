"""Rule governance: maker-checker.

Status path::

    draft ──submit──▶ under_review ──approve──▶ approved ──activate──▶ active
                          │                                              │
                          └──request changes──▶ changes_required          └──withdraw──▶ withdrawn

Two constraints are enforced here and cannot be bypassed:

1. **Separation of duties.** The account that authored or last edited a version
   cannot approve it. ``ALLOW_SELF_APPROVAL`` exists only for isolated tests and is
   refused in production by ``Settings.assert_production_ready``.

2. **Tested before approved.** A version cannot be approved until a simulator run
   covering every mandatory scenario has passed. The engine will not apply an
   untested interpretation to a real package.

Approval here means "an authorised rule administrator has reviewed this
interpretation in this workspace". It is not a statement that the interpretation has
been endorsed by any statutory authority; that is recorded separately in
``legal_authority_confirmed``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...domain.enums import Role, RuleStatus
from ...errors import ConflictError, GuardFailedError, PermissionDeniedError, ValidationError
from ...models.rule import RuleReview, RuleTestRun, RuleVersion
from ...observability import get_logger
from .. import audit as audit_service
from .checks import available_checks
from .simulator import Scenario, boundary_scenarios, default_scenarios, required_kinds_for, simulate

logger = get_logger(__name__)

#: Allowed status transitions and the decision label recorded for each.
TRANSITIONS: dict[tuple[str, str], str] = {
    (RuleStatus.DRAFT, RuleStatus.UNDER_REVIEW): "submitted",
    (RuleStatus.CHANGES_REQUIRED, RuleStatus.UNDER_REVIEW): "submitted",
    (RuleStatus.UNDER_REVIEW, RuleStatus.CHANGES_REQUIRED): "changes_required",
    (RuleStatus.UNDER_REVIEW, RuleStatus.APPROVED): "approved",
    (RuleStatus.APPROVED, RuleStatus.ACTIVE): "activated",
    (RuleStatus.APPROVED, RuleStatus.WITHDRAWN): "withdrawn",
    (RuleStatus.ACTIVE, RuleStatus.WITHDRAWN): "withdrawn",
}

#: Statuses in which the rule's content may still be edited.
EDITABLE_STATUSES = frozenset({RuleStatus.DRAFT.value, RuleStatus.CHANGES_REQUIRED.value})


@dataclass
class GovernanceOutcome:
    rule_version: RuleVersion
    decision: str
    note: str | None


def assert_test_kind_supported(test_kind: str) -> None:
    if test_kind not in available_checks():
        raise ValidationError(
            f"{test_kind!r} is not a test this system implements. "
            f"Available tests: {', '.join(available_checks())}.",
            details={"field": "test_kind", "available": list(available_checks())},
        )


def assert_editable(rule: RuleVersion) -> None:
    if rule.status not in EDITABLE_STATUSES:
        raise ConflictError(
            f"A rule version with status {rule.status!r} cannot be edited. "
            "Create a new version instead.",
            code="rule_not_editable",
            details={"status": rule.status, "editable_statuses": sorted(EDITABLE_STATUSES)},
        )


async def latest_test_run(db: AsyncSession, rule_version_id: uuid.UUID) -> RuleTestRun | None:
    return await db.scalar(
        select(RuleTestRun)
        .where(RuleTestRun.rule_version_id == rule_version_id)
        .order_by(RuleTestRun.run_at.desc())
        .limit(1)
    )


async def record_test_run(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    rule: RuleVersion,
    scenarios: list[Scenario],
    run_by_id: uuid.UUID | None,
    include_generated: bool = True,
) -> RuleTestRun:
    """Run the simulator and store the result.

    Generated date-window and boundary scenarios are added to whatever the author
    supplied, so the mechanical cases cannot be forgotten.
    """
    combined = list(scenarios)
    if include_generated:
        combined.extend(default_scenarios(rule))
        compliant = next((item for item in scenarios if item.kind == "compliant"), None)
        if compliant is not None:
            combined.extend(boundary_scenarios(rule, compliant))

    report = simulate(rule, combined)
    run = RuleTestRun(
        id=uuid.uuid4(),
        rule_version_id=rule.id,
        scenarios=[item.as_dict() for item in report.results],
        passed_count=report.passed_count,
        failed_count=report.failed_count,
        all_passed=report.all_passed,
        covered_scenario_kinds=report.covered_kinds,
        run_by_id=run_by_id,
        run_at=datetime.now(UTC),
    )
    db.add(run)

    rule.test_results = {
        "passed_count": report.passed_count,
        "failed_count": report.failed_count,
        "all_passed": report.all_passed,
        "covered_kinds": report.covered_kinds,
        "required_kinds": report.required_kinds,
        "missing_kinds": report.missing_kinds,
        "approval_ready": report.approval_ready,
        "run_at": run.run_at.isoformat(),
    }
    await db.flush()

    await audit_service.record(
        db,
        context,
        action="rule.simulated",
        entity_type="rule_version",
        entity_id=rule.id,
        new_values={
            "passed": report.passed_count,
            "failed": report.failed_count,
            "approval_ready": report.approval_ready,
            "missing_kinds": report.missing_kinds,
        },
    )
    return run


async def change_status(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    rule: RuleVersion,
    target_status: str,
    actor_id: uuid.UUID,
    actor_role: Role,
    note: str | None,
    expected_version: int,
) -> GovernanceOutcome:
    """Move a rule version through the governance path."""
    if rule.record_version != expected_version:
        from ...errors import StaleRecordError

        raise StaleRecordError(
            "This rule version changed after you loaded it. Reload and try again.",
            details={
                "your_version": expected_version,
                "current_version": rule.record_version,
            },
        )

    decision = TRANSITIONS.get((rule.status, target_status))
    if decision is None:
        allowed = [target for (source, target) in TRANSITIONS if source == rule.status]
        raise ConflictError(
            f"A rule version with status {rule.status!r} cannot move to " f"{target_status!r}.",
            code="invalid_rule_transition",
            details={"current_status": rule.status, "allowed_statuses": allowed},
        )

    settings = get_settings()
    now = datetime.now(UTC)

    if target_status == RuleStatus.APPROVED:
        if not settings.allow_self_approval and rule.author_id == actor_id:
            raise PermissionDeniedError(
                "The author of a rule version cannot approve it. Separation of duties "
                "requires a second rule administrator.",
                code="self_approval_refused",
                details={"author_id": str(rule.author_id) if rule.author_id else None},
            )
        run = await latest_test_run(db, rule.id)
        if run is None:
            raise GuardFailedError(
                "Run the rule simulator before approving this version.",
                code="simulation_required",
            )
        if not run.all_passed:
            raise GuardFailedError(
                f"{run.failed_count} simulator scenario(s) failed. Fix the rule or the "
                "expected outcomes before approving.",
                code="simulation_failed",
                details={"failed_count": run.failed_count},
            )
        missing = [
            kind
            for kind in required_kinds_for(rule)
            if kind not in (run.covered_scenario_kinds or [])
        ]
        if missing:
            raise GuardFailedError(
                "The simulator has not exercised every required case. Missing: "
                + ", ".join(missing),
                code="simulation_coverage_incomplete",
                details={"missing_scenario_kinds": missing},
            )
        rule.approver_id = actor_id
        rule.approved_at = now

    if target_status == RuleStatus.UNDER_REVIEW:
        if not rule.interpretation_note or len(rule.interpretation_note.strip()) < 20:
            raise ValidationError(
                "Record how the legal text was interpreted before submitting for "
                "review. This is what a reviewer checks.",
                details={"field": "interpretation_note"},
            )
        rule.submitted_at = now

    if target_status == RuleStatus.CHANGES_REQUIRED:
        if not note or len(note.strip()) < 10:
            raise ValidationError("Say what needs to change.", details={"field": "note"})
        rule.reviewer_id = actor_id
        rule.reviewed_at = now

    if target_status == RuleStatus.ACTIVE:
        # Activating supersedes any other active version of the same code whose
        # window has been overtaken.
        superseded = await db.execute(
            update(RuleVersion)
            .where(
                RuleVersion.code == rule.code,
                RuleVersion.id != rule.id,
                RuleVersion.status == RuleStatus.ACTIVE.value,
                RuleVersion.effective_from < rule.effective_from,
            )
            .values(status=RuleStatus.SUPERSEDED.value)
        )
        if superseded.rowcount:
            logger.info(
                "rule_versions_superseded",
                code=rule.code,
                count=int(superseded.rowcount),
                by_version=rule.version,
            )

    if target_status == RuleStatus.WITHDRAWN:
        if not note or len(note.strip()) < 10:
            raise ValidationError(
                "Give a reason for withdrawing this rule version.",
                details={"field": "note"},
            )
        rule.withdrawn_at = now
        rule.withdrawn_reason = note.strip()

    previous_status = rule.status
    rule.status = target_status
    await db.flush()

    db.add(
        RuleReview(
            id=uuid.uuid4(),
            rule_version_id=rule.id,
            decision=decision,
            note=note.strip() if note else None,
            actor_id=actor_id,
            actor_role=actor_role.value,
            recorded_at=now,
        )
    )
    await db.flush()

    await audit_service.record(
        db,
        context,
        action=f"rule.{decision}",
        entity_type="rule_version",
        entity_id=rule.id,
        entity_version=rule.record_version,
        old_values={"status": previous_status},
        new_values={
            "status": target_status,
            "code": rule.code,
            "version": rule.version,
        },
        reason=note,
    )
    logger.info(
        "rule_status_changed",
        code=rule.code,
        version=rule.version,
        from_status=previous_status,
        to_status=target_status,
        actor_role=actor_role.value,
    )
    return GovernanceOutcome(rule_version=rule, decision=decision, note=note)


async def confirm_legal_authority(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    rule: RuleVersion,
    confirmed: bool,
    note: str,
    actor_id: uuid.UUID,
) -> RuleVersion:
    """Record that a qualified authority has (or has not) endorsed the interpretation.

    Separate from approval on purpose. Workspace approval is an operational control;
    this flag is a statement about legal endorsement, and only the latter should ever
    be presented to the public as settled.
    """
    if len(note.strip()) < 20:
        raise ValidationError(
            "Record who confirmed the interpretation and on what basis, in at least "
            "20 characters.",
            details={"field": "note"},
        )

    previous = rule.legal_authority_confirmed
    rule.legal_authority_confirmed = confirmed
    rule.legal_authority_note = note.strip()
    await db.flush()

    await audit_service.record(
        db,
        context,
        action="rule.legal_authority_recorded",
        entity_type="rule_version",
        entity_id=rule.id,
        entity_version=rule.record_version,
        old_values={"legal_authority_confirmed": previous},
        new_values={"legal_authority_confirmed": confirmed},
        reason=note,
    )
    return rule


def governance_summary(rule: RuleVersion, run: RuleTestRun | None) -> dict[str, Any]:
    """What still stands between this version and approval."""
    blockers: list[str] = []
    if rule.status == RuleStatus.DRAFT:
        blockers.append("Submit the version for review.")
    if rule.status in EDITABLE_STATUSES and (
        not rule.interpretation_note or len(rule.interpretation_note.strip()) < 20
    ):
        blockers.append("Record the interpretation note.")
    if run is None:
        blockers.append("Run the simulator.")
    else:
        if not run.all_passed:
            blockers.append(f"{run.failed_count} simulator scenario(s) fail.")
        missing = [
            kind
            for kind in required_kinds_for(rule)
            if kind not in (run.covered_scenario_kinds or [])
        ]
        if missing:
            blockers.append("Cover these cases: " + ", ".join(missing))
    if rule.status == RuleStatus.UNDER_REVIEW:
        blockers.append("A rule administrator other than the author must approve this version.")

    return {
        "status": rule.status,
        "editable": rule.status in EDITABLE_STATUSES,
        "effective_for_inspections": rule.is_effective_for_rules,
        "author_id": str(rule.author_id) if rule.author_id else None,
        "approver_id": str(rule.approver_id) if rule.approver_id else None,
        "legal_authority_confirmed": rule.legal_authority_confirmed,
        "legal_authority_note": rule.legal_authority_note,
        "allowed_next_statuses": [
            target for (source, target) in TRANSITIONS if source == rule.status
        ],
        "approval_blockers": blockers,
        "test_run": (
            {
                "all_passed": run.all_passed,
                "passed_count": run.passed_count,
                "failed_count": run.failed_count,
                "covered_kinds": run.covered_scenario_kinds,
                "required_kinds": required_kinds_for(rule),
                "run_at": run.run_at,
            }
            if run
            else None
        ),
    }
