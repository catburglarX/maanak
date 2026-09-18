"""Text normalisation for OCR output.

OCR of small print on packaging produces a predictable set of confusions. These
helpers repair them *for matching purposes only*: the original OCR text is always
kept and shown to the officer, and only the normalised copy is used to find labels
and parse values. A repair therefore never rewrites the record of what the machine
actually read.
"""

from __future__ import annotations

import re
import unicodedata

#: Characters OCR commonly substitutes inside numbers. Applied only to a span
#: already identified as numeric, never to free text, because "O" to "0" in a
#: manufacturer name would corrupt it.
NUMERIC_CONFUSIONS = str.maketrans(
    {
        "O": "0",
        "o": "0",
        "Q": "0",
        "D": "0",
        "l": "1",
        "I": "1",
        "|": "1",
        "!": "1",
        "S": "5",
        "s": "5",
        "B": "8",
        "Z": "2",
        "z": "2",
        "G": "6",
        "T": "7",
    }
)

#: Currency symbol variants and OCR misreads of the rupee sign.
RUPEE_VARIANTS = ("₹", "R s", "Rs", "RS", "rs", "INR", "₨", "Rs,", "Rs;")

_WHITESPACE = re.compile(r"[\s\u00a0\u2000-\u200b]+")
_PUNCT_RUNS = re.compile(r"([.,:;\-_]){2,}")


def normalise_whitespace(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def normalise_for_matching(text: str) -> str:
    """Lower-cased, NFKC-normalised, single-spaced form used for label matching."""
    if not text:
        return ""
    normalised = unicodedata.normalize("NFKC", text)
    normalised = _PUNCT_RUNS.sub(r"\1", normalised)
    return normalise_whitespace(normalised).lower()


def repair_numeric_span(text: str) -> str:
    """Apply digit confusions to a span expected to be numeric."""
    if not text:
        return text
    return text.translate(NUMERIC_CONFUSIONS)


def looks_numeric(text: str) -> bool:
    """True when a span is mostly digits once obvious confusions are repaired."""
    if not text:
        return False
    repaired = repair_numeric_span(text)
    digits = sum(character.isdigit() for character in repaired)
    letters = sum(character.isalpha() for character in repaired)
    return digits >= 1 and digits >= letters


def strip_label(text: str, label: str) -> str:
    """Remove a matched label and its separator from the front of a value."""
    pattern = re.compile(rf"^\s*{re.escape(label)}\s*[:\-–=.]*\s*", re.IGNORECASE | re.UNICODE)
    return pattern.sub("", text).strip()


def collapse_ocr_gaps(text: str) -> str:
    """Join single characters split apart by OCR.

    "M R P" becomes "MRP". Only runs of three or more isolated capitals are joined,
    so genuine initials in an address are left alone.
    """
    return re.sub(
        r"\b((?:[A-Z]\s){2,}[A-Z])\b",
        lambda match: match.group(1).replace(" ", ""),
        text,
    )


def truncate_context(text: str, *, limit: int = 400) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def sanitise_for_storage(text: str) -> str:
    """Strip control characters that would corrupt a stored string or a PDF."""
    return "".join(
        character
        for character in text
        if character in "\n\t" or not unicodedata.category(character).startswith("C")
    )
