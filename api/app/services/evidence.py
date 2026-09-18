"""Evidence intake.

The upload path is deliberately strict and deliberately fast:

* the file is validated from its own bytes before anything else touches it;
* quality is measured in the request so the officer gets an immediate, specific
  instruction ("strong glare over the panel, photograph it again") rather than
  discovering the problem minutes later;
* the original bytes are written to object storage unchanged and hashed;
* OCR is queued, not run here.

Once written, an evidence row's file identity is immutable. A database trigger
enforces that, so a bug in this module cannot silently repoint a record at
different bytes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.enums import AnalysisState, EvidenceKind, FaceCaptureState, InspectionState
from ..errors import ConflictError, NotFoundError, ValidationError
from ..models.evidence import Evidence, EvidenceDerivative, OcrResult
from ..models.inspection import Inspection, InspectionFace
from ..observability import get_logger
from . import audit as audit_service
from . import imaging, jobs, storage
from .canonical import json_safe, sha256_bytes

logger = get_logger(__name__)

#: An officer may proceed past a blocking quality signal, but only with a reason.
#: The reason is stored on the evidence row and printed in the report, so the
#: decision to rely on a poor image is visible rather than hidden.
QUALITY_OVERRIDE_MIN_REASON = 15


@dataclass
class IntakeResult:
    evidence: Evidence
    quality: imaging.QualityReport
    job_id: uuid.UUID | None
    queued: bool
    duplicate_of: Evidence | None = None


async def store_evidence(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    inspection: Inspection,
    face: str,
    filename: str,
    supplied_mime: str | None,
    payload: bytes,
    uploaded_by_id: uuid.UUID,
    client_ip: str | None,
    capture_client_time: datetime | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    location_accuracy_m: float | None = None,
    quality_override_reason: str | None = None,
) -> IntakeResult:
    """Validate, store and queue one evidence file."""
    if inspection.is_frozen:
        raise ConflictError(
            "This inspection is closed to changes because a report has been issued.",
            code="inspection_frozen",
        )

    # 1. Validate from the bytes. Raises 413, 415 or 422 with a specific reason.
    decoded = imaging.decode_image(payload, declared_mime=supplied_mime)
    digest = sha256_bytes(payload)

    # 2. An identical file already on this inspection is a re-upload, not new
    #    evidence. Reported rather than silently accepted or silently dropped.
    duplicate = await db.scalar(
        select(Evidence).where(Evidence.inspection_id == inspection.id, Evidence.sha256 == digest)
    )
    if duplicate is not None:
        raise ConflictError(
            "This exact image has already been uploaded for this inspection "
            f"(as {duplicate.face.replace('_', ' ')}). Upload a different photograph.",
            code="duplicate_evidence",
            details={"existing_evidence_id": str(duplicate.id), "sha256": digest},
        )

    # 3. Measure quality now so the officer can act immediately.
    quality = imaging.analyse_quality(decoded)
    if quality.requires_recapture:
        if not quality_override_reason:
            raise ValidationError(
                "This image is not good enough to read reliably. "
                + " ".join(item.message for item in quality.blocking),
                code="evidence_quality_insufficient",
                details={
                    "quality": quality.as_dict(),
                    "override_field": "quality_override_reason",
                    "override_note": (
                        "If this is the best image obtainable, resubmit with a reason. "
                        "The reason is stored with the evidence and printed in the report."
                    ),
                },
            )
        if len(quality_override_reason.strip()) < QUALITY_OVERRIDE_MIN_REASON:
            raise ValidationError(
                "Explain in at least "
                f"{QUALITY_OVERRIDE_MIN_REASON} characters why this image must be used "
                "despite its quality.",
                details={"field": "quality_override_reason"},
            )

    sequence = (
        int(
            await db.scalar(
                select(func.count())
                .select_from(Evidence)
                .where(Evidence.inspection_id == inspection.id, Evidence.face == face)
            )
            or 0
        )
        + 1
    )

    evidence_id = uuid.uuid4()
    key = storage.evidence_key(
        inspection_reference=inspection.reference,
        evidence_id=evidence_id,
        extension=decoded.extension,
    )

    # 4. Write the original. Never modified after this point.
    stored = await storage.put_object(
        logical_bucket=storage.Bucket.ORIGINALS,
        key=key,
        data=payload,
        content_type=decoded.detected_mime,
        metadata={
            "inspection": inspection.reference,
            "evidence-id": str(evidence_id),
            "face": face,
            "sha256": digest,
        },
        overwrite=False,
    )

    evidence = Evidence(
        id=evidence_id,
        inspection_id=inspection.id,
        complaint_id=inspection.complaint_id,
        face=face,
        sequence=sequence,
        original_filename=(filename or "upload")[:255],
        supplied_mime_type=(supplied_mime or "")[:120] or None,
        detected_mime_type=decoded.detected_mime,
        size_bytes=len(payload),
        hash_algorithm="sha256",
        sha256=digest,
        original_width=decoded.width,
        original_height=decoded.height,
        stored_width=decoded.width,
        stored_height=decoded.height,
        storage_bucket=stored.bucket,
        storage_key=stored.key,
        storage_version_id=stored.version_id,
        storage_encryption=stored.encryption,
        uploaded_by_id=uploaded_by_id,
        uploaded_by_public=False,
        upload_client_time=capture_client_time,
        server_received_at=datetime.now(UTC),
        upload_ip=client_ip,
        device_make=str(decoded.exif.get("Make", ""))[:80] or None,
        device_model=str(decoded.exif.get("Model", ""))[:120] or None,
        capture_latitude=latitude,
        capture_longitude=longitude,
        capture_accuracy_m=location_accuracy_m,
        exif_handling="preserved_in_record",
        exif_summary=json_safe(decoded.exif),
        analysis_state=AnalysisState.PENDING,
        quality=json_safe(quality.as_dict()),
        quality_accepted_by_id=uploaded_by_id if quality_override_reason else None,
        quality_override_reason=(
            quality_override_reason.strip() if quality_override_reason else None
        ),
        barcodes=[],
        malware_scan_state="not_scanned",
        retention_state="active",
    )
    db.add(evidence)
    await db.flush()

    # 5. Mark the face captured.
    face_row = await db.scalar(
        select(InspectionFace).where(
            InspectionFace.inspection_id == inspection.id, InspectionFace.face == face
        )
    )
    if face_row is None:
        face_row = InspectionFace(
            id=uuid.uuid4(),
            inspection_id=inspection.id,
            face=face,
            capture_state=FaceCaptureState.CAPTURED,
            is_required=False,
            updated_by_id=uploaded_by_id,
        )
        db.add(face_row)
    else:
        face_row.capture_state = FaceCaptureState.CAPTURED
        face_row.updated_by_id = uploaded_by_id
    await db.flush()

    # 6. Queue the analysis and record that the inspection is processing.
    enqueued = await jobs.enqueue(
        db,
        job_type=jobs.JOB_ANALYSE_EVIDENCE,
        idempotency_key=jobs.evidence_analysis_key(evidence.id),
        evidence_id=evidence.id,
        inspection_id=inspection.id,
        payload={"face": face, "sha256": digest},
        requested_by_id=uploaded_by_id,
    )
    evidence.analysis_state = (
        AnalysisState.QUEUED if enqueued.queued_to_redis else AnalysisState.PENDING
    )

    from .inspections import apply_transition_unchecked

    if inspection.state in {InspectionState.DRAFT, InspectionState.EVIDENCE_PENDING}:
        if inspection.state == InspectionState.DRAFT:
            await apply_transition_unchecked(
                db,
                inspection=inspection,
                target=InspectionState.EVIDENCE_PENDING,
                reason="First evidence received.",
                actor_id=uploaded_by_id,
                actor_role=context.actor_role or "system",
            )
        await apply_transition_unchecked(
            db,
            inspection=inspection,
            target=InspectionState.PROCESSING,
            reason="Evidence submitted for analysis.",
            actor_id=uploaded_by_id,
            actor_role=context.actor_role or "system",
        )

    await audit_service.record(
        db,
        context,
        action="evidence.uploaded",
        entity_type="evidence",
        entity_id=evidence.id,
        new_values={
            "inspection": inspection.reference,
            "face": face,
            "sha256": digest,
            "size_bytes": len(payload),
            "detected_mime_type": decoded.detected_mime,
            "quality_verdict": quality.verdict,
            "quality_override_reason": evidence.quality_override_reason,
            "storage_key": stored.key,
        },
    )
    logger.info(
        "evidence_stored",
        inspection=inspection.reference,
        evidence_id=str(evidence.id),
        face=face,
        sha256=digest[:12],
        quality_verdict=quality.verdict,
        queued=enqueued.queued_to_redis,
    )

    return IntakeResult(
        evidence=evidence,
        quality=quality,
        job_id=enqueued.job.id,
        queued=enqueued.queued_to_redis,
    )


async def get_for_inspection(
    db: AsyncSession, *, evidence_id: uuid.UUID, inspection_id: uuid.UUID
) -> Evidence:
    evidence = await db.get(Evidence, evidence_id)
    if evidence is None or evidence.inspection_id != inspection_id:
        raise NotFoundError("That evidence was not found.")
    return evidence


async def derivative_key_for(
    db: AsyncSession, *, evidence_id: uuid.UUID, kind: str
) -> tuple[str, str] | None:
    row = await db.scalar(
        select(EvidenceDerivative).where(
            EvidenceDerivative.evidence_id == evidence_id, EvidenceDerivative.kind == kind
        )
    )
    if row is None:
        return None
    return row.storage_bucket, row.storage_key


async def view_url(
    db: AsyncSession, evidence: Evidence, *, prefer: str = EvidenceKind.THUMBNAIL
) -> str | None:
    """Signed URL for displaying evidence.

    Prefers a derivative so the original is not served to a browser cache. Falls back
    to the original when no derivative exists yet, which is the case in the seconds
    between upload and analysis.
    """
    pair = await derivative_key_for(db, evidence_id=evidence.id, kind=prefer)
    if pair is not None:
        _bucket, key = pair
        return await storage.signed_url(logical_bucket=storage.Bucket.DERIVATIVES, key=key)
    return await storage.signed_url(
        logical_bucket=storage.Bucket.ORIGINALS, key=evidence.storage_key
    )


async def verify_integrity(db: AsyncSession, evidence: Evidence) -> dict[str, Any]:
    """Re-read the stored object and confirm its hash still matches.

    Used by the evidence detail view and by the restore verification script. This is
    what makes the chain of custody a claim that can be checked rather than asserted.
    """
    try:
        matches = await storage.verify_stored_hash(
            logical_bucket=storage.Bucket.ORIGINALS,
            key=evidence.storage_key,
            expected_sha256=evidence.sha256,
        )
        detail = "The stored file matches the hash recorded at upload."
        if not matches:
            detail = (
                "The stored file does not match the hash recorded at upload. "
                "Treat this evidence as unreliable and investigate."
            )
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        logger.error(
            "evidence_integrity_check_failed",
            evidence_id=str(evidence.id),
            error=type(exc).__name__,
        )
        return {
            "verified": False,
            "detail": "The stored file could not be read to verify its hash.",
            "recorded_sha256": evidence.sha256,
        }
    return {
        "verified": matches,
        "detail": detail,
        "recorded_sha256": evidence.sha256,
        "algorithm": evidence.hash_algorithm,
    }


async def ocr_result_for(db: AsyncSession, evidence_id: uuid.UUID) -> OcrResult | None:
    return await db.scalar(
        select(OcrResult)
        .where(OcrResult.evidence_id == evidence_id)
        .order_by(OcrResult.created_at.desc())
        .limit(1)
    )


async def request_reanalysis(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    evidence: Evidence,
    requested_by_id: uuid.UUID,
    reason: str,
) -> uuid.UUID:
    """Queue analysis again, for example after an OCR upgrade."""
    attempt = f"v{int(datetime.now(UTC).timestamp())}"
    enqueued = await jobs.enqueue(
        db,
        job_type=jobs.JOB_ANALYSE_EVIDENCE,
        idempotency_key=jobs.evidence_analysis_key(evidence.id, attempt_tag=attempt),
        evidence_id=evidence.id,
        inspection_id=evidence.inspection_id,
        payload={"reason": reason},
        requested_by_id=requested_by_id,
    )
    evidence.analysis_state = AnalysisState.QUEUED
    await audit_service.record(
        db,
        context,
        action="evidence.reanalysis_requested",
        entity_type="evidence",
        entity_id=evidence.id,
        new_values={"job_id": str(enqueued.job.id)},
        reason=reason,
    )
    return enqueued.job.id
