"""Cases and notices.

A case is opened from an inspection that already has a report. A notice is prepared
from a versioned template, and at the moment it is issued the rendered text is stored
on the notice row and frozen. A database trigger then refuses any change to that text,
so publishing a new template version cannot alter a notice that was already served.

Nothing here decides a penalty. Maanak records that a notice was prepared, issued,
delivered and answered. The decision to take enforcement action is made by an
authorised officer outside this system.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.enums import CaseState, InspectionState, NoticeType, Role
from ..domain.states import CASE_MACHINE
from ..errors import (
    ConflictError,
    GuardFailedError,
    InvalidTransitionError,
    NotFoundError,
    PermissionDeniedError,
    StaleRecordError,
    ValidationError,
)
from ..models.case_file import Case, CaseEvent, Notice, NoticeTemplate
from ..models.inspection import Inspection, StateTransition
from ..models.report import Report
from ..models.rule import RuleVersion
from ..observability import current_request_id, get_logger
from ..security.permissions import JurisdictionScope
from . import audit as audit_service
from . import references
from .canonical import sha256_bytes

logger = get_logger(__name__)

#: Shipped template. Placeholders are substituted at issue time and the result frozen.
DEFAULT_TEMPLATE_CODE = "SHOW-CAUSE-GENERIC"
DEFAULT_TEMPLATE_VERSION = 1
DEFAULT_TEMPLATE_BODY = """\
To: {respondent_name}
{respondent_address}

Reference: {case_reference}
Inspection: {inspection_reference}
Report: {report_reference}
Date of issue: {issue_date}

Subject: {subject}

An inspection of the packaged commodity described below was carried out on
{inspection_date} at {premises}. The inspection record and its supporting evidence
are held under the reference above.

Product: {product_description}

The following matters were recorded:

{findings_block}

The provisions relied on are:

{legal_basis_block}

You are required to show cause, within {response_days} days of the date of this
notice, why action should not be taken in respect of the matters set out above. A
written response may be sent to the office issuing this notice, quoting the case
reference.

{signature_block}

This notice was prepared using Maanak, a record-keeping system. Maanak does not
itself determine any penalty or liability.
"""


async def get_for_actor(db: AsyncSession, case_id: uuid.UUID, scope: JurisdictionScope) -> Case:
    case = await db.get(Case, case_id)
    if case is None:
        raise NotFoundError("That case was not found.")
    scope.require(case.jurisdiction_code)
    return case


async def ensure_default_template(db: AsyncSession) -> NoticeTemplate:
    """Insert the shipped template if it is not present."""
    existing = await db.scalar(
        select(NoticeTemplate).where(
            NoticeTemplate.code == DEFAULT_TEMPLATE_CODE,
            NoticeTemplate.version == DEFAULT_TEMPLATE_VERSION,
        )
    )
    if existing is not None:
        return existing

    template = NoticeTemplate(
        id=uuid.uuid4(),
        code=DEFAULT_TEMPLATE_CODE,
        version=DEFAULT_TEMPLATE_VERSION,
        notice_type=NoticeType.SHOW_CAUSE,
        title="Show cause notice (generic)",
        body=DEFAULT_TEMPLATE_BODY,
        placeholders=[
            "respondent_name",
            "respondent_address",
            "case_reference",
            "inspection_reference",
            "report_reference",
            "issue_date",
            "subject",
            "inspection_date",
            "premises",
            "product_description",
            "findings_block",
            "legal_basis_block",
            "response_days",
            "signature_block",
        ],
        is_active=True,
    )
    db.add(template)
    await db.flush()
    return template


async def open_case(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    inspection: Inspection,
    opened_by_id: uuid.UUID,
    respondent_name: str,
    respondent_role: str | None,
    respondent_address: str | None,
    respondent_email: str | None,
    respondent_phone: str | None,
    subject: str,
) -> Case:
    """Open a case against a responsible party for a reported inspection."""
    report = await db.scalar(
        select(Report)
        .where(Report.inspection_id == inspection.id, Report.state == "issued")
        .order_by(Report.revision.desc())
        .limit(1)
    )
    if report is None:
        raise GuardFailedError(
            "Issue a report before opening a case. A case must rest on a report that "
            "records the findings and their evidence.",
            code="report_required",
        )

    existing = await db.scalar(
        select(func.count())
        .select_from(Case)
        .where(Case.inspection_id == inspection.id, Case.state.not_in([CaseState.CLOSED.value]))
    )
    if existing:
        raise ConflictError(
            "An open case already exists for this inspection.", code="case_already_open"
        )

    # Collect the provisions relied on from the findings that were not compliant.
    from ..models.finding import Finding

    findings = list(
        await db.scalars(
            select(Finding).where(
                Finding.inspection_id == inspection.id, Finding.is_current.is_(True)
            )
        )
    )
    adverse = [item for item in findings if item.effective_outcome == "non_compliant"]
    if not adverse:
        raise GuardFailedError(
            "No finding on this inspection records non-compliance, so there is nothing "
            "to put to a respondent.",
            code="no_adverse_finding",
        )

    legal_basis: list[dict[str, Any]] = []
    rule_ids: list[str] = []
    for finding in adverse:
        rule = await db.get(RuleVersion, finding.rule_version_id)
        if rule is None:
            continue
        rule_ids.append(str(rule.id))
        legal_basis.append(
            {
                "rule_code": rule.code,
                "rule_version": rule.version,
                "citation": rule.citation,
                "title": rule.title,
                "legal_authority_confirmed": rule.legal_authority_confirmed,
            }
        )

    reference = await references.next_reference(db, references.CASE_PREFIX)
    case = Case(
        id=uuid.uuid4(),
        reference=reference,
        inspection_id=inspection.id,
        report_id=report.id,
        state=CaseState.DRAFT,
        jurisdiction_code=inspection.jurisdiction_code,
        jurisdiction_name=inspection.jurisdiction_name,
        respondent_name=respondent_name.strip(),
        respondent_role=respondent_role,
        respondent_address=respondent_address,
        respondent_email=(respondent_email or "").strip().lower() or None,
        respondent_phone=respondent_phone,
        subject=subject.strip(),
        legal_basis=legal_basis,
        rule_version_ids=rule_ids,
        opened_by_id=opened_by_id,
        assigned_officer_id=opened_by_id,
    )
    db.add(case)
    await db.flush()

    db.add(
        StateTransition(
            id=uuid.uuid4(),
            entity_type="case",
            entity_id=case.id,
            from_state=None,
            to_state=CaseState.DRAFT,
            actor_id=opened_by_id,
            actor_role=context.actor_role,
            jurisdiction_code=case.jurisdiction_code,
            reason="Case opened.",
            request_id=current_request_id(),
            entity_version=case.version,
            occurred_at=datetime.now(UTC),
        )
    )

    from .inspections import apply_transition_unchecked

    await apply_transition_unchecked(
        db,
        inspection=inspection,
        target=InspectionState.CASE_OPENED,
        reason=f"Case {reference} opened.",
        actor_id=opened_by_id,
        actor_role=context.actor_role or "system",
    )

    await audit_service.record(
        db,
        context,
        action="case.opened",
        entity_type="case",
        entity_id=case.id,
        new_values={
            "reference": reference,
            "inspection": inspection.reference,
            "report": report.reference,
            "respondent_name": case.respondent_name,
            "adverse_findings": len(adverse),
        },
    )
    logger.info("case_opened", case=reference, inspection=inspection.reference)
    return case


def render_notice_body(
    *,
    template: NoticeTemplate,
    case: Case,
    inspection: Inspection,
    report: Report,
    findings: list[dict[str, Any]],
    response_days: int,
    signature_block: str,
) -> str:
    """Substitute placeholders. Missing values become an explicit marker.

    A blank in a served notice is a defect, so an absent value is written as
    ``[not recorded]`` rather than silently vanishing.
    """
    snapshot = report.snapshot or {}
    product = snapshot.get("product", {})
    product_description = " ".join(
        part
        for part in (
            product.get("brand"),
            product.get("name"),
            f"({product.get('declared_net_quantity')} {product.get('declared_net_quantity_unit')})"
            if product.get("declared_net_quantity")
            else None,
        )
        if part
    )

    findings_block = (
        "\n\n".join(
            f"{index}. {item['explanation']}"
            + (
                f"\n   Expected: {item['expected_value']}; observed: {item['observed_value']}."
                if item.get("expected_value") or item.get("observed_value")
                else ""
            )
            for index, item in enumerate(findings, start=1)
        )
        or "[not recorded]"
    )

    legal_basis_block = (
        "\n".join(
            f"- {item['citation']} ({item['rule_code']} version {item['rule_version']})"
            + (
                ""
                if item.get("legal_authority_confirmed")
                else "  [interpretation not yet confirmed by a statutory authority]"
            )
            for item in case.legal_basis or []
        )
        or "[not recorded]"
    )

    values = {
        "respondent_name": case.respondent_name or "[not recorded]",
        "respondent_address": case.respondent_address or "[not recorded]",
        "case_reference": case.reference,
        "inspection_reference": inspection.reference,
        "report_reference": report.reference,
        "issue_date": date.today().isoformat(),
        "subject": case.subject,
        "inspection_date": inspection.inspection_date.isoformat(),
        "premises": inspection.premises_name or "[not recorded]",
        "product_description": product_description or "[not recorded]",
        "findings_block": findings_block,
        "legal_basis_block": legal_basis_block,
        "response_days": str(response_days),
        "signature_block": signature_block,
    }

    missing = [name for name in template.placeholders if name not in values]
    if missing:
        raise ValidationError(
            "The template expects values this system does not supply: " + ", ".join(missing),
            code="template_placeholder_missing",
        )
    return template.body.format(**values)


async def prepare_notice(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    case: Case,
    notice_type: str,
    response_days: int,
    signature_block: str,
    prepared_by_id: uuid.UUID,
) -> Notice:
    """Render a notice and store it unissued, so it can be read before serving."""
    template = await ensure_default_template(db)
    inspection = await db.get(Inspection, case.inspection_id)
    report = await db.get(Report, case.report_id) if case.report_id else None
    if inspection is None or report is None:
        raise NotFoundError("The inspection or report for this case is missing.")

    from ..models.finding import Finding

    findings = [
        {
            "explanation": item.explanation,
            "expected_value": item.expected_value,
            "observed_value": item.observed_value,
        }
        for item in await db.scalars(
            select(Finding).where(
                Finding.inspection_id == inspection.id,
                Finding.is_current.is_(True),
                Finding.outcome == "non_compliant",
            )
        )
    ]

    body = render_notice_body(
        template=template,
        case=case,
        inspection=inspection,
        report=report,
        findings=findings,
        response_days=response_days,
        signature_block=signature_block,
    )

    reference = await references.next_reference(db, references.NOTICE_PREFIX)
    notice = Notice(
        id=uuid.uuid4(),
        case_id=case.id,
        reference=reference,
        notice_type=notice_type,
        template_code=template.code,
        template_version=template.version,
        rendered_body=body,
        rendered_at=datetime.now(UTC),
        body_sha256=sha256_bytes(body.encode("utf-8")),
        is_frozen=False,
        response_due_on=None,
    )
    db.add(notice)
    await db.flush()

    await audit_service.record(
        db,
        context,
        action="notice.prepared",
        entity_type="notice",
        entity_id=notice.id,
        new_values={
            "reference": reference,
            "case": case.reference,
            "template": f"{template.code} v{template.version}",
            "body_sha256": notice.body_sha256,
        },
    )
    return notice


async def issue_notice(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    case: Case,
    notice: Notice,
    issued_by_id: uuid.UUID,
    response_due_on: date,
    role: Role,
    expected_version: int,
) -> Notice:
    """Serve a notice and freeze its text."""
    if case.version != expected_version:
        raise StaleRecordError("This case changed after you loaded it. Reload and try again.")
    if notice.issued_at is not None:
        raise ConflictError("That notice has already been issued.", code="notice_already_issued")
    if response_due_on <= date.today():
        raise ValidationError(
            "The response deadline must be in the future.",
            details={"field": "response_due_on"},
        )

    notice.issued_at = datetime.now(UTC)
    notice.issued_by_id = issued_by_id
    notice.response_due_on = response_due_on
    notice.is_frozen = True
    await db.flush()

    await transition(
        db,
        context,
        case=case,
        target_state=CaseState.NOTICE_ISSUED,
        role=role,
        actor_id=issued_by_id,
        reason=f"Notice {notice.reference} issued.",
        expected_version=expected_version,
    )

    db.add(
        CaseEvent(
            id=uuid.uuid4(),
            case_id=case.id,
            event_type="notice_issued",
            summary=(
                f"Notice {notice.reference} issued with a response deadline "
                f"of {response_due_on}."
            ),
            detail={"notice_id": str(notice.id), "response_due_on": response_due_on.isoformat()},
            actor_id=issued_by_id,
            occurred_at=datetime.now(UTC),
        )
    )
    await audit_service.record(
        db,
        context,
        action="notice.issued",
        entity_type="notice",
        entity_id=notice.id,
        new_values={
            "reference": notice.reference,
            "response_due_on": response_due_on.isoformat(),
            "body_sha256": notice.body_sha256,
        },
    )
    logger.info("notice_issued", notice=notice.reference, case=case.reference)
    return notice


async def record_delivery(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    case: Case,
    notice: Notice,
    method: str,
    delivered_at: datetime,
    proof_reference: str | None,
    note: str | None,
    role: Role,
    actor_id: uuid.UUID,
    expected_version: int,
) -> Notice:
    if notice.issued_at is None:
        raise ConflictError("Issue the notice before recording delivery.", code="notice_not_issued")

    notice.delivery_method = method
    notice.delivered_at = delivered_at
    notice.delivery_proof_reference = proof_reference
    notice.delivery_note = note
    await db.flush()

    await transition(
        db,
        context,
        case=case,
        target_state=CaseState.AWAITING_RESPONSE,
        role=role,
        actor_id=actor_id,
        reason=f"Notice delivered by {method.replace('_', ' ')}.",
        expected_version=expected_version,
    )
    await audit_service.record(
        db,
        context,
        action="notice.delivery_recorded",
        entity_type="notice",
        entity_id=notice.id,
        new_values={"method": method, "proof_reference": proof_reference},
    )
    return notice


async def record_response(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    case: Case,
    notice: Notice,
    summary_text: str,
    received_at: datetime,
    role: Role,
    actor_id: uuid.UUID,
    expected_version: int,
) -> Notice:
    notice.response_received_at = received_at
    notice.response_summary = summary_text.strip()
    await db.flush()

    await transition(
        db,
        context,
        case=case,
        target_state=CaseState.RESPONSE_RECEIVED,
        role=role,
        actor_id=actor_id,
        reason="Response received.",
        expected_version=expected_version,
    )
    db.add(
        CaseEvent(
            id=uuid.uuid4(),
            case_id=case.id,
            event_type="response_received",
            summary=summary_text.strip()[:2000],
            detail={"notice_id": str(notice.id)},
            actor_id=actor_id,
            occurred_at=received_at,
        )
    )
    await audit_service.record(
        db,
        context,
        action="notice.response_recorded",
        entity_type="notice",
        entity_id=notice.id,
        new_values={"received_at": received_at.isoformat()},
    )
    return notice


async def transition(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    case: Case,
    target_state: str,
    role: Role,
    actor_id: uuid.UUID,
    reason: str | None,
    expected_version: int,
) -> Case:
    """Move a case through its state machine."""
    if case.version != expected_version:
        raise StaleRecordError(
            "This case changed after you loaded it. Reload and try again.",
            details={"your_version": expected_version, "current_version": case.version},
        )

    edge = CASE_MACHINE.find(case.state, target_state)
    if edge is None:
        raise InvalidTransitionError(
            f"A case in state {case.state!r} cannot move to {target_state!r}.",
            details={
                "current_state": case.state,
                "allowed_states": list(CASE_MACHINE.targets_from(case.state)),
            },
        )
    if role not in edge.roles:
        raise PermissionDeniedError(
            f"Your role cannot {edge.label.lower()}.",
            details={"required_roles": sorted(item.value for item in edge.roles)},
        )
    if edge.reason_required and not (reason and reason.strip()):
        raise ValidationError(
            f"A reason is required to {edge.label.lower()}.", details={"field": "reason"}
        )

    for guard in edge.guards:
        if guard == "notice_ready":
            ready = await db.scalar(
                select(func.count())
                .select_from(Notice)
                .where(Notice.case_id == case.id, Notice.rendered_body != "")
            )
            if not ready:
                raise GuardFailedError(
                    "Prepare a notice before issuing one.", code="notice_required"
                )
        if guard == "delivery_recorded":
            delivered = await db.scalar(
                select(func.count())
                .select_from(Notice)
                .where(Notice.case_id == case.id, Notice.delivered_at.is_not(None))
            )
            if not delivered:
                raise GuardFailedError(
                    "Record how and when the notice was delivered.",
                    code="delivery_required",
                )

    previous = case.state
    case.state = target_state
    now = datetime.now(UTC)
    if target_state == CaseState.CLOSED:
        case.closed_at = now
        case.closure_reason = (reason or "").strip()[:240] or case.closure_reason
    await db.flush()

    db.add(
        StateTransition(
            id=uuid.uuid4(),
            entity_type="case",
            entity_id=case.id,
            from_state=previous,
            to_state=target_state,
            actor_id=actor_id,
            actor_role=role.value,
            jurisdiction_code=case.jurisdiction_code,
            reason=reason,
            request_id=current_request_id(),
            entity_version=case.version,
            occurred_at=now,
        )
    )
    await db.flush()

    await audit_service.record(
        db,
        context,
        action="case.state_changed",
        entity_type="case",
        entity_id=case.id,
        entity_version=case.version,
        old_values={"state": previous},
        new_values={"state": target_state},
        reason=reason,
    )
    return case


async def available_transitions(db: AsyncSession, case: Case, role: Role) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in CASE_MACHINE.allowed_for(case.state, role):
        blocked: list[str] = []
        if "notice_ready" in item.guards:
            count = await db.scalar(
                select(func.count()).select_from(Notice).where(Notice.case_id == case.id)
            )
            if not count:
                blocked.append("Prepare a notice first.")
        if "delivery_recorded" in item.guards:
            count = await db.scalar(
                select(func.count())
                .select_from(Notice)
                .where(Notice.case_id == case.id, Notice.delivered_at.is_not(None))
            )
            if not count:
                blocked.append("Record delivery of the notice first.")
        results.append(
            {
                "target_state": item.target,
                "label": item.label,
                "reason_required": item.reason_required,
                "blocked_by": blocked,
            }
        )
    return results


def notice_payload(notice: Notice) -> dict[str, Any]:
    return {
        "id": str(notice.id),
        "reference": notice.reference,
        "notice_type": notice.notice_type,
        "template": f"{notice.template_code} v{notice.template_version}",
        "rendered_body": notice.rendered_body,
        "body_sha256": notice.body_sha256,
        "is_frozen": notice.is_frozen,
        "issued_at": notice.issued_at,
        "issued_by_id": str(notice.issued_by_id) if notice.issued_by_id else None,
        "delivery_method": notice.delivery_method,
        "delivered_at": notice.delivered_at,
        "delivery_proof_reference": notice.delivery_proof_reference,
        "response_due_on": notice.response_due_on,
        "response_received_at": notice.response_received_at,
        "response_summary": notice.response_summary,
        "withdrawn_at": notice.withdrawn_at,
        "withdrawn_reason": notice.withdrawn_reason,
    }


def case_payload(case: Case) -> dict[str, Any]:
    return {
        "id": str(case.id),
        "version": case.version,
        "reference": case.reference,
        "state": case.state,
        "inspection_id": str(case.inspection_id),
        "report_id": str(case.report_id) if case.report_id else None,
        "jurisdiction_code": case.jurisdiction_code,
        "jurisdiction_name": case.jurisdiction_name,
        "respondent_name": case.respondent_name,
        "respondent_role": case.respondent_role,
        "respondent_address": case.respondent_address,
        "respondent_email": case.respondent_email,
        "respondent_phone": case.respondent_phone,
        "subject": case.subject,
        "legal_basis": case.legal_basis or [],
        "outcome": case.outcome,
        "outcome_note": case.outcome_note,
        "closure_reason": case.closure_reason,
        "closed_at": case.closed_at,
        "created_at": case.created_at,
        "notices": [notice_payload(item) for item in case.notices],
    }
