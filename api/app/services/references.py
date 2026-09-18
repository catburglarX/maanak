"""Human-readable references.

Format::

    INSP-2026-000417
    CMP-2026-000042
    CASE-2026-000009
    RPT-2026-000015
    NOT-2026-000021

The number comes from a PostgreSQL sequence, so two concurrent requests can never
receive the same reference. A ``SELECT max(...) + 1`` read would allow exactly
that under load.

Sequences are global rather than per-year: the year in the reference is the year
of creation, and the counter simply never repeats. This keeps generation to one
round trip with no locking.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

INSPECTION_PREFIX = "INSP"
COMPLAINT_PREFIX = "CMP"
CASE_PREFIX = "CASE"
REPORT_PREFIX = "RPT"
NOTICE_PREFIX = "NOT"

_SEQUENCES: dict[str, str] = {
    INSPECTION_PREFIX: "maanak_inspection_reference_seq",
    COMPLAINT_PREFIX: "maanak_complaint_reference_seq",
    CASE_PREFIX: "maanak_case_reference_seq",
    REPORT_PREFIX: "maanak_report_reference_seq",
    NOTICE_PREFIX: "maanak_notice_reference_seq",
}

#: Zero padding for the counter. Widens automatically past the limit.
_PAD = 6


async def next_reference(db: AsyncSession, prefix: str, *, on: datetime | None = None) -> str:
    """Allocate the next reference for ``prefix``.

    Sequence values are consumed outside transactional rollback, so a failed
    request leaves a gap rather than reusing a number. That is the correct
    trade-off: a gap is harmless, a duplicate reference is not.
    """
    try:
        sequence_name = _SEQUENCES[prefix]
    except KeyError as exc:
        raise ValueError(f"unknown reference prefix {prefix!r}") from exc

    value = await db.scalar(text(f"SELECT nextval('{sequence_name}')"))
    moment = on or datetime.now(UTC)
    return f"{prefix}-{moment.year}-{int(value or 0):0{_PAD}d}"


def parse_reference(reference: str) -> tuple[str, int, int] | None:
    """Split a reference into ``(prefix, year, number)``, or None if malformed."""
    parts = reference.strip().upper().split("-")
    if len(parts) != 3:
        return None
    prefix, year, number = parts
    if prefix not in _SEQUENCES:
        return None
    if not (year.isdigit() and number.isdigit()):
        return None
    return prefix, int(year), int(number)


def looks_like_reference(reference: str) -> bool:
    return parse_reference(reference) is not None
