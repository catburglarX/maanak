"""Cases and notices."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...db import get_db
from ...deps import Principal, requires
from ...domain.enums import CaseState
from ...errors import ConflictError, NotFoundError, ValidationError
from ...models.case_file import Case, CaseEvent, Notice
from ...models.inspection import StateTransition
from ...schemas.common import Page, PageNumber, PageSize
from ...schemas.matters import (
    CaseEventRequest,
    CaseOpenRequest,
    CaseOutcomeRequest,
    CaseTransitionRequest,
    NoticeDeliveryRequest,
    NoticeIssueRequest,
    NoticePrepareRequest,
    NoticeResponseRequest,
)
from ...security import Permission
from ...services import audit as audit_service
from ...services import cases as case_service
from ...services import inspections as inspection_service

router = APIRouter(prefix="/cases", tags=["cases"])


async def _load(db: AsyncSession, case_id: uuid.UUID) -> Case:
    case = await db.scalar(
        select(Case)
        .options(selectinload(Case.notices))
        .where(Case.id == case_id)
        .execution_options(populate_existing=True)
    )
    if case is None:
        raise NotFoundError("That case was not found.")
    return case


async def _detail(db: AsyncSession, case: Case, principal: Principal) -> dict[str, Any]:
    payload = case_service.case_payload(case)
    payload["available_transitions"] = await case_service.available_transitions(
        db, case, principal.role
    )
    payload["events"] = [
        {
            "event_type": item.event_type,
            "summary": item.summary,
            "detail": item.detail,
            "scheduled_for": item.scheduled_for,
            "occurred_at": item.occurred_at,
            "actor_id": str(item.actor_id) if item.actor_id else None,
        }
        for item in await db.scalars(
            select(CaseEvent)
            .where(CaseEvent.case_id == case.id)
            .order_by(CaseEvent.occurred_at.asc())
        )
    ]
    payload["timeline"] = [
        {
            "at": item.occurred_at,
            "kind": "state_change",
            "summary": f"{item.from_state or 'opened'} to {item.to_state}",
            "actor_role": item.actor_role,
            "detail": {"reason": item.reason},
        }
        for item in await db.scalars(
            select(StateTransition)
            .where(
                StateTransition.entity_type == "case",
                StateTransition.entity_id == case.id,
            )
            .order_by(StateTransition.occurred_at.asc())
        )
    ]
    return payload


@router.get("", summary="List cases")
async def list_cases(
    page: PageNumber = Query(1),
    page_size: PageSize = Query(25),
    state: CaseState | None = Query(None),
    overdue_only: bool = Query(False),
    principal: Principal = Depends(requires(Permission.CASE_READ)),
    db: AsyncSession = Depends(get_db),
) -> Page[dict]:
    conditions: list[Any] = []
    scope_filter = principal.scope.filter(Case.jurisdiction_code)
    if scope_filter is not None:
        conditions.append(scope_filter)
    if state is not None:
        conditions.append(Case.state == state.value)
    if overdue_only:
        # Cases with a notice whose deadline has passed and no response recorded.
        conditions.append(
            Case.id.in_(
                select(Notice.case_id).where(
                    Notice.response_due_on < datetime.now(UTC).date(),
                    Notice.response_received_at.is_(None),
                    Notice.issued_at.is_not(None),
                    Notice.withdrawn_at.is_(None),
                )
            )
        )

    total = int(await db.scalar(select(func.count()).select_from(Case).where(*conditions)) or 0)
    rows = list(
        await db.scalars(
            select(Case)
            .options(selectinload(Case.notices))
            .where(*conditions)
            .order_by(Case.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return Page.build(
        [case_service.case_payload(row) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.post(
    "", status_code=status.HTTP_201_CREATED, summary="Open a case from a reported inspection"
)
async def open_case(
    payload: CaseOpenRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.CASE_CREATE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """A case requires an issued report and at least one non-compliant finding."""
    inspection = await inspection_service.get_for_actor(
        db, uuid.UUID(payload.inspection_id), principal.scope
    )
    case = await case_service.open_case(
        db,
        principal.audit_context(request),
        inspection=inspection,
        opened_by_id=principal.id,
        respondent_name=payload.respondent_name,
        respondent_role=payload.respondent_role,
        respondent_address=payload.respondent_address,
        respondent_email=str(payload.respondent_email) if payload.respondent_email else None,
        respondent_phone=payload.respondent_phone,
        subject=payload.subject,
    )
    await db.commit()
    return await _detail(db, await _load(db, case.id), principal)


@router.get("/{case_id}", summary="Read one case")
async def read_case(
    case_id: uuid.UUID,
    principal: Principal = Depends(requires(Permission.CASE_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    case = await _load(db, case_id)
    principal.scope.require(case.jurisdiction_code)
    return await _detail(db, case, principal)


@router.post(
    "/{case_id}/notices",
    status_code=status.HTTP_201_CREATED,
    summary="Prepare a notice for review",
)
async def prepare_notice(
    case_id: uuid.UUID,
    payload: NoticePrepareRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.CASE_UPDATE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Render the notice text without issuing it, so it can be read first."""
    case = await _load(db, case_id)
    principal.scope.require(case.jurisdiction_code)
    notice = await case_service.prepare_notice(
        db,
        principal.audit_context(request),
        case=case,
        notice_type=payload.notice_type.value,
        response_days=payload.response_days,
        signature_block=payload.signature_block,
        prepared_by_id=principal.id,
    )
    await db.commit()
    return case_service.notice_payload(notice)


@router.post("/{case_id}/notices/{notice_id}/issue", summary="Issue a prepared notice")
async def issue_notice(
    case_id: uuid.UUID,
    notice_id: uuid.UUID,
    payload: NoticeIssueRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.NOTICE_ISSUE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Serving a notice freezes its text permanently."""
    case = await _load(db, case_id)
    principal.scope.require(case.jurisdiction_code)
    notice = await db.get(Notice, notice_id)
    if notice is None or notice.case_id != case.id:
        raise NotFoundError("That notice was not found.")

    await case_service.issue_notice(
        db,
        principal.audit_context(request),
        case=case,
        notice=notice,
        issued_by_id=principal.id,
        response_due_on=payload.response_due_on,
        role=principal.role,
        expected_version=payload.expected_version,
    )
    await db.commit()
    return await _detail(db, await _load(db, case_id), principal)


@router.post("/{case_id}/notices/{notice_id}/delivery", summary="Record how a notice was delivered")
async def record_delivery(
    case_id: uuid.UUID,
    notice_id: uuid.UUID,
    payload: NoticeDeliveryRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.CASE_UPDATE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    case = await _load(db, case_id)
    principal.scope.require(case.jurisdiction_code)
    notice = await db.get(Notice, notice_id)
    if notice is None or notice.case_id != case.id:
        raise NotFoundError("That notice was not found.")

    await case_service.record_delivery(
        db,
        principal.audit_context(request),
        case=case,
        notice=notice,
        method=payload.method.value,
        delivered_at=payload.delivered_at,
        proof_reference=payload.proof_reference,
        note=payload.note,
        role=principal.role,
        actor_id=principal.id,
        expected_version=payload.expected_version,
    )
    await db.commit()
    return await _detail(db, await _load(db, case_id), principal)


@router.post("/{case_id}/notices/{notice_id}/response", summary="Record a response to a notice")
async def record_response(
    case_id: uuid.UUID,
    notice_id: uuid.UUID,
    payload: NoticeResponseRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.CASE_UPDATE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    case = await _load(db, case_id)
    principal.scope.require(case.jurisdiction_code)
    notice = await db.get(Notice, notice_id)
    if notice is None or notice.case_id != case.id:
        raise NotFoundError("That notice was not found.")

    await case_service.record_response(
        db,
        principal.audit_context(request),
        case=case,
        notice=notice,
        summary_text=payload.summary,
        received_at=payload.received_at,
        role=principal.role,
        actor_id=principal.id,
        expected_version=payload.expected_version,
    )
    await db.commit()
    return await _detail(db, await _load(db, case_id), principal)


@router.post("/{case_id}/notices/{notice_id}/withdraw", summary="Withdraw an issued notice")
async def withdraw_notice(
    case_id: uuid.UUID,
    notice_id: uuid.UUID,
    request: Request,
    reason: str = Query(min_length=10, max_length=2000),
    principal: Principal = Depends(requires(Permission.NOTICE_WITHDRAW)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The served text is kept; only its status changes."""
    case = await _load(db, case_id)
    principal.scope.require(case.jurisdiction_code)
    notice = await db.get(Notice, notice_id)
    if notice is None or notice.case_id != case.id:
        raise NotFoundError("That notice was not found.")
    if notice.issued_at is None:
        raise ConflictError("That notice has not been issued.", code="notice_not_issued")
    if notice.withdrawn_at is not None:
        raise ConflictError("That notice is already withdrawn.", code="notice_withdrawn")

    notice.withdrawn_at = datetime.now(UTC)
    notice.withdrawn_reason = reason.strip()
    await db.flush()

    await audit_service.record(
        db,
        principal.audit_context(request),
        action="notice.withdrawn",
        entity_type="notice",
        entity_id=notice.id,
        new_values={"reference": notice.reference},
        reason=reason,
    )
    await db.commit()
    return await _detail(db, await _load(db, case_id), principal)


@router.post("/{case_id}/transitions", summary="Change the case state")
async def transition_case(
    case_id: uuid.UUID,
    payload: CaseTransitionRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.CASE_TRANSITION)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    case = await _load(db, case_id)
    principal.scope.require(case.jurisdiction_code)
    if payload.target_state == CaseState.CLOSED:
        principal.require(Permission.CASE_CLOSE)

    await case_service.transition(
        db,
        principal.audit_context(request),
        case=case,
        target_state=payload.target_state.value,
        role=principal.role,
        actor_id=principal.id,
        reason=payload.reason,
        expected_version=payload.expected_version,
    )
    await db.commit()
    return await _detail(db, await _load(db, case_id), principal)


@router.post("/{case_id}/outcome", summary="Record the case outcome")
async def record_outcome(
    case_id: uuid.UUID,
    payload: CaseOutcomeRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.CASE_CLOSE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Records what was decided. Maanak does not itself determine any penalty."""
    case = await _load(db, case_id)
    principal.scope.require(case.jurisdiction_code)
    if case.version != payload.expected_version:
        from ...errors import StaleRecordError

        raise StaleRecordError("This case changed after you loaded it. Reload and try again.")

    case.outcome = payload.outcome.strip()
    case.outcome_note = payload.outcome_note.strip()
    await db.flush()

    await case_service.transition(
        db,
        principal.audit_context(request),
        case=case,
        target_state=CaseState.RESOLVED.value,
        role=principal.role,
        actor_id=principal.id,
        reason=payload.outcome_note,
        expected_version=case.version,
    )
    await db.commit()
    return await _detail(db, await _load(db, case_id), principal)


@router.post(
    "/{case_id}/events", status_code=status.HTTP_201_CREATED, summary="Add a case timeline entry"
)
async def add_event(
    case_id: uuid.UUID,
    payload: CaseEventRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.CASE_UPDATE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """For hearings, reminders and notes that are not state changes."""
    case = await _load(db, case_id)
    principal.scope.require(case.jurisdiction_code)
    if payload.scheduled_for and payload.scheduled_for < datetime.now(UTC):
        raise ValidationError(
            "A scheduled time must be in the future.", details={"field": "scheduled_for"}
        )

    db.add(
        CaseEvent(
            id=uuid.uuid4(),
            case_id=case.id,
            event_type=payload.event_type.strip(),
            summary=payload.summary.strip(),
            detail={},
            scheduled_for=payload.scheduled_for,
            actor_id=principal.id,
            occurred_at=datetime.now(UTC),
        )
    )
    await audit_service.record(
        db,
        principal.audit_context(request),
        action="case.event_recorded",
        entity_type="case",
        entity_id=case.id,
        new_values={"event_type": payload.event_type},
    )
    await db.commit()
    return await _detail(db, await _load(db, case_id), principal)
