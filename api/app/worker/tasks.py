"""Worker tasks.

``analyse_evidence`` is the heavy path: download the original, build derivatives,
run OCR, locate declarations, write candidates. It runs here rather than in a
request handler because a full-resolution package photograph takes seconds.

Idempotency: the task deletes any candidates it previously produced for the same
evidence before writing new ones, and derivatives are written with ``overwrite``.
Running the task twice therefore leaves the same state as running it once, which is
what makes a retry safe.

Officer corrections are never destroyed by a re-run: a candidate that has been
reviewed is left in place and the machine reading is refreshed only where the
officer has not yet decided.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select

from ..config import get_settings
from ..domain.enums import (
    AnalysisState,
    EvidenceKind,
    InspectionState,
    MachineState,
    ReviewState,
)
from ..models.evidence import Evidence, EvidenceDerivative, OcrResult
from ..models.finding import DeclarationCandidate
from ..models.inspection import Inspection
from ..models.product import Product
from ..observability import JOB_DURATION, JOBS_COMPLETED, get_logger
from ..services import imaging, jobs, storage
from ..services.audit import AuditContext
from ..services.audit import record as audit_record
from ..services.canonical import json_safe
from ..services.extraction import pipeline as extraction_pipeline
from ..services.ocr import read_barcodes, read_with_best_profile, run_ocr

logger = get_logger(__name__)

#: Confidence at or below which a located value is treated as unreadable rather
#: than reported as a reading. Set from measurement: below this level the parsed
#: value disagreed with the printed value often enough in testing that presenting
#: it as a reading would mislead the officer more often than it would help.
UNREADABLE_CONFIDENCE = 0.35


async def analyse_evidence(context: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Analyse one evidence file. Registered as an arq task."""
    from ..db import session_scope

    identifier = uuid.UUID(job_id)
    started = datetime.now(UTC)

    async with session_scope() as db:
        job = await jobs.mark_running(db, identifier)
        if job is None:
            logger.warning("job_missing", job_id=job_id)
            return {"status": "job_not_found"}
        evidence_id = job.evidence_id

    if evidence_id is None:
        async with session_scope() as db:
            await jobs.mark_failed(
                db, identifier, reason="This job has no evidence attached.", retryable=False
            )
        return {"status": "no_evidence"}

    try:
        result = await _run_analysis(identifier, evidence_id)
    except Exception:
        logger.exception("evidence_analysis_failed", evidence_id=str(evidence_id))
        async with session_scope() as db:
            await jobs.mark_failed(
                db,
                identifier,
                reason=(
                    "The evidence could not be analysed. The image is stored and "
                    "unchanged; analysis can be retried."
                ),
            )
            evidence = await db.get(Evidence, evidence_id)
            if evidence is not None:
                evidence.analysis_state = AnalysisState.FAILED
                evidence.analysis_failure_reason = (
                    "Analysis did not complete. The stored image is unaffected."
                )
        JOBS_COMPLETED.labels(job_type=jobs.JOB_ANALYSE_EVIDENCE, outcome="failed").inc()
        raise
    else:
        JOB_DURATION.labels(job_type=jobs.JOB_ANALYSE_EVIDENCE).observe(
            (datetime.now(UTC) - started).total_seconds()
        )
        JOBS_COMPLETED.labels(job_type=jobs.JOB_ANALYSE_EVIDENCE, outcome="succeeded").inc()
        return result


async def _run_analysis(job_id: uuid.UUID, evidence_id: uuid.UUID) -> dict[str, Any]:
    from ..db import session_scope

    settings = get_settings()

    async with session_scope() as db:
        evidence = await db.get(Evidence, evidence_id)
        if evidence is None:
            await jobs.mark_failed(
                db, job_id, reason="The evidence record no longer exists.", retryable=False
            )
            return {"status": "evidence_not_found"}
        evidence.analysis_state = AnalysisState.RUNNING
        key = evidence.storage_key
        inspection_id = evidence.inspection_id
        await jobs.update_progress(db, job_id, progress=10, stage="reading stored image")

    # 1. Read the original. Never modified, only read.
    payload = await storage.get_object(logical_bucket=storage.Bucket.ORIGINALS, key=key)
    decoded = imaging.decode_image(payload)

    async with session_scope() as db:
        await jobs.update_progress(db, job_id, progress=20, stage="building derivatives")

    # 2. Derivatives. Regenerable, stored separately from the original.
    thumbnail_bytes, thumbnail_width, thumbnail_height = imaging.make_thumbnail(decoded)
    ocr_bytes, ocr_width, ocr_height, transform = imaging.make_ocr_input(decoded)

    thumbnail_object = await storage.put_object(
        logical_bucket=storage.Bucket.DERIVATIVES,
        key=storage.derivative_key(
            evidence_id=evidence_id, kind=EvidenceKind.THUMBNAIL, extension=".jpg"
        ),
        data=thumbnail_bytes,
        content_type="image/jpeg",
        overwrite=True,
    )
    ocr_object = await storage.put_object(
        logical_bucket=storage.Bucket.DERIVATIVES,
        key=storage.derivative_key(
            evidence_id=evidence_id, kind=EvidenceKind.OCR_INPUT, extension=".png"
        ),
        data=ocr_bytes,
        content_type="image/png",
        overwrite=True,
    )

    async with session_scope() as db:
        for kind, stored, width, height, mime, applied in (
            (
                EvidenceKind.THUMBNAIL,
                thumbnail_object,
                thumbnail_width,
                thumbnail_height,
                "image/jpeg",
                {},
            ),
            (EvidenceKind.OCR_INPUT, ocr_object, ocr_width, ocr_height, "image/png", transform),
        ):
            existing = await db.scalar(
                select(EvidenceDerivative).where(
                    EvidenceDerivative.evidence_id == evidence_id,
                    EvidenceDerivative.kind == kind,
                )
            )
            if existing is None:
                db.add(
                    EvidenceDerivative(
                        id=uuid.uuid4(),
                        evidence_id=evidence_id,
                        kind=kind,
                        storage_bucket=stored.bucket,
                        storage_key=stored.key,
                        mime_type=mime,
                        size_bytes=stored.size_bytes,
                        sha256=stored.sha256,
                        width=width,
                        height=height,
                        transform=json_safe(applied),
                    )
                )
            else:
                existing.storage_key = stored.key
                existing.size_bytes = stored.size_bytes
                existing.sha256 = stored.sha256
                existing.width = width
                existing.height = height
                existing.transform = json_safe(applied)
        await jobs.update_progress(db, job_id, progress=35, stage="reading text")

    # 3. OCR on the prepared image. Several language configurations are tried and the
    #    one that reads best is kept; see ocr.read_with_best_profile.
    ocr_image = imaging.decode_image(ocr_bytes)
    outcome = read_with_best_profile(
        ocr_image.pillow,
        profile_names=("default", "english"),
        correct_orientation=True,
        min_word_confidence=settings.ocr_min_word_confidence,
    )

    # A panel with scattered text sometimes reads better with sparse segmentation.
    if outcome.succeeded and outcome.word_count < 8:
        alternative = run_ocr(ocr_image.pillow, profile_name="sparse", correct_orientation=False)
        if alternative.word_count > outcome.word_count:
            outcome = alternative

    async with session_scope() as db:
        await jobs.update_progress(db, job_id, progress=60, stage="reading barcodes")

    barcodes = read_barcodes(decoded.pillow)

    async with session_scope() as db:
        await jobs.update_progress(db, job_id, progress=70, stage="locating declarations")

    matches = extraction_pipeline.extract_matches(outcome)
    summary = extraction_pipeline.summarise(matches)

    async with session_scope() as db:
        evidence = await db.get(Evidence, evidence_id)
        if evidence is None:
            await jobs.mark_failed(
                db,
                job_id,
                reason="The evidence record was removed during analysis.",
                retryable=False,
            )
            return {"status": "evidence_removed"}

        # 4. Store the OCR result, replacing any previous run for this profile.
        existing_ocr = await db.scalar(
            select(OcrResult).where(
                OcrResult.evidence_id == evidence_id, OcrResult.profile == outcome.profile
            )
        )
        if existing_ocr is None:
            existing_ocr = OcrResult(
                id=uuid.uuid4(), evidence_id=evidence_id, profile=outcome.profile
            )
            db.add(existing_ocr)
        existing_ocr.engine = outcome.engine
        existing_ocr.engine_version = outcome.engine_version
        existing_ocr.languages = outcome.languages
        existing_ocr.detected_script = outcome.detected_script
        existing_ocr.orientation_applied_degrees = outcome.orientation_applied_degrees
        existing_ocr.raw_text = outcome.raw_text
        existing_ocr.words = json_safe([word.as_dict() for word in outcome.words])
        existing_ocr.lines = json_safe([line.as_dict() for line in outcome.lines])
        existing_ocr.mean_confidence = outcome.mean_confidence
        existing_ocr.word_count = outcome.word_count
        existing_ocr.duration_ms = outcome.duration_ms
        existing_ocr.failure_reason = outcome.failure_reason
        await db.flush()

        # 5. Candidates. Only unreviewed ones are replaced, so an officer's decision
        #    survives a re-analysis.
        await db.execute(
            delete(DeclarationCandidate).where(
                DeclarationCandidate.evidence_id == evidence_id,
                DeclarationCandidate.review_state == ReviewState.PENDING,
            )
        )
        reviewed_types = set(
            await db.scalars(
                select(DeclarationCandidate.declaration_type).where(
                    DeclarationCandidate.evidence_id == evidence_id,
                    DeclarationCandidate.review_state != ReviewState.PENDING,
                )
            )
        )

        created = 0
        for match in matches:
            if match.declaration_type.value in reviewed_types:
                continue
            candidate = _build_candidate(
                evidence=evidence,
                inspection_id=inspection_id,
                ocr_result_id=existing_ocr.id,
                match=match,
                outcome=outcome,
                transform=transform,
                original_size=(decoded.width, decoded.height),
            )
            db.add(candidate)
            created += 1

        # 6. Barcode reads recorded on the evidence, not treated as proof.
        evidence.barcodes = json_safe([item.as_dict() for item in barcodes])
        evidence.analysis_state = (
            AnalysisState.COMPLETED if outcome.succeeded else AnalysisState.FAILED
        )
        evidence.analysis_failure_reason = outcome.failure_reason
        evidence.stored_width = decoded.width
        evidence.stored_height = decoded.height

        result_payload = {
            "evidence_id": str(evidence_id),
            "ocr": {
                "profile": outcome.profile,
                "word_count": outcome.word_count,
                "mean_confidence": outcome.mean_confidence,
                "detected_script": outcome.detected_script,
                "orientation_applied_degrees": outcome.orientation_applied_degrees,
                "duration_ms": outcome.duration_ms,
                "failure_reason": outcome.failure_reason,
                "warnings": outcome.warnings,
            },
            "declarations": summary,
            "candidates_created": created,
            "barcodes_read": len(barcodes),
        }
        await jobs.mark_succeeded(db, job_id, result=json_safe(result_payload))
        await jobs.update_progress(db, job_id, progress=100, stage="complete")

        await audit_record(
            db,
            AuditContext(actor_role="system", jurisdiction_code=None),
            action="evidence.analysed",
            entity_type="evidence",
            entity_id=evidence_id,
            new_values={
                "candidates_created": created,
                "word_count": outcome.word_count,
                "barcodes_read": len(barcodes),
                "ocr_failure_reason": outcome.failure_reason,
            },
        )

    # 7. Advance the inspection when nothing is left to analyse.
    await _advance_inspection_if_ready(inspection_id)

    logger.info(
        "evidence_analysed",
        evidence_id=str(evidence_id),
        words=outcome.word_count,
        candidates=result_payload["candidates_created"],
        barcodes=len(barcodes),
        duration_ms=outcome.duration_ms,
    )
    return result_payload


def _build_candidate(
    *,
    evidence: Evidence,
    inspection_id: uuid.UUID,
    ocr_result_id: uuid.UUID,
    match: Any,
    outcome: Any,
    transform: dict[str, Any],
    original_size: tuple[int, int],
) -> DeclarationCandidate:
    """Turn an extraction match into a stored candidate.

    The region is mapped from OCR-input coordinates back to the original image, so a
    highlight drawn in the review screen lines up with the photograph the officer
    took rather than with the processed copy.
    """
    region: list[int] = []
    height_px: float | None = None
    context_text = ""

    if match.line_index is not None and match.span is not None:
        indexes = extraction_pipeline.build_line_index(outcome)
        for index in indexes:
            if index.line.index == match.line_index:
                raw_region = index.region_for_span(*match.span)
                if raw_region:
                    region = imaging.map_region_to_original(
                        [float(value) for value in raw_region],
                        transform=transform,
                        original_size=original_size,
                    )
                height_px = index.height_for_span(*match.span)
                break
        context_text = extraction_pipeline.context_for(outcome, match.line_index)

    machine_state = MachineState.LOCATED
    explanation_parts: list[str] = []

    if not match.parsed:
        machine_state = MachineState.UNREADABLE
        explanation_parts.append("The label was found but the value beside it could not be read.")
    elif match.parse_confidence <= UNREADABLE_CONFIDENCE:
        machine_state = MachineState.UNREADABLE
        explanation_parts.append(
            "A value was found but the reading confidence is too low to present it as "
            "reliable. Read the value from the photograph and record it."
        )
    elif match.normalised_value.get("ambiguous"):
        machine_state = MachineState.AMBIGUOUS
        explanation_parts.append("The printed value can be read more than one way.")

    if match.label_found:
        explanation_parts.append(f"Located next to the printed label {match.label_found!r}.")
    else:
        explanation_parts.append("Located by its format; no printed label was found beside it.")
    explanation_parts.extend(match.notes)

    numeric: Decimal | None = None
    if match.numeric_value is not None:
        try:
            numeric = Decimal(str(match.numeric_value))
        except (TypeError, ValueError):
            numeric = None

    return DeclarationCandidate(
        id=uuid.uuid4(),
        inspection_id=inspection_id,
        evidence_id=evidence.id,
        ocr_result_id=ocr_result_id,
        declaration_type=match.declaration_type.value,
        machine_state=machine_state,
        matched_text=match.matched_text,
        context_text=context_text[:2000] or None,
        normalised_value=json_safe(match.normalised_value),
        numeric_value=numeric,
        unit=match.unit,
        region=region,
        text_height_px=height_px,
        machine_confidence=match.parse_confidence,
        parser_name=match.parser_name,
        parser_version="1",
        machine_explanation=" ".join(explanation_parts)[:2000],
        review_state=ReviewState.PENDING,
    )


async def _advance_inspection_if_ready(inspection_id: uuid.UUID) -> None:
    """Move an inspection to officer review once every file has been analysed."""
    from ..db import session_scope

    async with session_scope() as db:
        inspection = await db.get(Inspection, inspection_id)
        if inspection is None:
            return
        pending = await jobs.evidence_pending_analysis(db, inspection_id)
        if pending:
            return
        if inspection.state in {InspectionState.PROCESSING, InspectionState.EVIDENCE_PENDING}:
            from ..services.inspections import apply_transition_unchecked

            await apply_transition_unchecked(
                db,
                inspection=inspection,
                target=InspectionState.OFFICER_REVIEW,
                reason="All submitted evidence has been analysed.",
                actor_id=None,
                actor_role="system",
            )


async def evaluate_inspection_rules(context: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Run the rule engine for an inspection in the background."""
    from ..db import session_scope
    from ..services.rules import evaluate_inspection

    identifier = uuid.UUID(job_id)
    async with session_scope() as db:
        job = await jobs.mark_running(db, identifier)
        if job is None or job.inspection_id is None:
            if job is not None:
                await jobs.mark_failed(
                    db, identifier, reason="This job has no inspection attached.", retryable=False
                )
            return {"status": "job_not_found"}
        inspection_id = job.inspection_id

    async with session_scope() as db:
        inspection = await db.get(Inspection, inspection_id)
        if inspection is None:
            await jobs.mark_failed(
                db, identifier, reason="The inspection no longer exists.", retryable=False
            )
            return {"status": "inspection_not_found"}
        product = await db.get(Product, inspection.product_id)
        if product is None:
            await jobs.mark_failed(
                db, identifier, reason="The product record is missing.", retryable=False
            )
            return {"status": "product_not_found"}

        result = await evaluate_inspection(db, inspection=inspection, product=product)
        await jobs.mark_succeeded(db, identifier, result=json_safe(result.as_dict()))

    JOBS_COMPLETED.labels(job_type=jobs.JOB_EVALUATE_RULES, outcome="succeeded").inc()
    return result.as_dict()
