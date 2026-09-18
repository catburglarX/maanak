"""Object storage.

Evidence is kept in S3-compatible object storage (MinIO locally, any compatible
service in production), never on the API container's filesystem. Three buckets with
different lifecycles:

``originals``
    The bytes exactly as received. Written once, with object-lock-friendly keys.
    Nothing in the application updates or overwrites an object here.

``derivatives``
    Thumbnails, OCR inputs, annotated images. Regenerable, so they can be purged
    and rebuilt without touching evidence.

``reports``
    Issued PDF and DOCX documents.

Access from the browser is by short-lived signed URL for images, and through a
controlled download endpoint for originals so that every access is logged.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from ..config import get_settings
from ..errors import StorageError
from ..observability import STORAGE_OPERATIONS, get_logger
from .canonical import sha256_bytes

logger = get_logger(__name__)

_client: Any | None = None
_public_client: Any | None = None
_buckets_ready = False

#: Object keys are built from server-controlled parts only. This pattern is the
#: last line of defence against traversal or injection through a filename.
SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_.=-]{0,900}$")


class Bucket:
    ORIGINALS = "originals"
    DERIVATIVES = "derivatives"
    REPORTS = "reports"


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    key: str
    size_bytes: int
    sha256: str
    version_id: str | None
    encryption: str | None
    content_type: str


def bucket_name(logical: str) -> str:
    settings = get_settings()
    mapping = {
        Bucket.ORIGINALS: settings.s3_bucket_originals,
        Bucket.DERIVATIVES: settings.s3_bucket_derivatives,
        Bucket.REPORTS: settings.s3_bucket_reports,
    }
    try:
        return mapping[logical]
    except KeyError as exc:
        raise ValueError(f"unknown bucket {logical!r}") from exc


def _build_client(endpoint: str) -> Any:
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=BotoConfig(
            signature_version="s3v4",
            # Required for MinIO and most self-hosted gateways.
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=30,
        ),
    )


def get_client() -> Any:
    """Client used for server-side operations."""
    global _client
    if _client is None:
        _client = _build_client(get_settings().s3_endpoint_url)
    return _client


def get_signing_client() -> Any:
    """Client that signs URLs against the browser-reachable endpoint.

    Inside the compose network the service is ``http://minio:9000``, but a browser
    must be sent ``http://localhost:9000``. Signing with the wrong host produces a
    URL that fails signature validation, so a separate client is used.
    """
    global _public_client
    settings = get_settings()
    endpoint = settings.s3_public_endpoint_url or settings.s3_endpoint_url
    if endpoint == settings.s3_endpoint_url:
        return get_client()
    if _public_client is None:
        _public_client = _build_client(endpoint)
    return _public_client


def reset_clients() -> None:
    global _client, _public_client, _buckets_ready
    _client = None
    _public_client = None
    _buckets_ready = False


# --------------------------------------------------------------------------
# Key construction
# --------------------------------------------------------------------------
def evidence_key(*, inspection_reference: str, evidence_id: uuid.UUID, extension: str) -> str:
    """Deterministic, non-guessable key for an original.

    Includes the inspection reference for human traceability during an incident,
    and the evidence UUID so the key cannot be guessed from the reference alone.
    """
    safe_reference = re.sub(r"[^A-Za-z0-9-]", "", inspection_reference)[:40]
    stamp = datetime.now(UTC).strftime("%Y/%m")
    return f"evidence/{stamp}/{safe_reference}/{evidence_id}{extension}"


def derivative_key(*, evidence_id: uuid.UUID, kind: str, extension: str) -> str:
    return f"derivatives/{evidence_id}/{kind}{extension}"


def complaint_attachment_key(
    *, complaint_reference: str, attachment_id: uuid.UUID, extension: str
) -> str:
    safe_reference = re.sub(r"[^A-Za-z0-9-]", "", complaint_reference)[:40]
    stamp = datetime.now(UTC).strftime("%Y/%m")
    return f"complaints/{stamp}/{safe_reference}/{attachment_id}{extension}"


def report_key(*, report_reference: str, report_id: uuid.UUID, extension: str) -> str:
    safe_reference = re.sub(r"[^A-Za-z0-9-]", "", report_reference)[:40]
    return f"reports/{safe_reference}/{report_id}{extension}"


def validate_key(key: str) -> str:
    if not SAFE_KEY.match(key) or ".." in key:
        raise StorageError("The storage key is not acceptable.", code="invalid_storage_key")
    return key


# --------------------------------------------------------------------------
# Bucket lifecycle
# --------------------------------------------------------------------------
def _ensure_buckets_sync() -> None:
    global _buckets_ready
    if _buckets_ready:
        return
    client = get_client()
    settings = get_settings()
    for logical in (Bucket.ORIGINALS, Bucket.DERIVATIVES, Bucket.REPORTS):
        name = bucket_name(logical)
        try:
            client.head_bucket(Bucket=name)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            client.create_bucket(Bucket=name)
            logger.info("storage_bucket_created", bucket=name)

        # Versioning on originals means an accidental overwrite is recoverable.
        if logical == Bucket.ORIGINALS:
            try:
                client.put_bucket_versioning(
                    Bucket=name, VersioningConfiguration={"Status": "Enabled"}
                )
            except ClientError as exc:  # pragma: no cover - gateway dependent
                logger.warning(
                    "storage_versioning_unavailable",
                    bucket=name,
                    error=exc.response.get("Error", {}).get("Code"),
                )

    _buckets_ready = True
    logger.info(
        "storage_ready",
        originals=settings.s3_bucket_originals,
        derivatives=settings.s3_bucket_derivatives,
        reports=settings.s3_bucket_reports,
    )


async def ensure_buckets() -> None:
    await asyncio.to_thread(_ensure_buckets_sync)


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------
def put_object_sync(
    *,
    logical_bucket: str,
    key: str,
    data: bytes,
    content_type: str,
    metadata: dict[str, str] | None = None,
    overwrite: bool = False,
) -> StoredObject:
    """Store bytes. Refuses to overwrite unless explicitly allowed."""
    _ensure_buckets_sync()
    validate_key(key)
    name = bucket_name(logical_bucket)
    client = get_client()
    settings = get_settings()

    if not overwrite:
        try:
            client.head_object(Bucket=name, Key=key)
            raise StorageError(
                "An object already exists at that storage key.", code="storage_key_exists"
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in {"404", "NoSuchKey", "NotFound"}:
                STORAGE_OPERATIONS.labels(operation="head", outcome="error").inc()
                raise

    arguments: dict[str, Any] = {
        "Bucket": name,
        "Key": key,
        "Body": data,
        "ContentType": content_type,
        "ChecksumAlgorithm": "SHA256",
    }
    if settings.s3_server_side_encryption:
        arguments["ServerSideEncryption"] = settings.s3_server_side_encryption
    if metadata:
        # Metadata values must be ASCII header-safe.
        arguments["Metadata"] = {
            k: re.sub(r"[^\x20-\x7E]", "", str(v))[:255] for k, v in metadata.items()
        }

    try:
        result = client.put_object(**arguments)
    except (BotoCoreError, ClientError) as exc:
        STORAGE_OPERATIONS.labels(operation="put", outcome="error").inc()
        logger.error("storage_put_failed", bucket=name, error=type(exc).__name__)
        raise StorageError from exc

    STORAGE_OPERATIONS.labels(operation="put", outcome="ok").inc()
    return StoredObject(
        bucket=name,
        key=key,
        size_bytes=len(data),
        sha256=sha256_bytes(data),
        version_id=result.get("VersionId"),
        encryption=result.get("ServerSideEncryption"),
        content_type=content_type,
    )


async def put_object(
    *,
    logical_bucket: str,
    key: str,
    data: bytes,
    content_type: str,
    metadata: dict[str, str] | None = None,
    overwrite: bool = False,
) -> StoredObject:
    return await asyncio.to_thread(
        put_object_sync,
        logical_bucket=logical_bucket,
        key=key,
        data=data,
        content_type=content_type,
        metadata=metadata,
        overwrite=overwrite,
    )


def get_object_sync(*, logical_bucket: str, key: str) -> bytes:
    validate_key(key)
    name = bucket_name(logical_bucket)
    try:
        result = get_client().get_object(Bucket=name, Key=key)
        payload: bytes = result["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        STORAGE_OPERATIONS.labels(operation="get", outcome="error").inc()
        raise StorageError(
            "The stored object could not be read.", code="storage_read_failed"
        ) from exc
    STORAGE_OPERATIONS.labels(operation="get", outcome="ok").inc()
    return payload


async def get_object(*, logical_bucket: str, key: str) -> bytes:
    return await asyncio.to_thread(get_object_sync, logical_bucket=logical_bucket, key=key)


def stream_object_sync(*, logical_bucket: str, key: str) -> BinaryIO:
    validate_key(key)
    try:
        result = get_client().get_object(Bucket=bucket_name(logical_bucket), Key=key)
        return result["Body"]
    except (BotoCoreError, ClientError) as exc:
        raise StorageError(
            "The stored object could not be read.", code="storage_read_failed"
        ) from exc


def signed_url_sync(
    *,
    logical_bucket: str,
    key: str,
    expires_in: int | None = None,
    download_filename: str | None = None,
) -> str:
    """Presigned GET URL, valid for a few minutes only."""
    validate_key(key)
    settings = get_settings()
    parameters: dict[str, Any] = {"Bucket": bucket_name(logical_bucket), "Key": key}
    if download_filename:
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", download_filename)[:120]
        parameters["ResponseContentDisposition"] = f'attachment; filename="{safe_name}"'
    try:
        return str(
            get_signing_client().generate_presigned_url(
                "get_object",
                Params=parameters,
                ExpiresIn=expires_in or settings.s3_signed_url_ttl_seconds,
            )
        )
    except (BotoCoreError, ClientError) as exc:
        raise StorageError(
            "A temporary access link could not be created.", code="signing_failed"
        ) from exc


async def signed_url(
    *,
    logical_bucket: str,
    key: str,
    expires_in: int | None = None,
    download_filename: str | None = None,
) -> str:
    return await asyncio.to_thread(
        signed_url_sync,
        logical_bucket=logical_bucket,
        key=key,
        expires_in=expires_in,
        download_filename=download_filename,
    )


def delete_object_sync(*, logical_bucket: str, key: str) -> None:
    validate_key(key)
    try:
        get_client().delete_object(Bucket=bucket_name(logical_bucket), Key=key)
    except (BotoCoreError, ClientError) as exc:
        STORAGE_OPERATIONS.labels(operation="delete", outcome="error").inc()
        raise StorageError(
            "The stored object could not be removed.", code="storage_delete_failed"
        ) from exc
    STORAGE_OPERATIONS.labels(operation="delete", outcome="ok").inc()


async def delete_object(*, logical_bucket: str, key: str) -> None:
    await asyncio.to_thread(delete_object_sync, logical_bucket=logical_bucket, key=key)


def object_exists_sync(*, logical_bucket: str, key: str) -> bool:
    try:
        get_client().head_object(Bucket=bucket_name(logical_bucket), Key=key)
    except ClientError:
        return False
    except BotoCoreError:
        return False
    return True


def _health_sync() -> bool:
    try:
        _ensure_buckets_sync()
        get_client().head_bucket(Bucket=bucket_name(Bucket.ORIGINALS))
    except (BotoCoreError, ClientError, StorageError):
        return False
    return True


async def health() -> bool:
    return await asyncio.to_thread(_health_sync)


async def verify_stored_hash(*, logical_bucket: str, key: str, expected_sha256: str) -> bool:
    """Re-read an object and confirm its digest.

    Used by the backup-restore check and by the evidence integrity endpoint.
    """
    payload = await get_object(logical_bucket=logical_bucket, key=key)
    return sha256_bytes(payload) == expected_sha256
