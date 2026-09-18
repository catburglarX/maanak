"""Complaints.

A complaint arrives from a member of the public and must be handled without
requiring them to hold an account. Two consequences shape this module:

* the submission endpoint is the most exposed surface in the system, so it is rate
  limited, validated strictly, and stores what was submitted without ever rendering
  it back as markup;
* the status lookup needs the reference *and* the contact detail that was supplied,
  so a guessed reference alone reveals nothing.

Triage priority is a published table, not a model score. The mapping from category to
priority is in ``CATEGORY_PRIORITY`` and the reason is stored on the complaint, so an
officer can see why something is near the top of the queue and argue with it.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.enums import ComplaintCategory, ComplaintState, Role
from ..domain.states import COMPLAINT_MACHINE
from ..errors import (
    GuardFailedError,
    InvalidTransitionError,
    NotFoundError,
    StaleRecordError,
    ValidationError,
)
from ..models.complaint import Complaint, ComplaintAttachment
from ..models.inspection import StateTransition
from ..observability import current_request_id, get_logger
from ..security.permissions import JurisdictionScope, normalise_jurisdiction
from . import audit as audit_service
from . import imaging, references, storage
from .canonical import sha256_bytes

logger = get_logger(__name__)

#: Published triage table. Higher is handled sooner.
#:
#: Consumer safety outranks everything else, then matters where a consumer has
#: probably lost money, then labelling defects. These are operational priorities for
#: queue order only; they say nothing about the legal seriousness of a breach.
CATEGORY_PRIORITY: dict[str, tuple[int, str]] = {
    ComplaintCategory.UNSAFE_PRODUCT.value: (
        95,
        "Reported as unsafe, so it is handled ahead of other complaints.",
    ),
    ComplaintCategory.ALLERGEN_INFORMATION.value: (
        90,
        "Allergen information affects consumer safety.",
    ),
    ComplaintCategory.MRP_OVERCHARGE.value: (
        70,
        "The consumer reports paying more than the declared price.",
    ),
    ComplaintCategory.INCORRECT_QUANTITY.value: (
        65,
        "The consumer reports receiving less than the declared quantity.",
    ),
    ComplaintCategory.EXPIRED_OR_DATE_ISSUE.value: (
        60,
        "Date marking affects whether the commodity should have been on sale.",
    ),
    ComplaintCategory.MISSING_DECLARATION.value: (
        45,
        "A mandatory declaration is reported as missing.",
    ),
    ComplaintCategory.MISLEADING_CLAIM.value: (40, "A claim on the package is disputed."),
    ComplaintCategory.UNREADABLE_LABEL.value: (
        35,
        "The label is reported as unreadable.",
    ),
    ComplaintCategory.OTHER.value: (25, "No specific category was selected."),
}

MAX_ATTACHMENTS = 5
PRIVACY_NOTICE_VERSION = "2026-09"

_PHONE_DIGITS = re.compile(r"\D")


@dataclass
class SubmissionResult:
    complaint: Complaint
    attachments: list[ComplaintAttachment]


def contact_lookup_hash(*, email: str | None, phone: str | None) -> str | None:
    """Hash of the contact detail, used for status lookup without exposing it.

    Either identifier works. The value is normalised first so that the same person
    entering their number with or without spaces still matches.
    """
    if email:
        return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
    if phone:
        digits = _PHONE_DIGITS.sub("", phone)
        if len(digits) >= 10:
            return hashlib.sha256(digits[-10:].encode("utf-8")).hexdigest()
    return None


def triage_priority(category: str) -> tuple[int, str]:
    return CATEGORY_PRIORITY.get(category, (25, "No specific category was selected."))


async def submit(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    product_name: str,
    brand: str | None,
    barcode_value: str | None,
    category: str,
    description: str,
    purchase_date: date | None,
    seller_name: str | None,
    marketplace_name: str | None,
    listing_url: str | None,
    stated_mrp: str | None,
    stated_price_paid: str | None,
    location_text: str | None,
    contact_name: str | None,
    contact_email: str | None,
    contact_phone: str | None,
    consent_to_contact: bool,
    attachments: list[tuple[str, str | None, bytes, str]],
) -> SubmissionResult:
    """Record a public complaint and its attachments.

    ``attachments`` is a list of ``(filename, supplied_mime, payload, kind)``.
    """
    if len(attachments) > MAX_ATTACHMENTS:
        raise ValidationError(
            f"Attach no more than {MAX_ATTACHMENTS} files.",
            details={"field": "attachments"},
        )
    if consent_to_contact and not (contact_email or contact_phone):
        raise ValidationError(
            "You asked to be contacted, so please give an email address or a phone number.",
            details={"field": "contact_email"},
        )

    priority, reason = triage_priority(category)
    reference = await references.next_reference(db, references.COMPLAINT_PREFIX)
    now = datetime.now(UTC)

    complaint = Complaint(
        id=uuid.uuid4(),
        reference=reference,
        product_name=product_name.strip(),
        brand=(brand or "").strip() or None,
        barcode_value=(barcode_value or "").strip() or None,
        category=category,
        description=description.strip(),
        purchase_date=purchase_date,
        seller_name=(seller_name or "").strip() or None,
        marketplace_name=(marketplace_name or "").strip() or None,
        listing_url=(listing_url or "").strip() or None,
        stated_mrp=(stated_mrp or "").strip() or None,
        stated_price_paid=(stated_price_paid or "").strip() or None,
        location_text=(location_text or "").strip() or None,
        contact_name=(contact_name or "").strip() or None,
        contact_email=(contact_email or "").strip().lower() or None,
        contact_phone=(contact_phone or "").strip() or None,
        contact_lookup_hash=contact_lookup_hash(email=contact_email, phone=contact_phone),
        consent_to_contact=consent_to_contact,
        consent_recorded_at=now if consent_to_contact else None,
        privacy_notice_version=PRIVACY_NOTICE_VERSION,
        state=ComplaintState.RECEIVED,
        priority=priority,
        priority_reason=reason,
        submitted_ip=context.ip_address,
        submitted_user_agent=(context.user_agent or "")[:400] or None,
        submission_metadata={"request_id": context.request_id},
    )
    db.add(complaint)
    await db.flush()

    stored: list[ComplaintAttachment] = []
    for filename, supplied_mime, payload, kind in attachments:
        # Same validation as inspection evidence: type is read from the bytes.
        decoded = imaging.decode_image(payload, declared_mime=supplied_mime)
        attachment_id = uuid.uuid4()
        key = storage.complaint_attachment_key(
            complaint_reference=reference,
            attachment_id=attachment_id,
            extension=decoded.extension,
        )
        object_record = await storage.put_object(
            logical_bucket=storage.Bucket.ORIGINALS,
            key=key,
            data=payload,
            content_type=decoded.detected_mime,
            metadata={"complaint": reference, "kind": kind},
            overwrite=False,
        )
        row = ComplaintAttachment(
            id=attachment_id,
            complaint_id=complaint.id,
            attachment_kind=kind,
            original_filename=(filename or "upload")[:255],
            detected_mime_type=decoded.detected_mime,
            size_bytes=len(payload),
            sha256=sha256_bytes(payload),
            storage_bucket=object_record.bucket,
            storage_key=object_record.key,
            width=decoded.width,
            height=decoded.height,
        )
        db.add(row)
        stored.append(row)
    await db.flush()

    await audit_service.record(
        db,
        context,
        action="complaint.received",
        entity_type="complaint",
        entity_id=complaint.id,
        new_values={
            "reference": reference,
            "category": category,
            "priority": priority,
            "attachments": len(stored),
            "contact_supplied": bool(complaint.contact_lookup_hash),
        },
    )
    logger.info(
        "complaint_received",
        reference=reference,
        category=category,
        priority=priority,
        attachments=len(stored),
    )
    return SubmissionResult(complaint=complaint, attachments=stored)


async def public_status(db: AsyncSession, *, reference: str, contact: str) -> dict[str, Any] | None:
    """Status for a member of the public.

    Requires both the reference and the contact detail supplied at submission. A
    reference alone is not enough, because references are sequential and therefore
    guessable.
    """
    complaint = await db.scalar(
        select(Complaint).where(Complaint.reference == reference.strip().upper())
    )
    if complaint is None or complaint.contact_lookup_hash is None:
        return None

    supplied = contact_lookup_hash(
        email=contact if "@" in contact else None,
        phone=None if "@" in contact else contact,
    )
    if supplied is None or supplied != complaint.contact_lookup_hash:
        return None

    return {
        "reference": complaint.reference,
        "submitted_at": complaint.created_at,
        "state": complaint.state,
        "state_description": _state_description(complaint.state),
        "category": complaint.category,
        "product_name": complaint.product_name,
        "resolution_note": complaint.resolution_note if complaint.resolved_at else None,
        "resolved_at": complaint.resolved_at,
        "closed_at": complaint.closed_at,
        "note": (
            "Only the progress of your complaint is shown here. Details of any "
            "inspection that follows are not published."
        ),
    }


def _state_description(state: str) -> str:
    return {
        ComplaintState.RECEIVED.value: "Received and waiting to be triaged.",
        ComplaintState.TRIAGED.value: "Triaged and waiting to be assigned to an officer.",
        ComplaintState.DUPLICATE.value: "Recorded as a duplicate of an earlier complaint.",
        ComplaintState.ASSIGNED.value: "Assigned to an officer.",
        ComplaintState.INSPECTION_CREATED.value: "An inspection has been opened.",
        ComplaintState.ESCALATED.value: "Escalated for further attention.",
        ComplaintState.RESOLVED.value: "Resolved.",
        ComplaintState.REJECTED.value: "Not taken forward.",
        ComplaintState.CLOSED.value: "Closed.",
    }.get(state, state.replace("_", " ").capitalize())


async def get_for_actor(
    db: AsyncSession, complaint_id: uuid.UUID, scope: JurisdictionScope
) -> Complaint:
    complaint = await db.get(Complaint, complaint_id)
    if complaint is None:
        raise NotFoundError("That complaint was not found.")
    # An unassigned complaint has no jurisdiction yet and is visible to anyone who
    # can triage; once assigned, normal scoping applies.
    scope.require(complaint.jurisdiction_code)
    return complaint


async def triage(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    complaint: Complaint,
    jurisdiction_code: str | None,
    jurisdiction_name: str | None,
    priority: int | None,
    triage_note: str | None,
    duplicate_of_id: uuid.UUID | None,
    matched_product_id: uuid.UUID | None,
    expected_version: int,
) -> Complaint:
    """Record triage decisions without changing state."""
    if complaint.version != expected_version:
        raise StaleRecordError(
            "This complaint changed after you loaded it. Reload and try again.",
            details={"your_version": expected_version, "current_version": complaint.version},
        )

    before = {
        "jurisdiction_code": complaint.jurisdiction_code,
        "priority": complaint.priority,
        "duplicate_of_id": str(complaint.duplicate_of_id) if complaint.duplicate_of_id else None,
    }

    if jurisdiction_code:
        complaint.jurisdiction_code = normalise_jurisdiction(jurisdiction_code)
        complaint.jurisdiction_name = jurisdiction_name or complaint.jurisdiction_code
    if priority is not None:
        complaint.priority = priority
        complaint.priority_reason = "Priority set manually during triage."
    if triage_note:
        complaint.triage_note = triage_note.strip()
    if duplicate_of_id:
        if duplicate_of_id == complaint.id:
            raise ValidationError("A complaint cannot be a duplicate of itself.")
        original = await db.get(Complaint, duplicate_of_id)
        if original is None:
            raise NotFoundError("The complaint it duplicates was not found.")
        complaint.duplicate_of_id = duplicate_of_id
    if matched_product_id:
        complaint.matched_product_id = matched_product_id

    await db.flush()
    await audit_service.record(
        db,
        context,
        action="complaint.triaged",
        entity_type="complaint",
        entity_id=complaint.id,
        entity_version=complaint.version,
        old_values=before,
        new_values={
            "jurisdiction_code": complaint.jurisdiction_code,
            "priority": complaint.priority,
            "duplicate_of_id": (
                str(complaint.duplicate_of_id) if complaint.duplicate_of_id else None
            ),
        },
        reason=triage_note,
    )
    return complaint


async def assign(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    complaint: Complaint,
    officer_id: uuid.UUID,
    officer_jurisdiction: str,
    expected_version: int,
) -> Complaint:
    if complaint.version != expected_version:
        raise StaleRecordError("This complaint changed after you loaded it. Reload and try again.")
    complaint.assigned_officer_id = officer_id
    complaint.assigned_at = datetime.now(UTC)
    if not complaint.jurisdiction_code:
        complaint.jurisdiction_code = officer_jurisdiction
        complaint.jurisdiction_name = officer_jurisdiction
    await db.flush()
    await audit_service.record(
        db,
        context,
        action="complaint.assigned",
        entity_type="complaint",
        entity_id=complaint.id,
        entity_version=complaint.version,
        new_values={"assigned_officer_id": str(officer_id)},
    )
    return complaint


async def transition(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    complaint: Complaint,
    target_state: str,
    role: Role,
    actor_id: uuid.UUID,
    reason: str | None,
    expected_version: int,
) -> Complaint:
    """Move a complaint through its state machine."""
    if complaint.version != expected_version:
        raise StaleRecordError(
            "This complaint changed after you loaded it. Reload and try again.",
            details={"your_version": expected_version, "current_version": complaint.version},
        )

    edge = COMPLAINT_MACHINE.find(complaint.state, target_state)
    if edge is None:
        raise InvalidTransitionError(
            f"A complaint in state {complaint.state!r} cannot move to {target_state!r}.",
            details={
                "current_state": complaint.state,
                "allowed_states": list(COMPLAINT_MACHINE.targets_from(complaint.state)),
            },
        )
    if role not in edge.roles:
        from ..errors import PermissionDeniedError

        raise PermissionDeniedError(
            f"Your role cannot {edge.label.lower()}.",
            details={"required_roles": sorted(item.value for item in edge.roles)},
        )
    if edge.reason_required and not (reason and reason.strip()):
        raise ValidationError(
            f"A reason is required to {edge.label.lower()}.", details={"field": "reason"}
        )

    for guard in edge.guards:
        if guard == "assignee_present" and complaint.assigned_officer_id is None:
            raise GuardFailedError(
                "Assign an officer before moving the complaint to assigned.",
                code="assignee_required",
            )
        if guard == "inspection_linked":
            from ..models.inspection import Inspection

            linked = await db.scalar(
                select(func.count())
                .select_from(Inspection)
                .where(Inspection.complaint_id == complaint.id)
            )
            if not linked:
                raise GuardFailedError(
                    "Open an inspection from this complaint first.",
                    code="inspection_required",
                )

    previous = complaint.state
    complaint.state = target_state
    now = datetime.now(UTC)
    if target_state == ComplaintState.RESOLVED:
        complaint.resolved_at = now
        complaint.resolution_note = (reason or "").strip() or complaint.resolution_note
    if target_state == ComplaintState.CLOSED:
        complaint.closed_at = now
        complaint.closure_reason = (reason or "").strip()[:240] or complaint.closure_reason
    await db.flush()

    db.add(
        StateTransition(
            id=uuid.uuid4(),
            entity_type="complaint",
            entity_id=complaint.id,
            from_state=previous,
            to_state=target_state,
            actor_id=actor_id,
            actor_role=role.value,
            jurisdiction_code=complaint.jurisdiction_code,
            reason=reason,
            request_id=current_request_id(),
            entity_version=complaint.version,
            occurred_at=now,
        )
    )
    await db.flush()

    await audit_service.record(
        db,
        context,
        action="complaint.state_changed",
        entity_type="complaint",
        entity_id=complaint.id,
        entity_version=complaint.version,
        old_values={"state": previous},
        new_values={"state": target_state},
        reason=reason,
    )
    return complaint


async def available_transitions(complaint: Complaint, role: Role) -> list[dict[str, Any]]:
    return [
        {
            "target_state": item.target,
            "label": item.label,
            "reason_required": item.reason_required,
            "blocked_by": (
                ["Assign an officer first."]
                if "assignee_present" in item.guards and complaint.assigned_officer_id is None
                else []
            ),
        }
        for item in COMPLAINT_MACHINE.allowed_for(complaint.state, role)
    ]


async def promote_attachment_to_evidence(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    attachment: ComplaintAttachment,
    inspection_id: uuid.UUID,
    face: str,
    uploaded_by_id: uuid.UUID,
) -> uuid.UUID:
    """Copy a complaint attachment into an inspection as evidence.

    The bytes are re-read from storage and re-hashed rather than trusting the stored
    hash, so the evidence record is established independently.
    """
    from ..models.inspection import Inspection
    from .evidence import store_evidence

    inspection = await db.get(Inspection, inspection_id)
    if inspection is None:
        raise NotFoundError("That inspection was not found.")

    payload = await storage.get_object(
        logical_bucket=storage.Bucket.ORIGINALS, key=attachment.storage_key
    )
    result = await store_evidence(
        db,
        context,
        inspection=inspection,
        face=face,
        filename=attachment.original_filename,
        supplied_mime=attachment.detected_mime_type,
        payload=payload,
        uploaded_by_id=uploaded_by_id,
        client_ip=context.ip_address,
        quality_override_reason=(
            "Supplied by the complainant with the complaint. Retained as submitted, "
            "because a consumer photograph cannot be retaken."
        ),
    )
    attachment.promoted_evidence_id = result.evidence.id
    await db.flush()

    await audit_service.record(
        db,
        context,
        action="complaint.attachment_promoted",
        entity_type="complaint_attachment",
        entity_id=attachment.id,
        new_values={
            "evidence_id": str(result.evidence.id),
            "inspection_id": str(inspection_id),
        },
    )
    return result.evidence.id


def summary(complaint: Complaint) -> dict[str, Any]:
    return {
        "id": str(complaint.id),
        "version": complaint.version,
        "reference": complaint.reference,
        "state": complaint.state,
        "state_description": _state_description(complaint.state),
        "category": complaint.category,
        "priority": complaint.priority,
        "priority_reason": complaint.priority_reason,
        "product_name": complaint.product_name,
        "brand": complaint.brand,
        "barcode_value": complaint.barcode_value,
        "jurisdiction_code": complaint.jurisdiction_code,
        "jurisdiction_name": complaint.jurisdiction_name,
        "assigned_officer_id": (
            str(complaint.assigned_officer_id) if complaint.assigned_officer_id else None
        ),
        "created_at": complaint.created_at,
        "purchase_date": complaint.purchase_date,
        "marketplace_name": complaint.marketplace_name,
        "seller_name": complaint.seller_name,
        "attachment_count": len(complaint.attachments),
        "has_contact": bool(complaint.contact_lookup_hash),
        "consent_to_contact": complaint.consent_to_contact,
    }


def detail(complaint: Complaint) -> dict[str, Any]:
    return {
        **summary(complaint),
        "description": complaint.description,
        "stated_mrp": complaint.stated_mrp,
        "stated_price_paid": complaint.stated_price_paid,
        "location_text": complaint.location_text,
        "listing_url": complaint.listing_url,
        "triage_note": complaint.triage_note,
        "resolution_note": complaint.resolution_note,
        "closure_reason": complaint.closure_reason,
        "resolved_at": complaint.resolved_at,
        "closed_at": complaint.closed_at,
        "duplicate_of_id": (str(complaint.duplicate_of_id) if complaint.duplicate_of_id else None),
        "matched_product_id": (
            str(complaint.matched_product_id) if complaint.matched_product_id else None
        ),
        "privacy_notice_version": complaint.privacy_notice_version,
        # Contact details are shown to officers who can act on them, never publicly.
        "contact_name": complaint.contact_name,
        "contact_email": complaint.contact_email,
        "contact_phone": complaint.contact_phone,
        "attachments": [
            {
                "id": str(item.id),
                "kind": item.attachment_kind,
                "filename": item.original_filename,
                "mime_type": item.detected_mime_type,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "promoted_evidence_id": (
                    str(item.promoted_evidence_id) if item.promoted_evidence_id else None
                ),
            }
            for item in complaint.attachments
        ],
    }
