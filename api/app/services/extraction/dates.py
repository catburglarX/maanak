"""Date markings.

Indian packages print dates in many forms, and the Legal Metrology rules require
month and year for manufacture, packing or import rather than a full date. So a
parsed date marking records the precision it was printed at: a "03/2026" marking is
a month, not the first of March, and nothing downstream may pretend otherwise.

Ambiguity is preserved rather than resolved. "05/03/2026" is genuinely ambiguous
between 5 March and 3 May; both readings are returned and the officer chooses.
Silently assuming day-first would produce a confident, wrong record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from .units import DEVANAGARI_DIGITS


class Precision(StrEnum):
    DAY = "day"
    MONTH = "month"
    YEAR = "year"


MONTH_NAMES: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

HINDI_MONTHS: dict[str, int] = {
    "जनवरी": 1,
    "फरवरी": 2,
    "मार्च": 3,
    "अप्रैल": 4,
    "मई": 5,
    "जून": 6,
    "जुलाई": 7,
    "अगस्त": 8,
    "सितंबर": 9,
    "सितम्बर": 9,
    "अक्टूबर": 10,
    "नवंबर": 11,
    "नवम्बर": 12,
    "दिसंबर": 12,
    "दिसम्बर": 12,
}

_MONTH_ALTERNATIVES = "|".join(
    sorted(
        (re.escape(name) for name in list(MONTH_NAMES) + list(HINDI_MONTHS)), key=len, reverse=True
    )
)

# Ordered most specific first: a full date must win over a month-year read of the
# same text.
_NUMERIC_DAY_FIRST = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b")
_MONTH_NAME_DAY = re.compile(
    rf"\b(\d{{1,2}})\s*[/.\- ]?\s*({_MONTH_ALTERNATIVES})\s*[/.\-, ]?\s*(\d{{2,4}})\b",
    re.IGNORECASE | re.UNICODE,
)
_MONTH_NAME_YEAR = re.compile(
    rf"\b({_MONTH_ALTERNATIVES})\s*[/.\-, ]?\s*(\d{{2,4}})\b", re.IGNORECASE | re.UNICODE
)
_NUMERIC_MONTH_YEAR = re.compile(r"\b(\d{1,2})[/.\-](\d{4})\b")
_NUMERIC_MONTH_SHORT_YEAR = re.compile(r"\b(\d{1,2})[/.\-](\d{2})\b")
_YEAR_ONLY = re.compile(r"\b(20\d{2})\b")

#: Two-digit years are read as 20xx. A packaged commodity marked "24" means 2024,
#: not 1924, and the rules only apply from 2011 onwards in any case.
CENTURY = 2000


@dataclass(frozen=True)
class DateMarking:
    """A parsed date marking with its printed precision."""

    year: int
    month: int | None
    day: int | None
    precision: Precision
    source_text: str
    #: Populated when the printed form is genuinely ambiguous.
    alternatives: tuple[date, ...] = ()

    @property
    def is_ambiguous(self) -> bool:
        return len(self.alternatives) > 1

    @property
    def as_date(self) -> date | None:
        """A concrete date, using the first of the month when only a month is given.

        Used for ordering and for effective-date comparisons. Callers that need to
        respect the printed precision must read ``precision`` instead.
        """
        try:
            return date(self.year, self.month or 1, self.day or 1)
        except ValueError:
            return None

    @property
    def display(self) -> str:
        if self.precision is Precision.YEAR:
            return str(self.year)
        month_name = [
            "",
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ][self.month or 0]
        if self.precision is Precision.MONTH:
            return f"{month_name} {self.year}"
        return f"{self.day} {month_name} {self.year}"

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "date",
            "year": self.year,
            "month": self.month,
            "day": self.day,
            "precision": self.precision.value,
            "iso": self.as_date.isoformat() if self.as_date else None,
            "display": self.display,
            "ambiguous": self.is_ambiguous,
            "alternatives": [item.isoformat() for item in self.alternatives],
            "source_text": self.source_text,
        }


def _expand_year(raw: str) -> int:
    value = int(raw)
    if value < 100:
        return CENTURY + value
    return value


def _valid_month(value: int) -> bool:
    return 1 <= value <= 12


def _valid_day(day: int, month: int, year: int) -> bool:
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def parse_date_marking(text: str) -> DateMarking | None:
    """Parse the first date marking in a string.

    Tried in order of specificity so that "12 Mar 2026" is not reduced to
    "Mar 2026".
    """
    if not text:
        return None
    prepared = text.translate(DEVANAGARI_DIGITS)

    match = _MONTH_NAME_DAY.search(prepared)
    if match:
        month = _lookup_month(match.group(2))
        day = int(match.group(1))
        year = _expand_year(match.group(3))
        if month and _valid_day(day, month, year):
            return DateMarking(year, month, day, Precision.DAY, match.group(0).strip())

    match = _NUMERIC_DAY_FIRST.search(prepared)
    if match:
        first, second = int(match.group(1)), int(match.group(2))
        year = _expand_year(match.group(3))
        readings: list[date] = []
        if _valid_month(second) and _valid_day(first, second, year):
            readings.append(date(year, second, first))
        if _valid_month(first) and _valid_day(second, first, year):
            candidate = date(year, first, second)
            if candidate not in readings:
                readings.append(candidate)
        if readings:
            chosen = readings[0]
            return DateMarking(
                chosen.year,
                chosen.month,
                chosen.day,
                Precision.DAY,
                match.group(0).strip(),
                alternatives=tuple(readings),
            )

    match = _MONTH_NAME_YEAR.search(prepared)
    if match:
        month = _lookup_month(match.group(1))
        year = _expand_year(match.group(2))
        if month:
            return DateMarking(year, month, None, Precision.MONTH, match.group(0).strip())

    match = _NUMERIC_MONTH_YEAR.search(prepared)
    if match:
        month, year = int(match.group(1)), int(match.group(2))
        if _valid_month(month):
            return DateMarking(year, month, None, Precision.MONTH, match.group(0).strip())

    match = _NUMERIC_MONTH_SHORT_YEAR.search(prepared)
    if match:
        month, year = int(match.group(1)), _expand_year(match.group(2))
        if _valid_month(month):
            return DateMarking(year, month, None, Precision.MONTH, match.group(0).strip())

    match = _YEAR_ONLY.search(prepared)
    if match:
        return DateMarking(int(match.group(1)), None, None, Precision.YEAR, match.group(0).strip())

    return None


def _lookup_month(raw: str) -> int | None:
    key = raw.strip().lower()
    if key in MONTH_NAMES:
        return MONTH_NAMES[key]
    return HINDI_MONTHS.get(raw.strip())


def find_all_date_markings(text: str) -> list[DateMarking]:
    """Every distinct date marking in a string.

    Matched regions are consumed so a single printed date is not reported twice at
    different precisions.
    """
    if not text:
        return []
    prepared = text.translate(DEVANAGARI_DIGITS)
    results: list[DateMarking] = []
    remaining = prepared
    for _ in range(12):  # bounded: packaging never carries more than a few dates
        marking = parse_date_marking(remaining)
        if marking is None:
            break
        results.append(marking)
        position = remaining.find(marking.source_text)
        if position < 0:
            break
        remaining = remaining[position + len(marking.source_text) :]
    return results


def months_between(earlier: DateMarking, later: DateMarking) -> int | None:
    """Whole months from one marking to another, or None if not comparable."""
    if earlier.month is None or later.month is None:
        return None
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)
