"""GTIN handling.

Implements the GS1 modulo-10 check digit as published in the GS1 General
Specifications: multiply digits alternately by 3 and 1 from the right (excluding
the check digit), sum, and the check digit is the amount needed to reach the next
multiple of ten.

Supported lengths:

* GTIN-8 (EAN-8)
* GTIN-12 (UPC-A)
* GTIN-13 (EAN-13)
* GTIN-14 (ITF-14, used on outer cases)

An Indian GS1 prefix is 890. That is recorded as an observation only: prefix does
not prove where a product was made, and it is never used to conclude anything about
country of origin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

VALID_LENGTHS = (8, 12, 13, 14)
INDIA_GS1_PREFIX = "890"
_NON_DIGIT = re.compile(r"[^0-9]")


@dataclass(frozen=True)
class GtinCheck:
    """Result of validating a candidate GTIN."""

    acceptable: bool
    normalised: str | None
    length: int | None
    check_digit_valid: bool
    problem: str | None = None
    india_prefix: bool = False

    @property
    def scheme(self) -> str | None:
        return {8: "ean8", 12: "upca", 13: "gtin", 14: "itf14"}.get(self.length or 0)


def normalise(value: str) -> str:
    """Strip spaces, hyphens and any other separator."""
    return _NON_DIGIT.sub("", value or "")


def compute_check_digit(digits_without_check: str) -> int:
    """The GS1 modulo-10 check digit for a body of digits.

    Weights alternate 3, 1 starting from the rightmost body digit.
    """
    if not digits_without_check.isdigit():
        raise ValueError("check digit input must be digits only")
    total = 0
    for position, character in enumerate(reversed(digits_without_check)):
        weight = 3 if position % 2 == 0 else 1
        total += int(character) * weight
    return (10 - (total % 10)) % 10


def validate_check_digit(value: str) -> bool:
    digits = normalise(value)
    if len(digits) not in VALID_LENGTHS:
        return False
    return compute_check_digit(digits[:-1]) == int(digits[-1])


def check(value: str) -> GtinCheck:
    """Validate a candidate GTIN and explain any problem in plain language."""
    digits = normalise(value)
    if not digits:
        return GtinCheck(False, None, None, False, "Enter the digits printed under the barcode.")
    if len(digits) not in VALID_LENGTHS:
        return GtinCheck(
            False,
            None,
            len(digits),
            False,
            (
                f"A barcode number has 8, 12, 13 or 14 digits. This one has {len(digits)}. "
                "Check for a missing or extra digit."
            ),
        )

    valid = compute_check_digit(digits[:-1]) == int(digits[-1])
    if not valid:
        expected = compute_check_digit(digits[:-1])
        return GtinCheck(
            False,
            digits,
            len(digits),
            False,
            (
                f"The last digit should be {expected} for this number. "
                "Re-read the digits printed under the barcode."
            ),
            india_prefix=digits.startswith(INDIA_GS1_PREFIX),
        )

    return GtinCheck(
        True,
        digits,
        len(digits),
        True,
        None,
        india_prefix=digits.startswith(INDIA_GS1_PREFIX),
    )


def to_gtin14(value: str) -> str | None:
    """Zero-pad a valid GTIN to 14 digits for consistent comparison."""
    result = check(value)
    if not result.acceptable or result.normalised is None:
        return None
    return result.normalised.rjust(14, "0")


def format_for_display(value: str) -> str:
    """Group digits for readability without changing the value."""
    digits = normalise(value)
    if len(digits) == 13:
        return f"{digits[0]} {digits[1:7]} {digits[7:13]}"
    if len(digits) == 12:
        return f"{digits[0]} {digits[1:6]} {digits[6:11]} {digits[11]}"
    if len(digits) == 8:
        return f"{digits[:4]} {digits[4:]}"
    if len(digits) == 14:
        return f"{digits[0]} {digits[1:8]} {digits[8:14]}"
    return digits
