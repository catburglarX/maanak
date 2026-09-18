"""Report generation and verification.

Flow at issue time:

1. freeze the snapshot and hash it;
2. render PDF and DOCX from the snapshot;
3. store each document and record its own hash;
4. optionally sign the snapshot hash through the signing adapter.

Signing: there is no real signature by default. ``REPORT_SIGNER=development``
produces an HMAC over the snapshot hash using a local key, and every place that value
appears is labelled a development signature. A production deployment supplies a real
signer through this adapter. A fabricated signature would be worse than none.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...domain.enums import ReportState
from ...errors import ConflictError, NotFoundError, ValidationError
from ...models.inspection import Inspection
from ...models.report import Report, ReportDocument
from ...observability import REPORTS_ISSUED, get_logger
from .. import audit as audit_service
from .. import storage
from ..canonical import sha256_bytes
from .docx import RENDERER_VERSION as DOCX_RENDERER_VERSION
from .docx import render_docx
from .pdf import RENDERER_VERSION as PDF_RENDERER_VERSION
from .pdf import render_pdf
from .snapshot import issue_report as freeze_report
from .snapshot import public_summary, verify_snapshot

logger = get_logger(__name__)

MIME_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@dataclass
class GeneratedReport:
    report: Report
    documents: dict[str, ReportDocument]


def sign_snapshot(snapshot_sha256: str) -> tuple[str, str | None, str | None]:
    """Return ``(status, signature, key_id)`` from the configured signer."""
    settings = get_settings()
    if settings.report_signer == "none":
        return "none", None, None

    key = settings.report_signing_key or settings.jwt_secret
    signature = hmac.new(
        key.encode("utf-8"), snapshot_sha256.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    # The key id makes it obvious in stored data that this is not a real signature.
    return "development", signature, "development-hmac-sha256"


def verification_url(report: Report) -> str:
    settings = get_settings()
    return f"{settings.verification_url_template}{report.reference}"


def _report_header(report: Report) -> dict[str, Any]:
    return {
        "reference": report.reference,
        "revision": report.revision,
        "issued_at": report.issued_at.isoformat(),
        "issuing_workspace": report.issuing_workspace,
        "snapshot_sha256": report.snapshot_sha256,
        "snapshot_algorithm": report.snapshot_algorithm,
        "verification_code": report.verification_code,
        "verification_url": verification_url(report),
        "signature_status": report.signature_status,
    }


async def issue(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    inspection: Inspection,
    issued_by_id: uuid.UUID,
    issuing_workspace: str,
) -> GeneratedReport:
    """Freeze, render and store a report."""
    issued = await freeze_report(
        db,
        inspection=inspection,
        issued_by_id=issued_by_id,
        issuing_workspace=issuing_workspace,
    )
    report = issued.report

    status, signature, key_id = sign_snapshot(report.snapshot_sha256)
    report.signature_status = status
    report.signature_value = signature
    report.signature_key_id = key_id
    report.signed_at = datetime.now(UTC) if signature else None
    await db.flush()

    header = _report_header(report)
    documents: dict[str, ReportDocument] = {}

    pdf_bytes, page_count = render_pdf(issued.snapshot, report=header)
    documents["pdf"] = await _store_document(
        db,
        report=report,
        fmt="pdf",
        payload=pdf_bytes,
        page_count=page_count,
        renderer_version=PDF_RENDERER_VERSION,
    )

    docx_bytes = render_docx(issued.snapshot, report=header)
    documents["docx"] = await _store_document(
        db,
        report=report,
        fmt="docx",
        payload=docx_bytes,
        page_count=None,
        renderer_version=DOCX_RENDERER_VERSION,
    )

    await audit_service.record(
        db,
        context,
        action="report.issued",
        entity_type="report",
        entity_id=report.id,
        new_values={
            "reference": report.reference,
            "inspection": inspection.reference,
            "snapshot_sha256": report.snapshot_sha256,
            "pdf_sha256": documents["pdf"].sha256,
            "docx_sha256": documents["docx"].sha256,
            "signature_status": report.signature_status,
            "revision": report.revision,
        },
    )
    for fmt in documents:
        REPORTS_ISSUED.labels(format=fmt).inc()

    return GeneratedReport(report=report, documents=documents)


async def _store_document(
    db: AsyncSession,
    *,
    report: Report,
    fmt: str,
    payload: bytes,
    page_count: int | None,
    renderer_version: str,
) -> ReportDocument:
    key = storage.report_key(
        report_reference=report.reference, report_id=report.id, extension=f".{fmt}"
    )
    stored = await storage.put_object(
        logical_bucket=storage.Bucket.REPORTS,
        key=key,
        data=payload,
        content_type=MIME_TYPES[fmt],
        metadata={
            "report": report.reference,
            "snapshot-sha256": report.snapshot_sha256,
        },
        overwrite=True,
    )
    document = ReportDocument(
        id=uuid.uuid4(),
        report_id=report.id,
        format=fmt,
        storage_bucket=stored.bucket,
        storage_key=stored.key,
        mime_type=MIME_TYPES[fmt],
        size_bytes=len(payload),
        sha256=sha256_bytes(payload),
        page_count=page_count,
        renderer_version=renderer_version,
    )
    db.add(document)
    await db.flush()
    logger.info(
        "report_document_stored",
        report=report.reference,
        format=fmt,
        size_bytes=len(payload),
        sha256=document.sha256[:16],
    )
    return document


async def get_document(
    db: AsyncSession, *, report: Report, fmt: str
) -> tuple[ReportDocument, bytes]:
    """Read a stored document and confirm it still matches its recorded hash."""
    if fmt not in MIME_TYPES:
        raise ValidationError("Reports are available as pdf or docx.", details={"field": "format"})
    document = await db.scalar(
        select(ReportDocument).where(
            ReportDocument.report_id == report.id, ReportDocument.format == fmt
        )
    )
    if document is None:
        raise NotFoundError(f"No {fmt.upper()} document exists for this report.")

    payload = await storage.get_object(
        logical_bucket=storage.Bucket.REPORTS, key=document.storage_key
    )
    if sha256_bytes(payload) != document.sha256:
        raise ConflictError(
            "The stored document does not match the hash recorded when it was issued. "
            "It has not been served.",
            code="document_hash_mismatch",
        )
    return document, payload


async def withdraw(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    report: Report,
    withdrawn_by_id: uuid.UUID,
    reason: str,
) -> Report:
    """Withdraw an issued report.

    The snapshot and documents are kept: a withdrawn report must remain inspectable,
    and anyone holding a copy needs the verification page to tell them it was
    withdrawn rather than that it never existed.
    """
    if report.state != ReportState.ISSUED:
        raise ConflictError(f"This report is already {report.state}.", code="report_not_issued")
    if len(reason.strip()) < 15:
        raise ValidationError(
            "Give a reason of at least 15 characters for withdrawing this report.",
            details={"field": "reason"},
        )

    report.state = ReportState.WITHDRAWN
    report.withdrawn_at = datetime.now(UTC)
    report.withdrawn_by_id = withdrawn_by_id
    report.withdrawn_reason = reason.strip()
    await db.flush()

    await audit_service.record(
        db,
        context,
        action="report.withdrawn",
        entity_type="report",
        entity_id=report.id,
        old_values={"state": ReportState.ISSUED.value},
        new_values={"state": report.state},
        reason=reason,
    )
    return report


async def find_by_reference(db: AsyncSession, reference: str) -> Report | None:
    return await db.scalar(select(Report).where(Report.reference == reference.strip().upper()))


async def find_by_verification_code(db: AsyncSession, code: str) -> Report | None:
    return await db.scalar(select(Report).where(Report.verification_code == code.strip().upper()))


__all__ = [
    "GeneratedReport",
    "find_by_reference",
    "find_by_verification_code",
    "get_document",
    "issue",
    "public_summary",
    "sign_snapshot",
    "verification_url",
    "verify_snapshot",
    "withdraw",
]
