"""Audit trail.

Read-only. There is no endpoint that edits or deletes an audit event, and the
database refuses UPDATE and DELETE on the table regardless, so the API cannot rewrite
history even if a bug tried to.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_db
from ...deps import Principal, requires
from ...models.audit import AuditEvent
from ...models.inspection import StateTransition
from ...schemas.common import Page, PageNumber, PageSize
from ...schemas.matters import AuditEventOut, ChainVerificationOut
from ...security import Permission
from ...services import audit as audit_service

router = APIRouter(prefix="/audit", tags=["audit"])


def _event_out(event: AuditEvent) -> AuditEventOut:
    return AuditEventOut(
        id=str(event.id),
        sequence=event.sequence,
        action=event.action,
        entity_type=event.entity_type,
        entity_id=str(event.entity_id) if event.entity_id else None,
        entity_version=event.entity_version,
        actor_id=str(event.actor_id) if event.actor_id else None,
        actor_role=event.actor_role,
        actor_email=event.actor_email,
        actor_is_public=event.actor_is_public,
        jurisdiction_code=event.jurisdiction_code,
        old_values=event.old_values or {},
        new_values=event.new_values or {},
        reason=event.reason,
        request_id=event.request_id,
        recorded_at=event.recorded_at,
        event_hash=event.event_hash,
        previous_hash=event.previous_hash,
    )


@router.get("", response_model=Page[AuditEventOut], summary="Read the audit trail")
async def list_events(
    page: PageNumber = Query(1),
    page_size: PageSize = Query(50),
    action: str | None = Query(None, max_length=80),
    entity_type: str | None = Query(None, max_length=40),
    entity_id: uuid.UUID | None = Query(None),
    actor_id: uuid.UUID | None = Query(None),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    principal: Principal = Depends(requires(Permission.AUDIT_READ)),
    db: AsyncSession = Depends(get_db),
) -> Page[AuditEventOut]:
    """Newest first.

    A controller sees events for their own jurisdiction subtree plus events with no
    jurisdiction (system and public actions). An administrator sees everything.
    """
    conditions: list[Any] = []
    scope_filter = principal.scope.filter(AuditEvent.jurisdiction_code, allow_null=True)
    if scope_filter is not None:
        conditions.append(scope_filter)
    if action:
        conditions.append(AuditEvent.action == action)
    if entity_type:
        conditions.append(AuditEvent.entity_type == entity_type)
    if entity_id:
        conditions.append(AuditEvent.entity_id == entity_id)
    if actor_id:
        conditions.append(AuditEvent.actor_id == actor_id)
    if since:
        conditions.append(AuditEvent.recorded_at >= since)
    if until:
        conditions.append(AuditEvent.recorded_at <= until)

    total = int(
        await db.scalar(select(func.count()).select_from(AuditEvent).where(*conditions)) or 0
    )
    rows = list(
        await db.scalars(
            select(AuditEvent)
            .where(*conditions)
            .order_by(AuditEvent.sequence.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return Page.build(
        [_event_out(row) for row in rows], page=page, page_size=page_size, total=total
    )


@router.get("/actions", summary="Distinct recorded actions")
async def list_actions(
    _principal: Principal = Depends(requires(Permission.AUDIT_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Used to populate the filter, so the interface never offers a value with no rows."""
    rows = await db.execute(
        select(AuditEvent.action, func.count())
        .group_by(AuditEvent.action)
        .order_by(func.count().desc())
    )
    return [{"action": action, "count": int(total)} for action, total in rows]


@router.get("/verify", response_model=ChainVerificationOut, summary="Verify the audit hash chain")
async def verify_chain(
    chain_key: str = Query(default="global", max_length=64),
    limit: int | None = Query(default=None, ge=1, le=200_000),
    _principal: Principal = Depends(requires(Permission.AUDIT_VERIFY)),
    db: AsyncSession = Depends(get_db),
) -> ChainVerificationOut:
    """Replay every event and recompute its hash.

    Detects three kinds of tampering: an altered field, a removed row, and a rewritten
    link. The result is a fact about the stored data, not an assertion of good faith.
    """
    result = await audit_service.verify_chain(db, chain_key=chain_key, limit=limit)
    summary = await audit_service.chain_summary(db, chain_key=chain_key)
    return ChainVerificationOut(
        chain_key=result.chain_key,
        events_checked=result.events_checked,
        intact=result.intact,
        first_broken_sequence=result.first_broken_sequence,
        problem=result.problem,
        head_sequence=summary["head_sequence"],
        head_hash=summary["head_hash"],
        note=(
            "Each event carries the hash of the previous event. Recomputing the chain "
            "confirms that no recorded event has been altered or removed. The database "
            "also refuses UPDATE and DELETE on this table."
            if result.intact
            else "The chain does not verify. Treat the audit trail as compromised from "
            "the sequence reported and investigate."
        ),
    )


@router.get("/summary", summary="Chain status")
async def chain_status(
    chain_key: str = Query(default="global", max_length=64),
    _principal: Principal = Depends(requires(Permission.AUDIT_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await audit_service.chain_summary(db, chain_key=chain_key)


@router.get("/entity/{entity_type}/{entity_id}", summary="Everything recorded about one record")
async def entity_history(
    entity_type: str,
    entity_id: uuid.UUID,
    principal: Principal = Depends(requires(Permission.AUDIT_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Audit events and state transitions for one record, in order."""
    events = list(
        await db.scalars(
            select(AuditEvent)
            .where(AuditEvent.entity_type == entity_type, AuditEvent.entity_id == entity_id)
            .order_by(AuditEvent.sequence.asc())
        )
    )
    for event in events:
        principal.scope.require(event.jurisdiction_code)

    transitions = list(
        await db.scalars(
            select(StateTransition)
            .where(
                StateTransition.entity_type == entity_type,
                StateTransition.entity_id == entity_id,
            )
            .order_by(StateTransition.occurred_at.asc())
        )
    )
    return {
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "events": [_event_out(item).model_dump() for item in events],
        "state_transitions": [
            {
                "from_state": item.from_state,
                "to_state": item.to_state,
                "actor_id": str(item.actor_id) if item.actor_id else None,
                "actor_role": item.actor_role,
                "reason": item.reason,
                "occurred_at": item.occurred_at,
                "request_id": item.request_id,
            }
            for item in transitions
        ],
    }


@router.get("/export.csv", summary="Export the audit trail as CSV")
async def export_csv(
    request: Request,
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    limit: int = Query(default=10_000, ge=1, le=100_000),
    principal: Principal = Depends(requires(Permission.AUDIT_READ, Permission.EXPORT_CREATE)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Exports are themselves recorded, so an export leaves a trace."""
    conditions: list[Any] = []
    scope_filter = principal.scope.filter(AuditEvent.jurisdiction_code, allow_null=True)
    if scope_filter is not None:
        conditions.append(scope_filter)
    if since:
        conditions.append(AuditEvent.recorded_at >= since)
    if until:
        conditions.append(AuditEvent.recorded_at <= until)

    rows = list(
        await db.scalars(
            select(AuditEvent).where(*conditions).order_by(AuditEvent.sequence.asc()).limit(limit)
        )
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "sequence",
            "recorded_at",
            "action",
            "entity_type",
            "entity_id",
            "actor_role",
            "actor_email",
            "jurisdiction_code",
            "reason",
            "request_id",
            "event_hash",
            "previous_hash",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.sequence,
                row.recorded_at.isoformat(),
                row.action,
                row.entity_type,
                str(row.entity_id) if row.entity_id else "",
                row.actor_role or "",
                row.actor_email or "",
                row.jurisdiction_code or "",
                (row.reason or "").replace("\n", " "),
                row.request_id or "",
                row.event_hash,
                row.previous_hash or "",
            ]
        )

    await audit_service.record(
        db,
        principal.audit_context(request),
        action="audit.exported",
        entity_type="audit_events",
        new_values={"rows": len(rows)},
    )
    await db.commit()

    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="maanak-audit-trail.csv"',
            "Cache-Control": "no-store",
        },
    )
