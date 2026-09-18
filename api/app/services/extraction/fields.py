"""Declaration detection.

Two stages, kept separate on purpose:

1. **Locate** a declaration by its printed label. Labels are matched from a table of
   English and Hindi variants, so "M.R.P.", "Maximum Retail Price", "अधिकतम खुदरा मूल्य"
   and "MRP Rs" all lead to the same declaration type.
2. **Parse** the value using a parser specific to that declaration type, so a price
   is read by the price parser and a quantity by the quantity parser.

Where a value can be recognised without a label, that is recorded with lower
confidence and a note saying no label was found. A declaration matched only by shape
is a weaker observation than one matched by its printed label, and the record says so.

Regular expressions are used as transparent, reviewable logic, not as the whole
answer: each match carries the exact text it came from and the image region it was
read at, so an officer can check it against the photograph.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ...domain.enums import DeclarationType
from . import dates, money, units
from . import text as text_utils


@dataclass
class Match:
    """One located declaration before it becomes a database candidate."""

    declaration_type: DeclarationType
    #: Verbatim OCR text the value was read from.
    matched_text: str
    #: Canonical parsed form, or empty when the value could not be parsed.
    normalised_value: dict[str, Any] = field(default_factory=dict)
    numeric_value: Decimal | None = None
    unit: str | None = None
    #: Which label matched, or None when found by shape alone.
    label_found: str | None = None
    #: Character span within the line, used to compute the image region.
    line_index: int | None = None
    span: tuple[int, int] | None = None
    #: 0-1. Combines OCR confidence with how the declaration was located.
    parse_confidence: float = 0.5
    notes: list[str] = field(default_factory=list)
    parser_name: str = "label_regex_v1"

    @property
    def parsed(self) -> bool:
        return bool(self.normalised_value)


LabelSet = tuple[str, ...]


@dataclass(frozen=True)
class DeclarationSpec:
    """How to find and read one declaration."""

    declaration_type: DeclarationType
    labels: LabelSet
    parser: Callable[[str], dict[str, Any] | None]
    #: Look on the following line when the label line holds no value.
    allow_next_line: bool = True
    #: Recognise the value without a label (for example an email address).
    shape_pattern: re.Pattern[str] | None = None
    #: Labels that must not appear, to avoid confusing similar declarations.
    negative_labels: LabelSet = ()


# --------------------------------------------------------------------------
# Value parsers
# --------------------------------------------------------------------------
def _parse_price(raw: str) -> dict[str, Any] | None:
    parsed = money.parse_money(raw)
    if parsed is None:
        # A labelled price field may print the number without a currency mark.
        repaired = text_utils.repair_numeric_span(raw)
        parsed = money.parse_bare_amount(repaired) if text_utils.looks_numeric(repaired) else None
    return parsed.as_dict() if parsed else None


def _parse_quantity(raw: str) -> dict[str, Any] | None:
    parsed = units.parse_quantity(raw)
    return parsed.as_dict() if parsed else None


def _parse_date(raw: str) -> dict[str, Any] | None:
    parsed = dates.parse_date_marking(raw)
    return parsed.as_dict() if parsed else None


def _parse_free_text(
    minimum: int = 3, maximum: int = 400
) -> Callable[[str], dict[str, Any] | None]:
    def parser(raw: str) -> dict[str, Any] | None:
        cleaned = text_utils.normalise_whitespace(text_utils.sanitise_for_storage(raw))
        cleaned = cleaned.strip(" .,:;-–")
        if len(cleaned) < minimum:
            return None
        return {"kind": "text", "value": cleaned[:maximum], "display": cleaned[:maximum]}

    return parser


_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Indian phone shapes: 10-digit mobile, landline with STD code, toll-free 1800.
_PHONE = re.compile(
    r"(?:\+?91[\s\-]?)?(?:1800[\s\-]?\d{3}[\s\-]?\d{3,4}|0\d{2,4}[\s\-]?\d{6,8}|[6-9]\d{9})"
)
_FSSAI = re.compile(r"\b(\d{14})\b")


def _parse_email(raw: str) -> dict[str, Any] | None:
    match = _EMAIL.search(raw)
    if not match:
        return None
    value = match.group(0).strip(" .,;").lower()
    return {"kind": "email", "value": value, "display": value}


def _parse_phone(raw: str) -> dict[str, Any] | None:
    repaired = text_utils.repair_numeric_span(raw)
    match = _PHONE.search(repaired)
    if not match:
        return None
    raw_value = match.group(0)
    digits = re.sub(r"\D", "", raw_value)
    if len(digits) < 10:
        return None
    return {
        "kind": "phone",
        "value": digits,
        "display": raw_value.strip(),
        "toll_free": digits.startswith("1800") or digits.startswith("911800"),
    }


def _parse_fssai(raw: str) -> dict[str, Any] | None:
    repaired = text_utils.repair_numeric_span(raw)
    match = _FSSAI.search(repaired)
    if not match:
        return None
    return {"kind": "licence", "value": match.group(1), "display": match.group(1)}


_COUNTRY_CLEAN = re.compile(r"^(?:the\s+)?", re.IGNORECASE)


def _parse_country(raw: str) -> dict[str, Any] | None:
    cleaned = text_utils.normalise_whitespace(raw).strip(" .,:;-–")
    cleaned = _COUNTRY_CLEAN.sub("", cleaned)
    # Stop at the next declaration label if the OCR line ran on.
    cleaned = re.split(r"\b(?:mrp|net|batch|mfg|packed|consumer)\b", cleaned, flags=re.IGNORECASE)[
        0
    ]
    cleaned = cleaned.strip(" .,:;-–")
    if not 2 <= len(cleaned) <= 60 or not re.search(r"[A-Za-z\u0900-\u097F]", cleaned):
        return None
    return {"kind": "country", "value": cleaned, "display": cleaned}


_VEG_MARK = re.compile(
    r"(veg(?:etarian)?|non[\s\-]?veg(?:etarian)?|शाकाहारी|मांसाहारी)", re.IGNORECASE
)


def _parse_veg_mark(raw: str) -> dict[str, Any] | None:
    match = _VEG_MARK.search(raw)
    if not match:
        return None
    token = match.group(1).lower().replace(" ", "").replace("-", "")
    is_non_veg = token.startswith("non") or "मांसाहारी" in match.group(1)
    value = "non_vegetarian" if is_non_veg else "vegetarian"
    return {
        "kind": "veg_mark",
        "value": value,
        "display": "Non-vegetarian" if is_non_veg else "Vegetarian",
    }


# --------------------------------------------------------------------------
# Declaration table
# --------------------------------------------------------------------------
SPECS: tuple[DeclarationSpec, ...] = (
    DeclarationSpec(
        DeclarationType.MRP,
        labels=(
            "maximum retail price",
            "max retail price",
            "m.r.p.",
            "m.r.p",
            "mrp",
            "retail sale price",
            "अधिकतम खुदरा मूल्य",
            "अधिकतम फुटकर मूल्य",
            "अधि. खुदरा मूल्य",
        ),
        parser=_parse_price,
        negative_labels=("unit sale price", "per unit", "unit price"),
    ),
    DeclarationSpec(
        DeclarationType.UNIT_SALE_PRICE,
        labels=(
            "unit sale price",
            "unit price",
            "price per unit",
            "per unit price",
            "इकाई विक्रय मूल्य",
            "प्रति इकाई मूल्य",
        ),
        parser=_parse_price,
    ),
    DeclarationSpec(
        DeclarationType.NET_QUANTITY,
        labels=(
            "net quantity",
            "net qty",
            "net weight",
            "net wt",
            "net content",
            "net contents",
            "net vol",
            "net volume",
            "quantity",
            "शुद्ध मात्रा",
            "शुद्ध वजन",
            "मात्रा",
            "कुल मात्रा",
        ),
        parser=_parse_quantity,
    ),
    DeclarationSpec(
        DeclarationType.MANUFACTURER,
        labels=(
            "manufactured by",
            "mfd by",
            "mfg by",
            "manufacturer",
            "manufactured at",
            "निर्माता",
            "द्वारा निर्मित",
        ),
        parser=_parse_free_text(4, 240),
    ),
    DeclarationSpec(
        DeclarationType.PACKER,
        labels=("packed by", "packer", "पैक किया गया", "पैकर"),
        parser=_parse_free_text(4, 240),
    ),
    DeclarationSpec(
        DeclarationType.IMPORTER,
        labels=("imported by", "importer", "आयातक", "द्वारा आयातित"),
        parser=_parse_free_text(4, 240),
    ),
    DeclarationSpec(
        DeclarationType.MARKETER,
        labels=("marketed by", "marketer", "विपणक", "द्वारा विपणित"),
        parser=_parse_free_text(4, 240),
    ),
    DeclarationSpec(
        DeclarationType.CONSUMER_CARE_EMAIL,
        labels=(
            "consumer care",
            "customer care",
            "consumer complaints",
            "email",
            "e-mail",
            "उपभोक्ता सेवा",
            "ग्राहक सेवा",
        ),
        parser=_parse_email,
        shape_pattern=_EMAIL,
    ),
    DeclarationSpec(
        DeclarationType.CONSUMER_CARE_PHONE,
        labels=(
            "consumer care",
            "customer care",
            "helpline",
            "toll free",
            "toll-free",
            "phone",
            "tel",
            "contact",
            "उपभोक्ता सेवा",
            "हेल्पलाइन",
        ),
        parser=_parse_phone,
    ),
    DeclarationSpec(
        DeclarationType.CONSUMER_CARE_NAME,
        labels=(
            "consumer care details",
            "for complaints contact",
            "consumer care officer",
            "customer relations",
        ),
        parser=_parse_free_text(4, 240),
    ),
    DeclarationSpec(
        DeclarationType.COUNTRY_OF_ORIGIN,
        labels=(
            "country of origin",
            "made in",
            "product of",
            "origin",
            "उत्पत्ति का देश",
            "मूल देश",
            "में निर्मित",
        ),
        parser=_parse_country,
    ),
    DeclarationSpec(
        DeclarationType.COMMON_GENERIC_NAME,
        labels=(
            "common name",
            "generic name",
            "common or generic name",
            "product name",
            "सामान्य नाम",
        ),
        parser=_parse_free_text(3, 240),
    ),
    DeclarationSpec(
        DeclarationType.DATE_OF_MANUFACTURE,
        labels=(
            "date of manufacture",
            "manufactured on",
            "mfg date",
            "mfg dt",
            "mfg",
            "mfd",
            "date of mfg",
            "निर्माण तिथि",
            "निर्माण दिनांक",
        ),
        parser=_parse_date,
        negative_labels=("packed on", "best before", "use by", "expiry"),
    ),
    DeclarationSpec(
        DeclarationType.DATE_OF_PACKING,
        labels=(
            "date of packing",
            "packed on",
            "packing date",
            "pkd on",
            "pkd",
            "पैकिंग तिथि",
            "पैक करने की तिथि",
        ),
        parser=_parse_date,
    ),
    DeclarationSpec(
        DeclarationType.DATE_OF_IMPORT,
        labels=("date of import", "imported on", "आयात तिथि"),
        parser=_parse_date,
    ),
    DeclarationSpec(
        DeclarationType.BEST_BEFORE,
        labels=(
            "best before",
            "best before use",
            "use by",
            "expiry date",
            "exp date",
            "expires on",
            "उपयोग की तिथि",
            "सर्वोत्तम",
        ),
        parser=_parse_free_text(2, 200),
    ),
    DeclarationSpec(
        DeclarationType.BATCH_NUMBER,
        labels=("batch no", "batch number", "batch", "b.no", "बैच संख्या", "बैच"),
        parser=_parse_free_text(2, 40),
    ),
    DeclarationSpec(
        DeclarationType.LOT_NUMBER,
        labels=("lot no", "lot number", "lot", "लॉट संख्या"),
        parser=_parse_free_text(2, 40),
    ),
    DeclarationSpec(
        DeclarationType.INGREDIENTS,
        labels=("ingredients", "ingredient", "composition", "सामग्री", "अवयव"),
        parser=_parse_free_text(6, 2000),
    ),
    DeclarationSpec(
        DeclarationType.NUTRITION,
        labels=(
            "nutritional information",
            "nutrition information",
            "nutritional facts",
            "nutrition facts",
            "पोषण संबंधी जानकारी",
        ),
        parser=_parse_free_text(6, 2000),
    ),
    DeclarationSpec(
        DeclarationType.ALLERGEN_STATEMENT,
        labels=("allergen", "contains", "may contain", "एलर्जी"),
        parser=_parse_free_text(4, 500),
    ),
    DeclarationSpec(
        DeclarationType.VEG_NONVEG_MARK,
        labels=("veg", "non veg", "non-veg", "vegetarian", "शाकाहारी", "मांसाहारी"),
        parser=_parse_veg_mark,
        shape_pattern=_VEG_MARK,
    ),
    DeclarationSpec(
        DeclarationType.FSSAI_LICENCE,
        labels=("fssai", "lic no", "licence no", "license no", "एफएसएसएआई"),
        parser=_parse_fssai,
    ),
)

SPECS_BY_TYPE: dict[DeclarationType, DeclarationSpec] = {
    spec.declaration_type: spec for spec in SPECS
}


def _label_pattern(label: str) -> re.Pattern[str]:
    """Match a label allowing OCR-inserted spaces and dots between characters.

    "MRP" also matches "M R P" and "M.R.P". Word boundaries are applied only for
    Latin labels, since Devanagari has no word-boundary equivalent in this regex
    dialect.
    """
    if re.search(r"[\u0900-\u097F]", label):
        return re.compile(re.escape(label), re.IGNORECASE | re.UNICODE)
    flexible = r"[\s.]*".join(re.escape(character) for character in label if character not in " .")
    return re.compile(rf"(?<![A-Za-z]){flexible}(?![A-Za-z])", re.IGNORECASE)


_COMPILED_LABELS: dict[str, re.Pattern[str]] = {}


def compiled_label(label: str) -> re.Pattern[str]:
    if label not in _COMPILED_LABELS:
        _COMPILED_LABELS[label] = _label_pattern(label)
    return _COMPILED_LABELS[label]
