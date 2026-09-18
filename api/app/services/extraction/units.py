"""Units of measurement.

Net quantity declarations use a small, fixed set of units. Every conversion is
exact: factors are ``Decimal`` and all of them are powers of ten or exact integer
ratios, so converting 1.5 kg to grams gives exactly 1500, never 1499.9999999.

Base unit per dimension:

===========  ==========  =========================================
Dimension    Base unit   Notes
===========  ==========  =========================================
weight       g           kg, mg, and the traditional quintal/tonne
volume       ml          l, cl, dl, kl
length       m           mm, cm, km
area         m2          cm2, mm2
count        unit        pieces, numbers, pairs, dozens
===========  ==========  =========================================

Weight and volume are kept as separate dimensions and never converted between each
other. Treating 1 ml as 1 g would be wrong for anything other than water, and a
comparison across dimensions is reported as not-comparable instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ...domain.enums import QuantityKind

#: unit symbol -> (dimension, factor to base unit)
UNIT_TABLE: dict[str, tuple[QuantityKind, Decimal]] = {
    # Weight, base gram
    "mcg": (QuantityKind.WEIGHT, Decimal("0.000001")),
    "mg": (QuantityKind.WEIGHT, Decimal("0.001")),
    "g": (QuantityKind.WEIGHT, Decimal("1")),
    "gm": (QuantityKind.WEIGHT, Decimal("1")),
    "gms": (QuantityKind.WEIGHT, Decimal("1")),
    "gram": (QuantityKind.WEIGHT, Decimal("1")),
    "grams": (QuantityKind.WEIGHT, Decimal("1")),
    "kg": (QuantityKind.WEIGHT, Decimal("1000")),
    "kgs": (QuantityKind.WEIGHT, Decimal("1000")),
    "kilogram": (QuantityKind.WEIGHT, Decimal("1000")),
    "kilograms": (QuantityKind.WEIGHT, Decimal("1000")),
    "quintal": (QuantityKind.WEIGHT, Decimal("100000")),
    "tonne": (QuantityKind.WEIGHT, Decimal("1000000")),
    "t": (QuantityKind.WEIGHT, Decimal("1000000")),
    # Volume, base millilitre
    "ml": (QuantityKind.VOLUME, Decimal("1")),
    "mls": (QuantityKind.VOLUME, Decimal("1")),
    "millilitre": (QuantityKind.VOLUME, Decimal("1")),
    "milliliter": (QuantityKind.VOLUME, Decimal("1")),
    "cl": (QuantityKind.VOLUME, Decimal("10")),
    "dl": (QuantityKind.VOLUME, Decimal("100")),
    "l": (QuantityKind.VOLUME, Decimal("1000")),
    "ltr": (QuantityKind.VOLUME, Decimal("1000")),
    "ltrs": (QuantityKind.VOLUME, Decimal("1000")),
    "litre": (QuantityKind.VOLUME, Decimal("1000")),
    "liter": (QuantityKind.VOLUME, Decimal("1000")),
    "litres": (QuantityKind.VOLUME, Decimal("1000")),
    "liters": (QuantityKind.VOLUME, Decimal("1000")),
    "kl": (QuantityKind.VOLUME, Decimal("1000000")),
    # Length, base metre
    "mm": (QuantityKind.LENGTH, Decimal("0.001")),
    "cm": (QuantityKind.LENGTH, Decimal("0.01")),
    "m": (QuantityKind.LENGTH, Decimal("1")),
    "metre": (QuantityKind.LENGTH, Decimal("1")),
    "meter": (QuantityKind.LENGTH, Decimal("1")),
    "metres": (QuantityKind.LENGTH, Decimal("1")),
    "meters": (QuantityKind.LENGTH, Decimal("1")),
    "km": (QuantityKind.LENGTH, Decimal("1000")),
    # Area, base square metre
    "mm2": (QuantityKind.AREA, Decimal("0.000001")),
    "cm2": (QuantityKind.AREA, Decimal("0.0001")),
    "m2": (QuantityKind.AREA, Decimal("1")),
    # Count, base single unit
    "n": (QuantityKind.COUNT, Decimal("1")),
    "no": (QuantityKind.COUNT, Decimal("1")),
    "nos": (QuantityKind.COUNT, Decimal("1")),
    "pc": (QuantityKind.COUNT, Decimal("1")),
    "pcs": (QuantityKind.COUNT, Decimal("1")),
    "piece": (QuantityKind.COUNT, Decimal("1")),
    "pieces": (QuantityKind.COUNT, Decimal("1")),
    "unit": (QuantityKind.COUNT, Decimal("1")),
    "units": (QuantityKind.COUNT, Decimal("1")),
    "tablet": (QuantityKind.COUNT, Decimal("1")),
    "tablets": (QuantityKind.COUNT, Decimal("1")),
    "sachet": (QuantityKind.COUNT, Decimal("1")),
    "sachets": (QuantityKind.COUNT, Decimal("1")),
    "pair": (QuantityKind.COUNT, Decimal("2")),
    "pairs": (QuantityKind.COUNT, Decimal("2")),
    "dozen": (QuantityKind.COUNT, Decimal("12")),
    "dozens": (QuantityKind.COUNT, Decimal("12")),
}

#: Hindi and Devanagari unit words seen on Indian packaging.
HINDI_UNITS: dict[str, str] = {
    "ग्राम": "g",
    "ग्रा": "g",
    "किलोग्राम": "kg",
    "किग्रा": "kg",
    "किलो": "kg",
    "मिलीग्राम": "mg",
    "मिलीलीटर": "ml",
    "मिली": "ml",
    "लीटर": "l",
    "मीटर": "m",
    "सेंटीमीटर": "cm",
    "नग": "nos",
    "पीस": "pcs",
    "इकाई": "unit",
}

#: Alias -> canonical symbol. Keeps the stored unit consistent, so a package
#: printed "100 gm" and one printed "100 g" produce the same record.
CANONICAL_UNIT: dict[str, str] = {
    "gm": "g",
    "gms": "g",
    "gram": "g",
    "grams": "g",
    "kgs": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "mls": "ml",
    "millilitre": "ml",
    "milliliter": "ml",
    "ltr": "l",
    "ltrs": "l",
    "litre": "l",
    "liter": "l",
    "litres": "l",
    "liters": "l",
    "metre": "m",
    "meter": "m",
    "metres": "m",
    "meters": "m",
    "n": "unit",
    "no": "unit",
    "nos": "unit",
    "units": "unit",
    "pc": "pcs",
    "piece": "pcs",
    "pieces": "pcs",
    "pairs": "pair",
    "dozens": "dozen",
    "tablets": "tablet",
    "sachets": "sachet",
    "t": "tonne",
}

#: Canonical display form per unit, so the record shows a consistent symbol.
CANONICAL_DISPLAY: dict[str, str] = {
    "g": "g",
    "kg": "kg",
    "mg": "mg",
    "ml": "ml",
    "l": "l",
    "m": "m",
    "cm": "cm",
    "mm": "mm",
    "m2": "m²",
    "unit": "N",
}

#: Preferred unit per dimension when presenting a converted value.
BASE_UNIT: dict[QuantityKind, str] = {
    QuantityKind.WEIGHT: "g",
    QuantityKind.VOLUME: "ml",
    QuantityKind.LENGTH: "m",
    QuantityKind.AREA: "m2",
    QuantityKind.COUNT: "unit",
}

# Devanagari digits map to ASCII so a Hindi-printed quantity parses.
DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

_NUMBER = r"(\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
_UNIT_ALTERNATIVES = "|".join(sorted((re.escape(key) for key in UNIT_TABLE), key=len, reverse=True))
_HINDI_ALTERNATIVES = "|".join(
    sorted((re.escape(key) for key in HINDI_UNITS), key=len, reverse=True)
)

QUANTITY_PATTERN = re.compile(
    rf"(?<![\d.]){_NUMBER}\s*(?:±\s*[\d.]+\s*\w*\s*)?({_UNIT_ALTERNATIVES}|{_HINDI_ALTERNATIVES})(?![A-Za-z0-9])",
    re.IGNORECASE | re.UNICODE,
)

#: "12 x 50 g" or "4 X 100ml": a multipiece declaration.
MULTIPIECE_PATTERN = re.compile(
    rf"(\d{{1,4}})\s*[xX×*]\s*{_NUMBER}\s*({_UNIT_ALTERNATIVES})(?![A-Za-z0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Quantity:
    """A parsed quantity with an exact conversion to its base unit."""

    amount: Decimal
    unit: str
    dimension: QuantityKind
    base_amount: Decimal
    base_unit: str
    #: Present for "12 x 50 g" style declarations.
    pieces: int | None = None
    per_piece_amount: Decimal | None = None
    source_text: str | None = None

    @property
    def display(self) -> str:
        symbol = CANONICAL_DISPLAY.get(self.unit, self.unit)
        amount = format(self.amount.normalize(), "f")
        if self.pieces:
            per_piece = format((self.per_piece_amount or Decimal(0)).normalize(), "f")
            return f"{self.pieces} × {per_piece} {symbol} ({amount} {symbol} total)"
        return f"{amount} {symbol}"

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "quantity",
            "amount": format(self.amount, "f"),
            "unit": self.unit,
            "dimension": self.dimension.value,
            "base_amount": format(self.base_amount, "f"),
            "base_unit": self.base_unit,
            "pieces": self.pieces,
            "per_piece_amount": (
                format(self.per_piece_amount, "f") if self.per_piece_amount is not None else None
            ),
            "display": self.display,
            "source_text": self.source_text,
        }


def normalise_unit(raw: str) -> str | None:
    """Map a written unit to its canonical table key.

    Handles Hindi words, punctuation and aliases, so "gm", "gram" and "g" all
    resolve to "g".
    """
    if not raw:
        return None
    cleaned = raw.strip().lower().rstrip(".").replace(" ", "")
    if cleaned in HINDI_UNITS:
        return CANONICAL_UNIT.get(HINDI_UNITS[cleaned], HINDI_UNITS[cleaned])
    if raw.strip() in HINDI_UNITS:
        mapped = HINDI_UNITS[raw.strip()]
        return CANONICAL_UNIT.get(mapped, mapped)
    # Superscript and written square forms.
    cleaned = cleaned.replace("²", "2").replace("sq.", "").replace("sq", "")
    if cleaned in UNIT_TABLE:
        return CANONICAL_UNIT.get(cleaned, cleaned)
    return None


def parse_decimal(raw: str) -> Decimal | None:
    """Parse a printed number.

    Handles Devanagari digits and Indian digit grouping (1,50,000 as well as
    150,000). A comma is only ever a group separator here: no locale on an Indian
    package uses it as a decimal point.
    """
    if not raw:
        return None
    text = raw.strip().translate(DEVANAGARI_DIGITS).replace(" ", "")
    text = text.replace(",", "")
    if not text or text.count(".") > 1:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if value.is_nan() or value.is_infinite():
        return None
    return value


def convert(amount: Decimal, unit: str) -> tuple[QuantityKind, Decimal, str] | None:
    """Convert to the base unit for the unit's dimension. Exact."""
    key = normalise_unit(unit)
    if key is None:
        return None
    dimension, factor = UNIT_TABLE[key]
    return dimension, (amount * factor), BASE_UNIT[dimension]


def build_quantity(
    amount: Decimal, unit: str, *, source_text: str | None = None
) -> Quantity | None:
    converted = convert(amount, unit)
    if converted is None:
        return None
    dimension, base_amount, base_unit = converted
    return Quantity(
        amount=amount,
        unit=normalise_unit(unit) or unit,
        dimension=dimension,
        base_amount=base_amount,
        base_unit=base_unit,
        source_text=source_text,
    )


def parse_quantity(text: str) -> Quantity | None:
    """Parse the first quantity in a string, preferring a multipiece form.

    "12 x 50 g" is read as 12 pieces of 50 g, total 600 g, rather than as 50 g,
    because the total is what the net quantity rule tests.
    """
    if not text:
        return None
    prepared = text.translate(DEVANAGARI_DIGITS)

    multipiece = MULTIPIECE_PATTERN.search(prepared)
    if multipiece:
        pieces_value = parse_decimal(multipiece.group(1))
        per_piece = parse_decimal(multipiece.group(2))
        unit = multipiece.group(3)
        if pieces_value and per_piece:
            converted = convert(per_piece * pieces_value, unit)
            if converted:
                dimension, base_amount, base_unit = converted
                return Quantity(
                    amount=per_piece * pieces_value,
                    unit=normalise_unit(unit) or unit,
                    dimension=dimension,
                    base_amount=base_amount,
                    base_unit=base_unit,
                    pieces=int(pieces_value),
                    per_piece_amount=per_piece,
                    source_text=multipiece.group(0),
                )

    match = QUANTITY_PATTERN.search(prepared)
    if not match:
        return None
    amount = parse_decimal(match.group(1))
    if amount is None:
        return None
    return build_quantity(amount, match.group(2), source_text=match.group(0))


def find_all_quantities(text: str) -> list[Quantity]:
    """Every quantity in a string, in order of appearance."""
    if not text:
        return []
    prepared = text.translate(DEVANAGARI_DIGITS)
    found: list[Quantity] = []
    for match in QUANTITY_PATTERN.finditer(prepared):
        amount = parse_decimal(match.group(1))
        if amount is None:
            continue
        quantity = build_quantity(amount, match.group(2), source_text=match.group(0))
        if quantity is not None:
            found.append(quantity)
    return found


def same_dimension(first: Quantity, second: Quantity) -> bool:
    return first.dimension == second.dimension


def to_unit(quantity: Quantity, target_unit: str) -> Decimal | None:
    """Express a quantity in another unit of the same dimension. Exact."""
    key = normalise_unit(target_unit)
    if key is None:
        return None
    dimension, factor = UNIT_TABLE[key]
    if dimension != quantity.dimension:
        return None
    return quantity.base_amount / factor
