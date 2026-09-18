"""Inspections, evidence, candidate review and findings.

Every handler here resolves the inspection through
``inspections.get_for_actor``, which applies jurisdiction scope and raises 404 for a
record the caller may not see. List endpoints apply the same scope inside the SQL
query rather than filtering after loading.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...db import get_db
from ...deps import Principal, get_principal, requires
from ...domain.enums import (
    DeclarationType,
    EvidenceKind,
    ReviewState,
)
from ...errors import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    StaleRecordError,
    ValidationError,
)
from ...models.evidence import Evidence
from ...models.finding import CandidateRevision, DeclarationCandidate, Finding
from ...models.inspection import Inspection, StateTransition
from ...models.product import Product
from ...models.report import Report
from ...models.rule import RuleVersion
from ...observability import current_request_id
from ...schemas.common import Page, PageNumber, PageSize, TransitionRequest
from ...schemas.workspace import (
    CandidateOut,
    CandidateReviewRequest,
    DecisionRequest,
    EvidenceSummary,
    EvidenceUploadResponse,
    FaceStateRequest,
    FindingOut,
    FindingOverrideRequest,
    InspectionCreateRequest,
    InspectionDetail,
    InspectionSummary,
    InspectionUpdateRequest,
    MeasurementRequest,
    RunChecksResponse,
)
from ...security import Permission
from ...security.ratelimit import EVIDENCE_UPLOAD_PER_USER, enforce
from ...services import audit as audit_service
from ...services import evidence as evidence_service
from ...services import inspections as inspection_service
from ...services import jobs, storage
from ...services.canonical import json_safe
from ...services.extraction.fields import SPECS_BY_TYPE
from ...services.rules import evaluate_inspection, summarise_outcomes
from .products import _summary as product_summary
from .products import create_product_record, load_product


async def _load_products(
    db: AsyncSession, product_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Product]:
    """Batch-load products with their identifiers for a page of inspections.

    One query for the products plus one for the identifiers, rather than a pair per
    row, so a 25-row register does not issue fifty queries.
    """
    if not product_ids:
        return {}
    from sqlalchemy.orm import selectinload

    rows = await db.scalars(
        select(Product)
        .options(
            selectinload(Product.identifiers),
            selectinload(Product.responsible_parties),
        )
        .where(Product.id.in_(product_ids))
        .execution_options(populate_existing=True)
    )
    return {row.id: row for row in rows}


router = APIRouter(prefix="/inspections", tags=["inspections"])
candidate_router = APIRouter(prefix="/candidates", tags=["review"])
finding_router = APIRouter(prefix="/findings", tags=["review"])
job_router = APIRouter(prefix="/jobs", tags=["operations"])


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------
def _evidence_summary(evidence: Evidence) -> EvidenceSummary:
    quality = evidence.quality or {}
    return EvidenceSummary(
        id=str(evidence.id),
        face=evidence.face,
        sequence=evidence.sequence,
        original_filename=evidence.original_filename,
        detected_mime_type=evidence.detected_mime_type,
        size_bytes=evidence.size_bytes,
        sha256=evidence.sha256,
        original_width=evidence.original_width,
        original_height=evidence.original_height,
        analysis_state=evidence.analysis_state,
        analysis_failure_reason=evidence.analysis_failure_reason,
        quality_verdict=quality.get("verdict"),
        quality_actions=list(quality.get("actions", [])),
        quality_override_reason=evidence.quality_override_reason,
        barcodes=list(evidence.barcodes or []),
        uploaded_by_id=str(evidence.uploaded_by_id) if evidence.uploaded_by_id else None,
        server_received_at=evidence.server_received_at,
    )


def _candidate_out(candidate: DeclarationCandidate) -> CandidateOut:
    value = candidate.effective_value or {}
    return CandidateOut(
        id=str(candidate.id),
        version=candidate.version,
        evidence_id=str(candidate.evidence_id) if candidate.evidence_id else None,
        declaration_type=candidate.declaration_type,
        machine_state=candidate.machine_state,
        review_state=candidate.review_state,
        matched_text=candidate.matched_text,
        context_text=candidate.context_text,
        normalised_value=candidate.normalised_value or {},
        display_value=str(value.get("display") or value.get("value") or "") or None,
        unit=candidate.effective_unit,
        region=list(candidate.region or []),
        text_height_px=candidate.text_height_px,
        machine_confidence=candidate.machine_confidence,
        machine_explanation=candidate.machine_explanation,
        parser_name=candidate.parser_name,
        corrected_value=candidate.corrected_value or {},
        correction_reason=candidate.correction_reason,
        review_note=candidate.review_note,
        reviewed_by_id=str(candidate.reviewed_by_id) if candidate.reviewed_by_id else None,
        reviewed_at=candidate.reviewed_at,
        is_usable_for_rules=candidate.is_usable_for_rules,
    )


def _finding_out(finding: Finding, rule: RuleVersion | None) -> FindingOut:
    return FindingOut(
        id=str(finding.id),
        version=finding.version,
        declaration_type=finding.declaration_type,
        outcome=finding.outcome,
        effective_outcome=finding.effective_outcome,
        explanation=finding.explanation,
        expected_value=finding.expected_value,
        observed_value=finding.observed_value,
        calculation=list(finding.calculation or []),
        test_inputs=finding.test_inputs or {},
        selection_reason=finding.selection_reason or {},
        rule=(
            {
                "id": str(rule.id),
                "code": rule.code,
                "version": rule.version,
                "title": rule.title,
                "citation": rule.citation,
                "source_url": rule.source_url,
                "effective_from": rule.effective_from.isoformat(),
                "effective_to": rule.effective_to.isoformat() if rule.effective_to else None,
                "status": rule.status,
                "plain_explanation": rule.plain_explanation,
                "interpretation_note": rule.interpretation_note,
                "uncertainty_note": rule.uncertainty_note,
                "legal_authority_confirmed": rule.legal_authority_confirmed,
            }
            if rule
            else {}
        ),
        candidate_id=str(finding.candidate_id) if finding.candidate_id else None,
        officer_outcome=finding.officer_outcome,
        officer_note=finding.officer_note,
        engine_version=finding.engine_version,
        is_current=finding.is_current,
    )


async def _counts_for(db: AsyncSession, inspection_id: uuid.UUID) -> tuple[int, int, int]:
    evidence_count = int(
        await db.scalar(
            select(func.count())
            .select_from(Evidence)
            .where(Evidence.inspection_id == inspection_id)
        )
        or 0
    )
    pending = int(
        await db.scalar(
            select(func.count())
            .select_from(DeclarationCandidate)
            .where(
                DeclarationCandidate.inspection_id == inspection_id,
                DeclarationCandidate.review_state == ReviewState.PENDING,
            )
        )
        or 0
    )
    findings = int(
        await db.scalar(
            select(func.count())
            .select_from(Finding)
            .where(Finding.inspection_id == inspection_id, Finding.is_current.is_(True))
        )
        or 0
    )
    return evidence_count, pending, findings


async def _summary_for(
    db: AsyncSession, inspection: Inspection, product: Product
) -> InspectionSummary:
    evidence_count, pending, findings = await _counts_for(db, inspection.id)
    return InspectionSummary(
        id=str(inspection.id),
        version=inspection.version,
        reference=inspection.reference,
        state=inspection.state,
        decision=inspection.decision,
        jurisdiction_code=inspection.jurisdiction_code,
        jurisdiction_name=inspection.jurisdiction_name,
        source=inspection.source,
        inspection_date=inspection.inspection_date,
        premises_name=inspection.premises_name,
        product=product_summary(product),
        evidence_count=evidence_count,
        pending_candidate_count=pending,
        current_finding_count=findings,
        created_at=inspection.created_at,
        updated_at=inspection.updated_at,
    )


# --------------------------------------------------------------------------
# Inspection collection
# --------------------------------------------------------------------------
@router.get("", response_model=Page[InspectionSummary], summary="List inspections")
async def list_inspections(
    page: PageNumber = Query(1),
    page_size: PageSize = Query(25),
    search: str = Query("", max_length=160),
    state: str | None = Query(None, max_length=40),
    decided: bool | None = Query(None),
    assigned_to_me: bool = Query(False),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    sort: str = Query(
        "updated_desc", pattern="^(updated_desc|updated_asc|created_desc|date_desc)$"
    ),
    principal: Principal = Depends(requires(Permission.INSPECTION_READ)),
    db: AsyncSession = Depends(get_db),
) -> Page[InspectionSummary]:
    conditions: list[Any] = []

    scope_filter = principal.scope.filter(Inspection.jurisdiction_code)
    if scope_filter is not None:
        conditions.append(scope_filter)

    if search:
        pattern = f"%{search.lower()}%"
        conditions.append(
            or_(
                func.lower(Inspection.reference).like(pattern),
                func.lower(Inspection.premises_name).like(pattern),
                Inspection.product_id.in_(
                    select(Product.id).where(
                        or_(
                            func.lower(Product.brand).like(pattern),
                            func.lower(Product.name).like(pattern),
                        )
                    )
                ),
            )
        )
    if state:
        conditions.append(Inspection.state == state)
    if decided is True:
        conditions.append(Inspection.decision.is_not(None))
    if decided is False:
        conditions.append(Inspection.decision.is_(None))
    if assigned_to_me:
        conditions.append(Inspection.assigned_officer_id == principal.id)
    if from_date:
        conditions.append(Inspection.inspection_date >= from_date)
    if to_date:
        conditions.append(Inspection.inspection_date <= to_date)

    orderings: dict[str, Any] = {
        "updated_desc": Inspection.updated_at.desc(),
        "updated_asc": Inspection.updated_at.asc(),
        "created_desc": Inspection.created_at.desc(),
        "date_desc": Inspection.inspection_date.desc(),
    }
    ordering = orderings[sort]

    total = int(
        await db.scalar(select(func.count()).select_from(Inspection).where(*conditions)) or 0
    )
    rows = list(
        await db.scalars(
            select(Inspection)
            .where(*conditions)
            .order_by(ordering)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )

    products = await _load_products(db, [row.product_id for row in rows])
    items: list[InspectionSummary] = []
    for row in rows:
        product = products.get(row.product_id)
        if product is None:
            continue
        items.append(await _summary_for(db, row, product))
    return Page.build(items, page=page, page_size=page_size, total=total)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=InspectionDetail,
    summary="Open an inspection",
)
async def create_inspection(
    payload: InspectionCreateRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.INSPECTION_CREATE)),
    db: AsyncSession = Depends(get_db),
) -> InspectionDetail:
    context = principal.audit_context(request)

    if payload.product_id:
        product = await load_product(db, uuid.UUID(payload.product_id))
        if product is None:
            raise NotFoundError("That product was not found.")
    elif payload.product is not None:
        created = await create_product_record(
            db, context, payload=payload.product, created_by_id=principal.id
        )
        await db.flush()
        product = await load_product(db, created.id) or created
    else:
        raise ValidationError(
            "Supply either an existing product_id or the details of a new product.",
            details={"field": "product"},
        )

    jurisdiction_code = payload.jurisdiction_code or principal.jurisdiction_code
    jurisdiction_name = payload.jurisdiction_name or principal.user.jurisdiction_name
    # An officer may only open an inspection inside their own scope.
    principal.scope.require(jurisdiction_code)

    inspection = await inspection_service.create_inspection(
        db,
        context,
        product=product,
        created_by_id=principal.id,
        jurisdiction_code=jurisdiction_code,
        jurisdiction_name=jurisdiction_name,
        inspection_date=payload.inspection_date,
        source=payload.source.value,
        premises_name=payload.premises_name,
        premises_address=payload.premises_address,
        marketplace_name=payload.marketplace_name,
        listing_url=payload.listing_url,
        batch_reference=payload.batch_reference,
        complaint_id=uuid.UUID(payload.complaint_id) if payload.complaint_id else None,
    )
    await db.commit()
    return await _build_detail(db, principal, inspection)


@router.get("/{inspection_id}", response_model=InspectionDetail, summary="Read one inspection")
async def read_inspection(
    inspection_id: uuid.UUID,
    principal: Principal = Depends(requires(Permission.INSPECTION_READ)),
    db: AsyncSession = Depends(get_db),
) -> InspectionDetail:
    inspection = await inspection_service.get_for_actor(db, inspection_id, principal.scope)
    return await _build_detail(db, principal, inspection)


async def _build_detail(
    db: AsyncSession, principal: Principal, inspection: Inspection
) -> InspectionDetail:
    # Columns with a server-side onupdate (updated_at) are expired after a flush, and
    # reading an expired attribute would attempt a lazy load inside an async session.
    # One refresh here keeps every mutating route safe.
    await db.refresh(inspection)

    product = await load_product(db, inspection.product_id)
    if product is None:
        raise NotFoundError("The product record for this inspection is missing.")

    summary = await _summary_for(db, inspection, product)

    evidence_rows = list(
        await db.scalars(
            select(Evidence)
            .where(Evidence.inspection_id == inspection.id)
            .order_by(Evidence.face.asc(), Evidence.sequence.asc())
        )
    )
    evidence_out: list[EvidenceSummary] = []
    for row in evidence_rows:
        item = _evidence_summary(row)
        item.thumbnail_url = await evidence_service.view_url(db, row, prefer=EvidenceKind.THUMBNAIL)
        item.view_url = await storage.signed_url(
            logical_bucket=storage.Bucket.ORIGINALS, key=row.storage_key
        )
        evidence_out.append(item)

    candidates = list(
        await db.scalars(
            select(DeclarationCandidate)
            .where(DeclarationCandidate.inspection_id == inspection.id)
            .order_by(
                DeclarationCandidate.declaration_type.asc(),
                DeclarationCandidate.machine_confidence.desc().nullslast(),
            )
        )
    )

    finding_rows = list(
        await db.scalars(
            select(Finding)
            .where(Finding.inspection_id == inspection.id, Finding.is_current.is_(True))
            .order_by(Finding.created_at.asc())
        )
    )
    findings_out: list[FindingOut] = []
    for finding in finding_rows:
        rule = await db.get(RuleVersion, finding.rule_version_id)
        findings_out.append(_finding_out(finding, rule))

    reports = list(
        await db.scalars(
            select(Report)
            .where(Report.inspection_id == inspection.id)
            .order_by(Report.revision.desc())
        )
    )

    history = list(
        await db.scalars(
            select(StateTransition)
            .where(
                StateTransition.entity_type == "inspection",
                StateTransition.entity_id == inspection.id,
            )
            .order_by(StateTransition.occurred_at.asc())
        )
    )

    return InspectionDetail(
        **summary.model_dump(),
        decision_note=inspection.decision_note,
        decided_at=inspection.decided_at,
        decided_by_id=str(inspection.decided_by_id) if inspection.decided_by_id else None,
        assigned_officer_id=(
            str(inspection.assigned_officer_id) if inspection.assigned_officer_id else None
        ),
        reviewer_id=str(inspection.reviewer_id) if inspection.reviewer_id else None,
        checks_executed_at=inspection.checks_executed_at,
        rules_evaluated_count=inspection.rules_evaluated_count,
        listing_url=inspection.listing_url,
        marketplace_name=inspection.marketplace_name,
        batch_reference=inspection.batch_reference,
        premises_address=inspection.premises_address,
        context=inspection.context or {},
        coverage=await inspection_service.coverage_summary(db, inspection.id),
        evidence=evidence_out,
        candidates=[_candidate_out(item) for item in candidates],
        findings=findings_out,
        findings_summary=summarise_outcomes(finding_rows),
        available_transitions=await inspection_service.available_transitions(
            db, inspection, principal.role
        ),
        jobs=await jobs.jobs_for_inspection(db, inspection.id),
        reports=[
            {
                "id": str(item.id),
                "reference": item.reference,
                "revision": item.revision,
                "state": item.state,
                "issued_at": item.issued_at,
                "snapshot_sha256": item.snapshot_sha256,
                "verification_code": item.verification_code,
                "formats": [document.format for document in item.documents],
            }
            for item in reports
        ],
        timeline=[
            {
                "at": item.occurred_at,
                "kind": "state_change",
                "summary": (
                    f"{item.from_state or 'created'} to {item.to_state}"
                    if item.from_state
                    else f"opened as {item.to_state}"
                ),
                "actor_role": item.actor_role,
                "detail": {"reason": item.reason, "request_id": item.request_id},
            }
            for item in history
        ],
    )


@router.patch(
    "/{inspection_id}", response_model=InspectionDetail, summary="Update inspection context"
)
async def update_inspection(
    inspection_id: uuid.UUID,
    payload: InspectionUpdateRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.INSPECTION_UPDATE)),
    db: AsyncSession = Depends(get_db),
) -> InspectionDetail:
    inspection = await inspection_service.get_for_actor(db, inspection_id, principal.scope)
    await inspection_service.ensure_not_frozen(inspection)
    inspection_service.check_version(inspection, payload.expected_version)

    changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    if changes.get("assigned_officer_id"):
        principal.require(Permission.INSPECTION_ASSIGN)
        from ...models.user import User

        assignee = await db.get(User, uuid.UUID(changes["assigned_officer_id"]))
        if assignee is None or not assignee.is_active:
            raise ValidationError("That officer account was not found or is inactive.")
        principal.scope.require(assignee.jurisdiction_code)
        changes["assigned_officer_id"] = assignee.id

    before = {key: getattr(inspection, key, None) for key in changes}
    for field, value in changes.items():
        setattr(inspection, field, value)
    await db.flush()

    await audit_service.record(
        db,
        principal.audit_context(request),
        action="inspection.updated",
        entity_type="inspection",
        entity_id=inspection.id,
        entity_version=inspection.version,
        old_values={key: str(value) for key, value in before.items()},
        new_values={key: str(getattr(inspection, key, None)) for key in changes},
    )
    await db.commit()
    return await _build_detail(db, principal, inspection)


@router.post(
    "/{inspection_id}/transitions",
    response_model=InspectionDetail,
    summary="Change the inspection state",
)
async def transition_inspection(
    inspection_id: uuid.UUID,
    payload: TransitionRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.INSPECTION_TRANSITION)),
    db: AsyncSession = Depends(get_db),
) -> InspectionDetail:
    inspection = await inspection_service.get_for_actor(db, inspection_id, principal.scope)
    await inspection_service.transition(
        db,
        principal.audit_context(request),
        inspection=inspection,
        target_state=payload.target_state,
        role=principal.role,
        actor_id=principal.id,
        expected_version=payload.expected_version,
        reason=payload.reason,
    )
    await db.commit()
    return await _build_detail(db, principal, inspection)


@router.post(
    "/{inspection_id}/decision",
    response_model=InspectionDetail,
    summary="Record the reasoned decision",
)
async def record_decision(
    inspection_id: uuid.UUID,
    payload: DecisionRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.INSPECTION_DECIDE)),
    db: AsyncSession = Depends(get_db),
) -> InspectionDetail:
    """Blocked until every candidate is reviewed and the checks have been run.

    Those preconditions are guards on the state machine edge, so they cannot be
    bypassed by calling this endpoint directly.
    """
    inspection = await inspection_service.get_for_actor(db, inspection_id, principal.scope)
    await inspection_service.record_decision(
        db,
        principal.audit_context(request),
        inspection=inspection,
        decision=payload.decision,
        note=payload.note,
        role=principal.role,
        actor_id=principal.id,
        expected_version=payload.expected_version,
    )
    await db.commit()
    return await _build_detail(db, principal, inspection)


@router.post(
    "/{inspection_id}/faces",
    response_model=InspectionDetail,
    summary="Record the state of an expected package face",
)
async def set_face_state(
    inspection_id: uuid.UUID,
    payload: FaceStateRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.INSPECTION_UPDATE)),
    db: AsyncSession = Depends(get_db),
) -> InspectionDetail:
    inspection = await inspection_service.get_for_actor(db, inspection_id, principal.scope)
    await inspection_service.ensure_not_frozen(inspection)
    await inspection_service.set_face_state(
        db,
        principal.audit_context(request),
        inspection=inspection,
        face=payload.face.value,
        capture_state=payload.capture_state.value,
        reason=payload.reason,
        actor_id=principal.id,
    )
    await db.commit()
    return await _build_detail(db, principal, inspection)


@router.post(
    "/{inspection_id}/measurements",
    response_model=InspectionDetail,
    summary="Record character height measurements",
)
async def record_measurements(
    inspection_id: uuid.UUID,
    payload: MeasurementRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.INSPECTION_UPDATE)),
    db: AsyncSession = Depends(get_db),
) -> InspectionDetail:
    """Store officer-supplied character heights in millimetres.

    The character-height check refuses to guess a physical size from pixels, so this
    is how a height ever becomes testable.
    """
    inspection = await inspection_service.get_for_actor(db, inspection_id, principal.scope)
    await inspection_service.ensure_not_frozen(inspection)
    inspection_service.check_version(inspection, payload.expected_version)

    context = dict(inspection.context or {})
    measurements = dict(context.get("character_height_measurements", {}))
    for item in payload.measurements:
        measurements[item.declaration.value] = {
            "observed_mm": format(item.observed_mm, "f"),
            "uncertainty_mm": format(item.uncertainty_mm, "f"),
            "method": item.method,
            "recorded_by": str(principal.id),
            "recorded_at": datetime.now(UTC).isoformat(),
        }
    context["character_height_measurements"] = measurements
    inspection.context = json_safe(context)
    await db.flush()

    await audit_service.record(
        db,
        principal.audit_context(request),
        action="inspection.measurements_recorded",
        entity_type="inspection",
        entity_id=inspection.id,
        entity_version=inspection.version,
        new_values={"declarations": [item.declaration.value for item in payload.measurements]},
    )
    await db.commit()
    return await _build_detail(db, principal, inspection)


@router.post(
    "/{inspection_id}/checks",
    response_model=RunChecksResponse,
    summary="Run the legal checks",
)
async def run_checks(
    inspection_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(requires(Permission.CHECKS_RUN)),
    db: AsyncSession = Depends(get_db),
) -> RunChecksResponse:
    """Apply every approved, applicable rule version to the reviewed evidence."""
    inspection = await inspection_service.get_for_actor(db, inspection_id, principal.scope)
    await inspection_service.ensure_not_frozen(inspection)
    product = await load_product(db, inspection.product_id)
    if product is None:
        raise NotFoundError("The product record for this inspection is missing.")

    result = await evaluate_inspection(
        db, inspection=inspection, product=product, actor_id=principal.id
    )
    await audit_service.record(
        db,
        principal.audit_context(request),
        action="inspection.checks_run",
        entity_type="inspection",
        entity_id=inspection.id,
        entity_version=inspection.version,
        new_values={
            "rules_applied": result.rules_applied,
            "findings_written": result.findings_written,
            "outcomes": result.outcomes,
        },
    )
    await db.commit()
    return RunChecksResponse(**result.as_dict())


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------
@router.post(
    "/{inspection_id}/evidence",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=EvidenceUploadResponse,
    summary="Upload a package image",
)
async def upload_evidence(
    inspection_id: uuid.UUID,
    request: Request,
    face: str = Form(..., max_length=40),
    quality_override_reason: str | None = Form(default=None, max_length=2000),
    capture_client_time: datetime | None = Form(default=None),
    latitude: float | None = Form(default=None, ge=-90, le=90),
    longitude: float | None = Form(default=None, ge=-180, le=180),
    location_accuracy_m: float | None = Form(default=None, ge=0, le=100000),
    file: UploadFile = File(...),
    principal: Principal = Depends(requires(Permission.EVIDENCE_UPLOAD)),
    db: AsyncSession = Depends(get_db),
) -> EvidenceUploadResponse:
    """Store an original image and queue its analysis.

    Returns 202: the file is stored and hashed synchronously, OCR runs in the worker.
    """
    await enforce(EVIDENCE_UPLOAD_PER_USER, str(principal.id))
    settings = get_settings()

    inspection = await inspection_service.get_for_actor(db, inspection_id, principal.scope)

    from ...domain.enums import PackageFace

    if face not in {member.value for member in PackageFace}:
        raise ValidationError(
            "That package face is not one this system records.",
            details={"field": "face", "allowed": [member.value for member in PackageFace]},
        )

    # Read with a hard ceiling so an oversized upload cannot exhaust memory.
    payload = await file.read(settings.max_upload_bytes + 1)
    if len(payload) > settings.max_upload_bytes:
        raise PayloadTooLargeError(
            f"The image is larger than the {settings.max_upload_mb} MB limit.",
            details={"limit_bytes": settings.max_upload_bytes},
        )

    result = await evidence_service.store_evidence(
        db,
        principal.audit_context(request),
        inspection=inspection,
        face=face,
        filename=file.filename or "upload",
        supplied_mime=file.content_type,
        payload=payload,
        uploaded_by_id=principal.id,
        client_ip=request.client.host if request.client else None,
        capture_client_time=capture_client_time,
        latitude=latitude,
        longitude=longitude,
        location_accuracy_m=location_accuracy_m,
        quality_override_reason=quality_override_reason,
    )
    await db.commit()

    summary = _evidence_summary(result.evidence)
    summary.thumbnail_url = None
    summary.view_url = await storage.signed_url(
        logical_bucket=storage.Bucket.ORIGINALS, key=result.evidence.storage_key
    )

    message = "Evidence stored and queued for reading."
    if not result.queued:
        message = (
            "Evidence stored. The analysis queue is not reachable right now; the "
            "reading will be retried automatically."
        )
    elif result.quality.advisories:
        message = "Evidence stored and queued for reading. " + " ".join(
            item.message for item in result.quality.advisories
        )

    return EvidenceUploadResponse(
        evidence=summary,
        quality=result.quality.as_dict(),
        job_id=str(result.job_id) if result.job_id else None,
        queued=result.queued,
        message=message,
    )


@router.get(
    "/{inspection_id}/evidence/{evidence_id}/ocr",
    summary="Raw OCR output for one evidence file",
)
async def read_ocr(
    inspection_id: uuid.UUID,
    evidence_id: uuid.UUID,
    principal: Principal = Depends(requires(Permission.EVIDENCE_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Exactly what the machine read, so an officer can compare it with the image."""
    inspection = await inspection_service.get_for_actor(db, inspection_id, principal.scope)
    evidence = await evidence_service.get_for_inspection(
        db, evidence_id=evidence_id, inspection_id=inspection.id
    )
    result = await evidence_service.ocr_result_for(db, evidence.id)
    if result is None:
        return {
            "available": False,
            "analysis_state": evidence.analysis_state,
            "detail": "This image has not been read yet.",
        }
    return {
        "available": True,
        "profile": result.profile,
        "engine": result.engine,
        "engine_version": result.engine_version,
        "languages": result.languages,
        "detected_script": result.detected_script,
        "orientation_applied_degrees": result.orientation_applied_degrees,
        "mean_confidence": result.mean_confidence,
        "word_count": result.word_count,
        "duration_ms": result.duration_ms,
        "failure_reason": result.failure_reason,
        "raw_text": result.raw_text,
        "lines": result.lines,
        "words": result.words,
        "image": {
            "width": evidence.original_width,
            "height": evidence.original_height,
        },
    }


@router.get(
    "/{inspection_id}/evidence/{evidence_id}/integrity",
    summary="Re-verify the stored evidence hash",
)
async def verify_evidence(
    inspection_id: uuid.UUID,
    evidence_id: uuid.UUID,
    principal: Principal = Depends(requires(Permission.EVIDENCE_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    inspection = await inspection_service.get_for_actor(db, inspection_id, principal.scope)
    evidence = await evidence_service.get_for_inspection(
        db, evidence_id=evidence_id, inspection_id=inspection.id
    )
    return await evidence_service.verify_integrity(db, evidence)


@router.get(
    "/{inspection_id}/evidence/{evidence_id}/download",
    summary="Download the original evidence file",
)
async def download_evidence(
    inspection_id: uuid.UUID,
    evidence_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(requires(Permission.EVIDENCE_DOWNLOAD_ORIGINAL)),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Served through the API rather than a signed URL so every access is recorded."""
    inspection = await inspection_service.get_for_actor(db, inspection_id, principal.scope)
    evidence = await evidence_service.get_for_inspection(
        db, evidence_id=evidence_id, inspection_id=inspection.id
    )

    await audit_service.record(
        db,
        principal.audit_context(request),
        action="evidence.downloaded",
        entity_type="evidence",
        entity_id=evidence.id,
        new_values={"sha256": evidence.sha256, "inspection": inspection.reference},
    )
    await db.commit()

    payload = await storage.get_object(
        logical_bucket=storage.Bucket.ORIGINALS, key=evidence.storage_key
    )
    filename = f"{inspection.reference}-{evidence.face}-{evidence.sequence}"
    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
        evidence.detected_mime_type, ""
    )

    import io

    return StreamingResponse(
        io.BytesIO(payload),
        media_type=evidence.detected_mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}{extension}"',
            "X-Evidence-SHA256": evidence.sha256,
            "Cache-Control": "no-store",
        },
    )


@router.post(
    "/{inspection_id}/evidence/{evidence_id}/reanalyse",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue analysis again",
)
async def reanalyse_evidence(
    inspection_id: uuid.UUID,
    evidence_id: uuid.UUID,
    request: Request,
    reason: str = Query(min_length=5, max_length=500),
    principal: Principal = Depends(requires(Permission.EVIDENCE_REANALYSE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    inspection = await inspection_service.get_for_actor(db, inspection_id, principal.scope)
    await inspection_service.ensure_not_frozen(inspection)
    evidence = await evidence_service.get_for_inspection(
        db, evidence_id=evidence_id, inspection_id=inspection.id
    )
    job_id = await evidence_service.request_reanalysis(
        db,
        principal.audit_context(request),
        evidence=evidence,
        requested_by_id=principal.id,
        reason=reason,
    )
    await db.commit()
    return {
        "job_id": str(job_id),
        "message": (
            "Analysis queued again. Reviewed readings are kept; only unreviewed ones "
            "are replaced."
        ),
    }


# --------------------------------------------------------------------------
# Candidate review
# --------------------------------------------------------------------------
@candidate_router.patch(
    "/{candidate_id}", response_model=CandidateOut, summary="Review a machine reading"
)
async def review_candidate(
    candidate_id: uuid.UUID,
    payload: CandidateReviewRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.CANDIDATE_REVIEW)),
    db: AsyncSession = Depends(get_db),
) -> CandidateOut:
    """Confirm, correct, reject or set aside one machine reading.

    A correction never overwrites the machine reading: the previous state is written
    to ``candidate_revisions`` first, and the machine fields are left intact.
    """
    candidate = await db.get(DeclarationCandidate, candidate_id)
    if candidate is None:
        raise NotFoundError("That reading was not found.")

    inspection = await inspection_service.get_for_actor(
        db, candidate.inspection_id, principal.scope
    )
    await inspection_service.ensure_not_frozen(inspection)

    if candidate.version != payload.expected_version:
        raise StaleRecordError(
            "This reading changed after you loaded it. Reload and try again.",
            details={
                "your_version": payload.expected_version,
                "current_version": candidate.version,
            },
        )

    previous = {
        "review_state": candidate.review_state,
        "corrected_value": candidate.corrected_value,
        "correction_reason": candidate.correction_reason,
        "review_note": candidate.review_note,
    }

    if payload.review_state == ReviewState.CORRECTED:
        if not payload.corrected_text or not payload.corrected_text.strip():
            raise ValidationError(
                "Give the value as printed on the package.",
                details={"field": "corrected_text"},
            )
        if not payload.correction_reason or len(payload.correction_reason.strip()) < 5:
            raise ValidationError(
                "Give a reason for the correction. It is printed in the report.",
                details={"field": "correction_reason"},
            )
        parsed, numeric, unit = _parse_correction(
            candidate.declaration_type, payload.corrected_text.strip()
        )
        candidate.corrected_value = json_safe(parsed)
        candidate.corrected_numeric_value = numeric
        candidate.corrected_unit = unit
        candidate.correction_reason = payload.correction_reason.strip()
    elif payload.review_state == ReviewState.PENDING:
        raise ValidationError(
            "A reading cannot be returned to pending. Reject it instead.",
            details={"field": "review_state"},
        )
    else:
        if payload.review_state == ReviewState.REJECTED and not (
            payload.review_note and payload.review_note.strip()
        ):
            raise ValidationError(
                "Give a note explaining why this reading is rejected.",
                details={"field": "review_note"},
            )
        candidate.corrected_value = {}
        candidate.corrected_numeric_value = None
        candidate.corrected_unit = None
        candidate.correction_reason = None

    candidate.review_state = payload.review_state.value
    candidate.review_note = (payload.review_note or "").strip() or None
    candidate.reviewed_by_id = principal.id
    candidate.reviewed_at = datetime.now(UTC)

    revision_number = (
        int(
            await db.scalar(
                select(func.count())
                .select_from(CandidateRevision)
                .where(CandidateRevision.candidate_id == candidate.id)
            )
            or 0
        )
        + 1
    )
    db.add(
        CandidateRevision(
            id=uuid.uuid4(),
            candidate_id=candidate.id,
            revision_number=revision_number,
            previous_state=json_safe(previous),
            new_state=json_safe(
                {
                    "review_state": candidate.review_state,
                    "corrected_value": candidate.corrected_value,
                    "correction_reason": candidate.correction_reason,
                    "review_note": candidate.review_note,
                }
            ),
            change_kind=f"review.{payload.review_state.value}",
            reason=candidate.correction_reason or candidate.review_note,
            actor_id=principal.id,
            actor_role=principal.role.value,
            request_id=current_request_id(),
            recorded_at=datetime.now(UTC),
        )
    )

    # A change to the inputs invalidates the previous check run.
    inspection.checks_executed_at = None
    await db.flush()

    await audit_service.record(
        db,
        principal.audit_context(request),
        action="candidate.reviewed",
        entity_type="declaration_candidate",
        entity_id=candidate.id,
        entity_version=candidate.version,
        old_values=previous,
        new_values={
            "review_state": candidate.review_state,
            "declaration_type": candidate.declaration_type,
            "corrected_value": candidate.corrected_value,
        },
        reason=candidate.correction_reason or candidate.review_note,
    )
    await db.commit()
    await db.refresh(candidate)
    return _candidate_out(candidate)


def _parse_correction(
    declaration_type: str, text: str
) -> tuple[dict[str, Any], Decimal | None, str | None]:
    """Parse an officer-supplied value with the same parser used for OCR output.

    Using one parser for both means a corrected value and a machine value are
    directly comparable, and the rule engine cannot behave differently depending on
    which produced the input.
    """
    try:
        declaration = DeclarationType(declaration_type)
    except ValueError as exc:
        raise ValidationError("Unknown declaration type.") from exc

    spec = SPECS_BY_TYPE.get(declaration)
    if spec is None:
        return ({"kind": "text", "value": text, "display": text}, None, None)

    parsed = spec.parser(text)
    if parsed is None:
        raise ValidationError(
            f"{text!r} could not be read as a {declaration.value.replace('_', ' ')}. "
            "Enter it as printed on the package.",
            details={"field": "corrected_text"},
        )

    numeric: Decimal | None = None
    unit: str | None = None
    if parsed.get("kind") == "money":
        numeric = Decimal(str(parsed["amount"]))
        unit = str(parsed.get("currency"))
    elif parsed.get("kind") == "quantity":
        numeric = Decimal(str(parsed["base_amount"]))
        unit = str(parsed.get("base_unit"))
    return parsed, numeric, unit


@candidate_router.get("/{candidate_id}/history", summary="Every change made to a machine reading")
async def candidate_history(
    candidate_id: uuid.UUID,
    principal: Principal = Depends(requires(Permission.CANDIDATE_REVIEW)),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    candidate = await db.get(DeclarationCandidate, candidate_id)
    if candidate is None:
        raise NotFoundError("That reading was not found.")
    await inspection_service.get_for_actor(db, candidate.inspection_id, principal.scope)

    rows = list(
        await db.scalars(
            select(CandidateRevision)
            .where(CandidateRevision.candidate_id == candidate_id)
            .order_by(CandidateRevision.revision_number.asc())
        )
    )
    return [
        {
            "revision_number": row.revision_number,
            "change_kind": row.change_kind,
            "previous_state": row.previous_state,
            "new_state": row.new_state,
            "reason": row.reason,
            "actor_id": str(row.actor_id) if row.actor_id else None,
            "actor_role": row.actor_role,
            "recorded_at": row.recorded_at,
        }
        for row in rows
    ]


# --------------------------------------------------------------------------
# Finding override
# --------------------------------------------------------------------------
@finding_router.patch(
    "/{finding_id}", response_model=FindingOut, summary="Override a deterministic outcome"
)
async def override_finding(
    finding_id: uuid.UUID,
    payload: FindingOverrideRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.FINDING_OVERRIDE)),
    db: AsyncSession = Depends(get_db),
) -> FindingOut:
    """Record a reviewer's departure from the deterministic outcome.

    The engine's outcome is never altered. The override sits alongside it, with a
    mandatory reason, and both appear in the report.
    """
    finding = await db.get(Finding, finding_id)
    if finding is None:
        raise NotFoundError("That finding was not found.")
    inspection = await inspection_service.get_for_actor(db, finding.inspection_id, principal.scope)
    await inspection_service.ensure_not_frozen(inspection)

    if finding.version != payload.expected_version:
        raise StaleRecordError(
            "This finding changed after you loaded it. Reload and try again.",
            details={"your_version": payload.expected_version, "current_version": finding.version},
        )
    if not finding.is_current:
        raise ConflictError(
            "This finding has been superseded by a later check run.",
            code="finding_superseded",
        )

    previous = {"officer_outcome": finding.officer_outcome, "officer_note": finding.officer_note}
    finding.officer_outcome = payload.officer_outcome
    finding.officer_note = payload.officer_note.strip()
    finding.officer_id = principal.id
    finding.officer_recorded_at = datetime.now(UTC)
    await db.flush()

    await audit_service.record(
        db,
        principal.audit_context(request),
        action="finding.overridden",
        entity_type="finding",
        entity_id=finding.id,
        entity_version=finding.version,
        old_values=previous,
        new_values={
            "officer_outcome": finding.officer_outcome,
            "engine_outcome": finding.outcome,
        },
        reason=finding.officer_note,
    )
    await db.commit()
    await db.refresh(finding)
    rule = await db.get(RuleVersion, finding.rule_version_id)
    return _finding_out(finding, rule)


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
@job_router.get("/{job_id}", summary="Analysis job progress")
async def read_job(
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Polled by the interface while an image is being read."""
    status_payload = await jobs.job_status(db, job_id)
    if status_payload is None:
        raise NotFoundError("That job was not found.")
    if status_payload.get("inspection_id"):
        await inspection_service.get_for_actor(
            db, uuid.UUID(status_payload["inspection_id"]), principal.scope
        )
    return status_payload
