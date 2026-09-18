"""Complaints: public intake and officer handling."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...config import get_settings
from ...db import get_db
from ...deps import Principal, public_audit_context, requires
from ...domain.enums import ComplaintState, InspectionSource
from ...errors import NotFoundError, PayloadTooLargeError, ValidationError
from ...models.complaint import Complaint, ComplaintAttachment
from ...models.inspection import Inspection, StateTransition
from ...models.user import User
from ...schemas.common import Page, PageNumber, PageSize
from ...schemas.matters import (
    ComplaintAssignRequest,
    ComplaintSubmission,
    ComplaintSubmissionResponse,
    ComplaintToInspectionRequest,
    ComplaintTransitionRequest,
    ComplaintTriageRequest,
)
from ...schemas.workspace import IdentifierInput, InspectionDetail, ProductCreateRequest
from ...security import Permission
from ...security.ratelimit import COMPLAINT_PER_IP, COMPLAINT_STATUS_PER_IP, enforce
from ...services import complaints as complaint_service
from ...services import inspections as inspection_service
from ...services import storage
from .inspections import _build_detail
from .products import create_product_record, load_product

router = APIRouter(prefix="/complaints", tags=["complaints"])
public_router = APIRouter(prefix="/public/complaints", tags=["public"])

MAX_ATTACHMENT_COUNT = 5


async def _load(db: AsyncSession, complaint_id: uuid.UUID) -> Complaint:
    complaint = await db.scalar(
        select(Complaint)
        .options(selectinload(Complaint.attachments))
        .where(Complaint.id == complaint_id)
        .execution_options(populate_existing=True)
    )
    if complaint is None:
        raise NotFoundError("That complaint was not found.")
    return complaint


# --------------------------------------------------------------------------
# Public intake
# --------------------------------------------------------------------------
@public_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ComplaintSubmissionResponse,
    summary="Submit a complaint (public)",
)
async def submit_complaint(
    request: Request,
    complaint: str = Form(
        ...,
        description="JSON object matching ComplaintSubmission.",
    ),
    package_images: list[UploadFile] = File(default_factory=list),
    purchase_proof: UploadFile | None = File(default=None),
    db: AsyncSession = Depends(get_db),
) -> ComplaintSubmissionResponse:
    """Accept a complaint from a member of the public.

    Rate limited per address and fails closed if the limiter is unavailable: this is
    the most exposed endpoint in the system.

    The form fields arrive as one JSON part plus file parts, so the same strict
    schema validation applies here as everywhere else rather than a looser
    form-field-per-value parse.
    """
    context = public_audit_context(request)
    await enforce(COMPLAINT_PER_IP, context.ip_address or "unknown")

    try:
        payload = ComplaintSubmission.model_validate(json.loads(complaint))
    except json.JSONDecodeError as exc:
        raise ValidationError(
            "The complaint details could not be read.", code="malformed_complaint_json"
        ) from exc
    except PydanticValidationError as exc:
        # The body arrives as a JSON part rather than a request body, so FastAPI's own
        # handler does not see it. Map it to the same 422 shape here, reporting the
        # field and the rule without echoing the submitted value.
        raise ValidationError(
            "Some values could not be accepted.",
            details={
                "fields": [
                    {
                        "field": ".".join(str(part) for part in error.get("loc", ())) or "body",
                        "problem": str(error.get("msg", "invalid"))[:200],
                        "type": str(error.get("type", "")),
                    }
                    for error in exc.errors()
                ]
            },
        ) from exc

    settings = get_settings()
    attachments: list[tuple[str, str | None, bytes, str]] = []

    files: list[tuple[UploadFile, str]] = [
        (item, "package_image") for item in package_images if item is not None
    ]
    if purchase_proof is not None:
        files.append((purchase_proof, "purchase_proof"))

    if len(files) > MAX_ATTACHMENT_COUNT:
        raise ValidationError(
            f"Attach no more than {MAX_ATTACHMENT_COUNT} files.",
            details={"field": "package_images"},
        )

    for upload, kind in files:
        data = await upload.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise PayloadTooLargeError(
                f"Each file must be under {settings.max_upload_mb} MB.",
                details={"filename": upload.filename},
            )
        if not data:
            continue
        attachments.append((upload.filename or "upload", upload.content_type, data, kind))

    result = await complaint_service.submit(
        db,
        context,
        product_name=payload.product_name,
        brand=payload.brand,
        barcode_value=payload.barcode_value,
        category=payload.category.value,
        description=payload.description,
        purchase_date=payload.purchase_date,
        seller_name=payload.seller_name,
        marketplace_name=payload.marketplace_name,
        listing_url=payload.listing_url,
        stated_mrp=payload.stated_mrp,
        stated_price_paid=payload.stated_price_paid,
        location_text=payload.location_text,
        contact_name=payload.contact_name,
        contact_email=str(payload.contact_email) if payload.contact_email else None,
        contact_phone=payload.contact_phone,
        consent_to_contact=payload.consent_to_contact,
        attachments=attachments,
    )
    await db.commit()

    has_contact = bool(result.complaint.contact_lookup_hash)
    return ComplaintSubmissionResponse(
        reference=result.complaint.reference,
        state=result.complaint.state,
        submitted_at=result.complaint.created_at,
        priority=result.complaint.priority,
        priority_reason=result.complaint.priority_reason,
        attachments_stored=len(result.attachments),
        status_lookup_available=has_contact,
        message=(
            "Your complaint has been recorded. Keep the reference above. "
            + (
                "You can check its progress using the reference and the contact detail "
                "you supplied."
                if has_contact
                else "Because you did not supply a contact detail, the progress page is "
                "not available for this complaint."
            )
        ),
    )


@public_router.get("/status", summary="Check a complaint's progress (public)")
async def complaint_status(
    request: Request,
    reference: str = Query(min_length=6, max_length=40),
    contact: str = Query(
        min_length=5,
        max_length=320,
        description="The email address or phone number supplied when the complaint was made.",
    ),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Requires the reference and the contact detail together.

    References are sequential, so a reference alone would let anyone enumerate
    complaints. The pair is required, and a mismatch gives the same answer as an
    unknown reference.
    """
    await enforce(COMPLAINT_STATUS_PER_IP, request.client.host if request.client else "unknown")
    result = await complaint_service.public_status(db, reference=reference, contact=contact)
    if result is None:
        return {
            "found": False,
            "detail": (
                "No complaint matches that reference and contact detail. Check both "
                "exactly as you entered them when submitting."
            ),
        }
    return {"found": True, **result}


# --------------------------------------------------------------------------
# Officer handling
# --------------------------------------------------------------------------
@router.get("", summary="Complaint queue")
async def list_complaints(
    page: PageNumber = Query(1),
    page_size: PageSize = Query(25),
    search: str = Query("", max_length=160),
    state: ComplaintState | None = Query(None),
    unassigned: bool = Query(False),
    mine: bool = Query(False),
    principal: Principal = Depends(requires(Permission.COMPLAINT_READ)),
    db: AsyncSession = Depends(get_db),
) -> Page[dict]:
    """Ordered by triage priority, then age.

    Unassigned complaints have no jurisdiction yet and are visible to anyone who can
    triage; once assigned, jurisdiction scoping applies.
    """
    conditions: list[Any] = []
    scope_filter = principal.scope.filter(Complaint.jurisdiction_code, allow_null=True)
    if scope_filter is not None:
        conditions.append(scope_filter)
    if search:
        pattern = f"%{search.lower()}%"
        conditions.append(
            or_(
                func.lower(Complaint.reference).like(pattern),
                func.lower(Complaint.product_name).like(pattern),
                func.lower(Complaint.brand).like(pattern),
                Complaint.barcode_value == search.strip(),
            )
        )
    if state is not None:
        conditions.append(Complaint.state == state.value)
    if unassigned:
        conditions.append(Complaint.assigned_officer_id.is_(None))
    if mine:
        conditions.append(Complaint.assigned_officer_id == principal.id)

    total = int(
        await db.scalar(select(func.count()).select_from(Complaint).where(*conditions)) or 0
    )
    rows = list(
        await db.scalars(
            select(Complaint)
            .options(selectinload(Complaint.attachments))
            .where(*conditions)
            .order_by(Complaint.priority.desc(), Complaint.created_at.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return Page.build(
        [complaint_service.summary(row) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/{complaint_id}", summary="Read one complaint")
async def read_complaint(
    complaint_id: uuid.UUID,
    principal: Principal = Depends(requires(Permission.COMPLAINT_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    complaint = await _load(db, complaint_id)
    principal.scope.require(complaint.jurisdiction_code)

    payload = complaint_service.detail(complaint)
    payload["available_transitions"] = await complaint_service.available_transitions(
        complaint, principal.role
    )
    linked = list(
        await db.scalars(
            select(Inspection.id, Inspection.reference).where(
                Inspection.complaint_id == complaint.id
            )
        )
    )
    payload["linked_inspections"] = [str(item) for item in linked]
    payload["timeline"] = [
        {
            "at": item.occurred_at,
            "kind": "state_change",
            "summary": f"{item.from_state or 'received'} to {item.to_state}",
            "actor_role": item.actor_role,
            "detail": {"reason": item.reason},
        }
        for item in await db.scalars(
            select(StateTransition)
            .where(
                StateTransition.entity_type == "complaint",
                StateTransition.entity_id == complaint.id,
            )
            .order_by(StateTransition.occurred_at.asc())
        )
    ]
    return payload


@router.get(
    "/{complaint_id}/attachments/{attachment_id}/view",
    summary="Signed link to a complaint attachment",
)
async def view_attachment(
    complaint_id: uuid.UUID,
    attachment_id: uuid.UUID,
    principal: Principal = Depends(requires(Permission.COMPLAINT_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    complaint = await _load(db, complaint_id)
    principal.scope.require(complaint.jurisdiction_code)
    attachment = await db.get(ComplaintAttachment, attachment_id)
    if attachment is None or attachment.complaint_id != complaint.id:
        raise NotFoundError("That attachment was not found.")
    url = await storage.signed_url(
        logical_bucket=storage.Bucket.ORIGINALS, key=attachment.storage_key
    )
    return {"url": url, "expires_in_seconds": get_settings().s3_signed_url_ttl_seconds}


@router.post("/{complaint_id}/triage", summary="Record triage decisions")
async def triage_complaint(
    complaint_id: uuid.UUID,
    payload: ComplaintTriageRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.COMPLAINT_TRIAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    complaint = await _load(db, complaint_id)
    principal.scope.require(complaint.jurisdiction_code)
    if payload.jurisdiction_code:
        principal.scope.require(payload.jurisdiction_code)

    await complaint_service.triage(
        db,
        principal.audit_context(request),
        complaint=complaint,
        jurisdiction_code=payload.jurisdiction_code,
        jurisdiction_name=payload.jurisdiction_name,
        priority=payload.priority,
        triage_note=payload.triage_note,
        duplicate_of_id=(uuid.UUID(payload.duplicate_of_id) if payload.duplicate_of_id else None),
        matched_product_id=(
            uuid.UUID(payload.matched_product_id) if payload.matched_product_id else None
        ),
        expected_version=payload.expected_version,
    )
    await db.commit()
    return complaint_service.detail(await _load(db, complaint_id))


@router.post("/{complaint_id}/assign", summary="Assign a complaint to an officer")
async def assign_complaint(
    complaint_id: uuid.UUID,
    payload: ComplaintAssignRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.COMPLAINT_ASSIGN)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    complaint = await _load(db, complaint_id)
    principal.scope.require(complaint.jurisdiction_code)

    officer = await db.get(User, uuid.UUID(payload.officer_id))
    if officer is None or not officer.is_active:
        raise ValidationError("That officer account was not found or is inactive.")
    principal.scope.require(officer.jurisdiction_code)

    await complaint_service.assign(
        db,
        principal.audit_context(request),
        complaint=complaint,
        officer_id=officer.id,
        officer_jurisdiction=officer.jurisdiction_code,
        expected_version=payload.expected_version,
    )
    await db.commit()
    return complaint_service.detail(await _load(db, complaint_id))


@router.post("/{complaint_id}/transitions", summary="Change the complaint state")
async def transition_complaint(
    complaint_id: uuid.UUID,
    payload: ComplaintTransitionRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.COMPLAINT_TRIAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    complaint = await _load(db, complaint_id)
    principal.scope.require(complaint.jurisdiction_code)
    await complaint_service.transition(
        db,
        principal.audit_context(request),
        complaint=complaint,
        target_state=payload.target_state.value,
        role=principal.role,
        actor_id=principal.id,
        reason=payload.reason,
        expected_version=payload.expected_version,
    )
    await db.commit()
    return complaint_service.detail(await _load(db, complaint_id))


@router.post(
    "/{complaint_id}/inspection",
    status_code=status.HTTP_201_CREATED,
    response_model=InspectionDetail,
    summary="Open an inspection from a complaint",
)
async def convert_to_inspection(
    complaint_id: uuid.UUID,
    payload: ComplaintToInspectionRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.COMPLAINT_CONVERT)),
    db: AsyncSession = Depends(get_db),
) -> InspectionDetail:
    """Turn a complaint into an inspection, carrying the consumer's evidence across.

    The complainant's photographs become inspection evidence with a recorded reason
    for accepting their quality, because a consumer photograph cannot be retaken to
    order.
    """
    complaint = await _load(db, complaint_id)
    principal.scope.require(complaint.jurisdiction_code)
    context = principal.audit_context(request)

    jurisdiction_code = (
        payload.jurisdiction_code or complaint.jurisdiction_code or principal.jurisdiction_code
    )
    principal.scope.require(jurisdiction_code)

    if payload.product_id:
        product = await load_product(db, uuid.UUID(payload.product_id))
        if product is None:
            raise NotFoundError("That product was not found.")
    else:
        created = await create_product_record(
            db,
            context,
            payload=ProductCreateRequest(
                brand=complaint.brand or "Not recorded",
                name=complaint.product_name,
                commodity_category=payload.commodity_category,
                identifiers=(
                    [IdentifierInput(scheme="gtin", value=complaint.barcode_value)]
                    if complaint.barcode_value and len(complaint.barcode_value) >= 8
                    else []
                ),
            ),
            created_by_id=principal.id,
        )
        await db.flush()
        product = await load_product(db, created.id) or created

    inspection = await inspection_service.create_inspection(
        db,
        context,
        product=product,
        created_by_id=principal.id,
        jurisdiction_code=jurisdiction_code,
        jurisdiction_name=complaint.jurisdiction_name or jurisdiction_code,
        inspection_date=payload.inspection_date,
        source=(
            InspectionSource.ECOMMERCE_LISTING.value
            if complaint.listing_url
            else InspectionSource.COMPLAINT.value
        ),
        premises_name=payload.premises_name or complaint.seller_name,
        premises_address=payload.premises_address,
        marketplace_name=complaint.marketplace_name,
        listing_url=complaint.listing_url,
        complaint_id=complaint.id,
        extra_context={
            "opened_from_complaint": complaint.reference,
            "consumer_reported_mrp": complaint.stated_mrp,
            "consumer_reported_price_paid": complaint.stated_price_paid,
        },
    )
    await db.flush()

    promoted: list[str] = []
    if payload.promote_attachments:
        for attachment in complaint.attachments:
            if attachment.attachment_kind != "package_image":
                continue
            try:
                evidence_id = await complaint_service.promote_attachment_to_evidence(
                    db,
                    context,
                    attachment=attachment,
                    inspection_id=inspection.id,
                    face=payload.attachment_face.value,
                    uploaded_by_id=principal.id,
                )
            except Exception as exc:  # noqa: BLE001 - one bad file must not block the inspection
                from ...observability import get_logger

                get_logger(__name__).warning(
                    "complaint_attachment_promotion_failed",
                    attachment_id=str(attachment.id),
                    error=type(exc).__name__,
                )
                continue
            promoted.append(str(evidence_id))

    # Move the complaint on, now that an inspection exists. The officer who opened it
    # is recorded as handling it; that is self-assignment, not assigning work to
    # someone else, so it needs no reviewer.
    if complaint.state == ComplaintState.RECEIVED:
        await complaint_service.transition(
            db,
            context,
            complaint=complaint,
            target_state=ComplaintState.TRIAGED.value,
            role=principal.role,
            actor_id=principal.id,
            reason="Triaged while opening an inspection.",
            expected_version=complaint.version,
        )
    if complaint.assigned_officer_id is None:
        complaint.assigned_officer_id = principal.id
        from datetime import UTC, datetime

        complaint.assigned_at = datetime.now(UTC)
        if not complaint.jurisdiction_code:
            complaint.jurisdiction_code = jurisdiction_code
            complaint.jurisdiction_name = complaint.jurisdiction_name or jurisdiction_code
        await db.flush()

    if complaint.state in {ComplaintState.TRIAGED, ComplaintState.ASSIGNED}:
        await complaint_service.transition(
            db,
            context,
            complaint=complaint,
            target_state=ComplaintState.INSPECTION_CREATED.value,
            role=principal.role,
            actor_id=principal.id,
            reason=f"Inspection {inspection.reference} opened.",
            expected_version=complaint.version,
        )

    from ...services import audit as audit_service

    await audit_service.record(
        db,
        context,
        action="complaint.converted_to_inspection",
        entity_type="complaint",
        entity_id=complaint.id,
        new_values={
            "inspection_reference": inspection.reference,
            "evidence_promoted": len(promoted),
        },
    )
    await db.commit()
    return await _build_detail(db, principal, inspection)
