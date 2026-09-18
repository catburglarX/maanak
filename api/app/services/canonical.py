"""Canonical serialisation and hashing.

Both the audit chain and report snapshots need a byte-for-byte reproducible
serialisation: the same logical content must always hash to the same digest, on
any machine, in any Python version. Rules:

* keys sorted;
* no insignificant whitespace;
* UTF-8, not escaped ASCII, so Hindi text hashes as itself;
* Decimal written as its exact string form, never through float;
* datetime written as UTC ISO-8601 with an explicit offset;
* UUID and date written as strings.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

CANONICAL_ALGORITHM = "sha256/json-canonical-v1"


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        # Exact decimal string. normalize() would turn 45.00 into 4.5E+1, so the
        # plain string form is used and trailing zeros are preserved.
        return {"__decimal__": format(value, "f")}
    if isinstance(value, datetime):
        moment = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        return {"__datetime__": moment.isoformat(timespec="microseconds")}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, uuid.UUID):
        return {"__uuid__": str(value)}
    if isinstance(value, set | frozenset):
        return sorted(str(item) for item in value)
    if isinstance(value, bytes):
        return {"__bytes_sha256__": hashlib.sha256(value).hexdigest()}
    raise TypeError(f"{type(value).__name__} is not canonically serialisable")


def canonical_json(payload: Any) -> str:
    """Deterministic JSON text for hashing."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_default,
    )


def canonical_bytes(payload: Any) -> bytes:
    return canonical_json(payload).encode("utf-8")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_safe(payload: Any) -> Any:
    """Convert to plain JSON types for storage in a JSONB column.

    Uses the same conventions as :func:`canonical_json` so a stored snapshot
    re-hashes to the value recorded at write time.
    """
    return json.loads(canonical_json(payload))
