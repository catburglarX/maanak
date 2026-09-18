"""Prices.

Every monetary value is a ``Decimal`` parsed from the printed digits. Binary
floating point is never used: ``0.1 + 0.2 != 0.3`` in binary floating point, and a
legal conclusion about a printed price cannot rest on that.

Rounding rule, applied only where a computed price is compared with a printed one:
half-up to two decimal places (paise). Half-up is the convention used in Indian
retail pricing and is the behaviour a reader expects from a printed price. The rule
is stated on every finding that performs a division, together with the unrounded
intermediate value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .units import DEVANAGARI_DIGITS

PAISE = Decimal("0.01")
CURRENCY_INR = "INR"

#: Symbols and words that introduce a rupee amount.
CURRENCY_TOKENS = (
    "₹",
    "rs.",
    "rs",
    "inr",
    "rupees",
    "rupee",
    "र\u0942.",
    "रु.",
    "रु",
    "रुपये",
    "रूपये",
)

# Indian grouping (1,50,000) and Western grouping (150,000) both appear in print.
_AMOUNT = r"(\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"

_CURRENCY_PREFIX = "|".join(re.escape(token) for token in CURRENCY_TOKENS)

PRICE_PATTERN = re.compile(
    rf"(?:{_CURRENCY_PREFIX})\s*{_AMOUNT}|{_AMOUNT}\s*(?:{_CURRENCY_PREFIX})",
    re.IGNORECASE | re.UNICODE,
)

#: Values outside this band on a retail package almost always indicate a
#: misread rather than a genuine price. Flagged, never silently corrected.
IMPLAUSIBLE_BELOW = Decimal("0.50")
IMPLAUSIBLE_ABOVE = Decimal("1000000")


@dataclass(frozen=True)
class Money:
    """A printed monetary amount."""

    amount: Decimal
    currency: str = CURRENCY_INR
    source_text: str | None = None
    #: True when the printed amount carried more than two decimal places.
    sub_paise_precision: bool = False

    @property
    def display(self) -> str:
        return f"₹{self.amount:,.2f}"

    @property
    def plausible(self) -> bool:
        return IMPLAUSIBLE_BELOW <= self.amount <= IMPLAUSIBLE_ABOVE

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "money",
            "amount": format(self.amount, "f"),
            "currency": self.currency,
            "display": self.display,
            "plausible": self.plausible,
            "source_text": self.source_text,
        }


def quantise_paise(value: Decimal) -> Decimal:
    """Round to two decimal places, half-up.

    Used only when comparing a computed value with a printed one. The unrounded
    value is always recorded alongside so the step is auditable.
    """
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


def parse_amount(raw: str) -> Decimal | None:
    if not raw:
        return None
    text = raw.strip().translate(DEVANAGARI_DIGITS).replace(",", "").replace(" ", "")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if value.is_nan() or value.is_infinite() or value < 0:
        return None
    return value


def parse_money(text: str) -> Money | None:
    """Parse the first currency-marked amount in a string.

    A bare number is deliberately not treated as a price: on a package almost every
    number is something else, so requiring a currency marker avoids reading a batch
    code as an MRP.
    """
    if not text:
        return None
    prepared = text.translate(DEVANAGARI_DIGITS)
    match = PRICE_PATTERN.search(prepared)
    if not match:
        return None
    raw_amount = match.group(1) or match.group(2)
    amount = parse_amount(raw_amount)
    if amount is None:
        return None
    return Money(
        amount=amount,
        source_text=match.group(0).strip(),
        sub_paise_precision=_has_sub_paise(raw_amount),
    )


def find_all_money(text: str) -> list[Money]:
    if not text:
        return []
    prepared = text.translate(DEVANAGARI_DIGITS)
    results: list[Money] = []
    for match in PRICE_PATTERN.finditer(prepared):
        raw_amount = match.group(1) or match.group(2)
        amount = parse_amount(raw_amount)
        if amount is None:
            continue
        results.append(
            Money(
                amount=amount,
                source_text=match.group(0).strip(),
                sub_paise_precision=_has_sub_paise(raw_amount),
            )
        )
    return results


def parse_bare_amount(text: str) -> Money | None:
    """Parse a number with no currency marker.

    Only for a field already known to be a price, such as a value an officer typed
    into the MRP box.
    """
    amount = parse_amount(text)
    if amount is None:
        return None
    return Money(amount=amount, source_text=text.strip(), sub_paise_precision=_has_sub_paise(text))


def _has_sub_paise(raw: str) -> bool:
    cleaned = raw.strip().replace(",", "")
    if "." not in cleaned:
        return False
    return len(cleaned.split(".", 1)[1]) > 2


def unit_sale_price(
    total_price: Decimal, quantity_base_amount: Decimal, *, per_units: Decimal
) -> tuple[Decimal, Decimal, list[str]]:
    """Price for ``per_units`` of the base unit.

    Returns ``(exact_value, rounded_value, calculation_steps)``. The exact value is
    kept so a finding can show that, for example, 45/250 is 0.18 exactly rather than
    a rounded 0.18 that happens to match.
    """
    if quantity_base_amount <= 0:
        raise ValueError("quantity must be greater than zero")

    exact = (total_price / quantity_base_amount) * per_units
    rounded = quantise_paise(exact)
    steps = [
        f"printed price = {format(total_price, 'f')}",
        f"net quantity in base units = {format(quantity_base_amount, 'f')}",
        f"price per base unit = {format(total_price, 'f')} / "
        f"{format(quantity_base_amount, 'f')} = {format(exact / per_units, 'f')}",
        f"price per {format(per_units, 'f')} base units = {format(exact, 'f')}",
        f"rounded half-up to paise = {format(rounded, 'f')}",
    ]
    return exact, rounded, steps


def amounts_match(first: Decimal, second: Decimal, *, tolerance: Decimal = PAISE) -> bool:
    """Compare two amounts within a tolerance, default one paisa.

    A one-paisa tolerance absorbs the rounding a packer legitimately applies when
    printing a computed unit price.
    """
    return abs(quantise_paise(first) - quantise_paise(second)) <= tolerance
