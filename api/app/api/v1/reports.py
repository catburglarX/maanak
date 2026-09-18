"""Reports and public verification."""

from __future__ import annotations

import io
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...db import get_db
from ...deps import Principal, public_audit_context, requires
from ...domain.enums import InspectionState
from ...errors import NotFoundError, ValidationError
from ...models.report import Report
from ...schemas.common import Page, PageNumber, PageSize
from ...security import Permission
from ...security.ratelimit import REPORT_VERIFY_PER_IP, enforce
from ...services import audit as audit_service
from ...services import inspections as inspection_service
from ...services import reports as report_service

router = APIRouter(prefix="/reports", tags=["reports"])
public_router = APIRouter(prefix="/public", tags=["public"])


def _summary(report: Report) -> dict[str, Any]:
    snapshot = report.snapshot or {}
    inspection = snapshot.get("inspection", {})
    product = snapshot.get("product", {})
    return {
        "id": str(report.id),
        "reference": report.reference,
        "revision": report.revision,
        "state": report.state,
        "inspection_id": str(report.inspection_id),
        "inspection_reference": inspection.get("reference"),
        "product": f"{product.get('brand', '')} {product.get('name', '')}".strip(),
        "decision": inspection.get("decision"),
        "jurisdiction_code": report.jurisdiction_code,
        "issued_at": report.issued_at,
        "issued_by_id": str(report.issued_by_id) if report.issued_by_id else None,
        "issuing_workspace": report.issuing_workspace,
        "snapshot_sha256": report.snapshot_sha256,
        "verification_code": report.verification_code,
        "verification_url": report_service.verification_url(report),
        "signature_status": report.signature_status,
        "withdrawn_at": report.withdrawn_at,
        "withdrawn_reason": report.withdrawn_reason,
        "documents": [
            {
                "format": document.format,
                "size_bytes": document.size_bytes,
                "sha256": document.sha256,
                "page_count": document.page_count,
            }
            for document in report.documents
        ],
    }


async def _load(db: AsyncSession, report_id: uuid.UUID) -> Report:
    report = await db.scalar(
        select(Report)
        .options(selectinload(Report.documents))
        .where(Report.id == report_id)
        .execution_options(populate_existing=True)
    )
    if report is None:
        raise NotFoundError("That report was not found.")
    return report


@router.get("", summary="List issued reports")
async def list_reports(
    page: PageNumber = Query(1),
    page_size: PageSize = Query(25),
    principal: Principal = Depends(requires(Permission.REPORT_READ)),
    db: AsyncSession = Depends(get_db),
) -> Page[dict]:
    conditions: list[Any] = []
    scope_filter = principal.scope.filter(Report.jurisdiction_code)
    if scope_filter is not None:
        conditions.append(scope_filter)

    total = int(await db.scalar(select(func.count()).select_from(Report).where(*conditions)) or 0)
    rows = list(
        await db.scalars(
            select(Report)
            .options(selectinload(Report.documents))
            .where(*conditions)
            .order_by(Report.issued_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return Page.build([_summary(row) for row in rows], page=page, page_size=page_size, total=total)


@router.post(
    "/inspections/{inspection_id}",
    status_code=status.HTTP_201_CREATED,
    summary="Issue a report for an inspection",
)
async def issue_report(
    inspection_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(requires(Permission.REPORT_ISSUE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Freeze the inspection, render the documents and record the hashes.

    Refused unless a decision with a reason exists, every reading has been reviewed,
    and the checks have been run since the last change.
    """
    inspection = await inspection_service.get_for_actor(db, inspection_id, principal.scope)
    context = principal.audit_context(request)

    generated = await report_service.issue(
        db,
        context,
        inspection=inspection,
        issued_by_id=principal.id,
        issuing_workspace=(f"{inspection.jurisdiction_name} (Maanak workspace)"),
    )

    # Move the inspection to report_issued now that a report exists.
    await inspection_service.apply_transition_unchecked(
        db,
        inspection=inspection,
        target=InspectionState.REPORT_ISSUED,
        reason=f"Report {generated.report.reference} issued.",
        actor_id=principal.id,
        actor_role=principal.role.value,
    )
    await db.commit()

    report = await _load(db, generated.report.id)
    return _summary(report)


@router.get("/{report_id}", summary="Read one report")
async def read_report(
    report_id: uuid.UUID,
    principal: Principal = Depends(requires(Permission.REPORT_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    report = await _load(db, report_id)
    principal.scope.require(report.jurisdiction_code)
    payload = _summary(report)
    payload["integrity"] = report_service.verify_snapshot(report)
    return payload


@router.get("/{report_id}/snapshot", summary="The frozen record behind a report")
async def read_snapshot(
    report_id: uuid.UUID,
    principal: Principal = Depends(requires(Permission.REPORT_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Everything the report was built from, exactly as hashed."""
    report = await _load(db, report_id)
    principal.scope.require(report.jurisdiction_code)
    return {
        "reference": report.reference,
        "snapshot_sha256": report.snapshot_sha256,
        "snapshot_algorithm": report.snapshot_algorithm,
        "integrity": report_service.verify_snapshot(report),
        "snapshot": report.snapshot,
    }


@router.get("/{report_id}/document.{fmt}", summary="Download a report document")
async def download_report(
    report_id: uuid.UUID,
    fmt: str,
    request: Request,
    principal: Principal = Depends(requires(Permission.REPORT_DOWNLOAD)),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Served through the API so every download is recorded.

    The document's hash is re-checked before it is served; a mismatch is refused
    rather than delivered.
    """
    report = await _load(db, report_id)
    principal.scope.require(report.jurisdiction_code)

    document, payload = await report_service.get_document(db, report=report, fmt=fmt)

    await audit_service.record(
        db,
        principal.audit_context(request),
        action="report.downloaded",
        entity_type="report",
        entity_id=report.id,
        new_values={"format": fmt, "sha256": document.sha256},
    )
    await db.commit()

    return StreamingResponse(
        io.BytesIO(payload),
        media_type=document.mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{report.reference}.{fmt}"',
            "X-Document-SHA256": document.sha256,
            "X-Snapshot-SHA256": report.snapshot_sha256,
            "Cache-Control": "no-store",
        },
    )


@router.post("/{report_id}/withdraw", summary="Withdraw an issued report")
async def withdraw_report(
    report_id: uuid.UUID,
    request: Request,
    reason: str = Query(min_length=15, max_length=2000),
    principal: Principal = Depends(requires(Permission.REPORT_WITHDRAW)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    report = await _load(db, report_id)
    principal.scope.require(report.jurisdiction_code)
    await report_service.withdraw(
        db,
        principal.audit_context(request),
        report=report,
        withdrawn_by_id=principal.id,
        reason=reason,
    )
    await db.commit()
    reloaded = await _load(db, report_id)
    return _summary(reloaded)


# --------------------------------------------------------------------------
# Public verification
# --------------------------------------------------------------------------
@public_router.get("/reports/verify", summary="Verify a report (public)")
async def verify_report(
    request: Request,
    reference: str = Query(default="", max_length=40),
    code: str = Query(default="", max_length=24),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Confirm that a report reference was issued and its content is unchanged.

    Reveals only safe facts: the reference, when it was issued, by which workspace,
    the content hash, and whether it is current, withdrawn or superseded. It does not
    reveal the product, the premises, the officer or the findings, because a reference
    printed on a document is not authorisation to read the case.
    """
    await enforce(REPORT_VERIFY_PER_IP, request.client.host if request.client else "unknown")

    if not reference and not code:
        raise ValidationError(
            "Supply the report reference or the verification code printed on the document.",
            details={"fields": ["reference", "code"]},
        )

    report = None
    if reference:
        report = await report_service.find_by_reference(db, reference)
    if report is None and code:
        report = await report_service.find_by_verification_code(db, code)

    if report is None:
        # A deliberately unhelpful answer: confirming which of two identifiers was
        # wrong would let someone enumerate references.
        return {
            "found": False,
            "detail": (
                "No report matches what you entered. Check the reference and the "
                "verification code exactly as printed on the document."
            ),
        }

    # When both were supplied, both must match.
    if reference and code and report.verification_code.upper() != code.strip().upper():
        return {
            "found": False,
            "detail": (
                "No report matches what you entered. Check the reference and the "
                "verification code exactly as printed on the document."
            ),
        }

    summary = report_service.public_summary(report)
    summary["found"] = True

    await audit_service.record(
        db,
        public_audit_context(request),
        action="report.publicly_verified",
        entity_type="report",
        entity_id=report.id,
        new_values={"reference": report.reference, "matched_by": "code" if code else "reference"},
    )
    await db.commit()
    return summary
