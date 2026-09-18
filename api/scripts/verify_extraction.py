"""Verify the extraction and normalisation layer.

docker compose run --rm --no-deps --entrypoint python api scripts/verify_extraction.py
"""

from __future__ import annotations

import sys
from decimal import Decimal

from app.domain.enums import DeclarationType, QuantityKind
from app.services.extraction import dates, money, units
from app.services.extraction.dates import Precision
from app.services.extraction.fields import SPECS_BY_TYPE
from app.services.extraction.pipeline import extract_matches
from app.services.ocr import Line, OcrOutcome, Word

failures: list[str] = []
checks = 0


def check(condition: bool, description: str) -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok    {description}")
    else:
        failures.append(description)
        print(f"  FAIL  {description}")


def verify_units() -> None:
    print("quantity parsing")
    cases = [
        ("Net Quantity: 500 g", Decimal("500"), "g", QuantityKind.WEIGHT, Decimal("500")),
        ("Net Wt. 1.5 kg", Decimal("1.5"), "kg", QuantityKind.WEIGHT, Decimal("1500")),
        ("NET QTY 250ml", Decimal("250"), "ml", QuantityKind.VOLUME, Decimal("250")),
        ("Contents: 2 L", Decimal("2"), "l", QuantityKind.VOLUME, Decimal("2000")),
        ("Net quantity 100 gm", Decimal("100"), "g", QuantityKind.WEIGHT, Decimal("100")),
        ("शुद्ध मात्रा ५०० ग्राम", Decimal("500"), "g", QuantityKind.WEIGHT, Decimal("500")),
        ("Net Quantity 20 Nos", Decimal("20"), "unit", QuantityKind.COUNT, Decimal("20")),
        ("Length 1.2 m", Decimal("1.2"), "m", QuantityKind.LENGTH, Decimal("1.2")),
    ]
    for text, amount, unit, dimension, base in cases:
        parsed = units.parse_quantity(text)
        ok = (
            parsed is not None
            and parsed.amount == amount
            and parsed.unit == unit
            and parsed.dimension == dimension
            and parsed.base_amount == base
        )
        got = (
            f"{parsed.amount} {parsed.unit} -> {parsed.base_amount} {parsed.base_unit}"
            if parsed
            else "None"
        )
        check(ok, f"{text!r} -> {amount} {unit} ({base} base) [got {got}]")

    print("exact conversion, no floating point drift")
    parsed = units.parse_quantity("1.5 kg")
    assert parsed is not None
    check(
        parsed.base_amount == Decimal("1500"),
        f"1.5 kg is exactly 1500 g (got {parsed.base_amount})",
    )
    parsed = units.parse_quantity("0.1 kg")
    assert parsed is not None
    check(
        parsed.base_amount == Decimal("100.0"),
        f"0.1 kg is exactly 100 g (got {parsed.base_amount})",
    )
    triple = units.parse_quantity("3 mg")
    assert triple is not None
    check(
        triple.base_amount == Decimal("0.003"),
        f"3 mg is exactly 0.003 g (got {triple.base_amount})",
    )

    print("multipiece declarations")
    parsed = units.parse_quantity("12 x 50 g")
    ok = (
        parsed is not None
        and parsed.pieces == 12
        and parsed.per_piece_amount == Decimal("50")
        and parsed.amount == Decimal("600")
        and parsed.base_amount == Decimal("600")
    )
    check(
        ok, f"'12 x 50 g' totals 600 g across 12 pieces [got {parsed.display if parsed else None}]"
    )

    print("dimension isolation")
    weight = units.parse_quantity("500 g")
    volume = units.parse_quantity("500 ml")
    assert weight and volume
    check(
        not units.same_dimension(weight, volume), "grams and millilitres are not the same dimension"
    )
    check(units.to_unit(weight, "ml") is None, "cross-dimension conversion refused")
    check(units.to_unit(weight, "kg") == Decimal("0.5"), "500 g converts to exactly 0.5 kg")

    print("rejections")
    check(units.parse_quantity("Batch 5A") is None, "batch code is not read as a quantity")
    check(units.parse_quantity("") is None, "empty string yields nothing")
    check(units.normalise_unit("furlong") is None, "unknown unit rejected")


def verify_money() -> None:
    print("price parsing")
    cases = [
        ("MRP ₹45.00", Decimal("45.00")),
        ("M.R.P. Rs. 120", Decimal("120")),
        ("Maximum Retail Price INR 1,250.50", Decimal("1250.50")),
        ("Rs 1,50,000", Decimal("150000")),
        ("99.99 Rs", Decimal("99.99")),
        ("अधिकतम खुदरा मूल्य रु. 60.50", Decimal("60.50")),
    ]
    for text, expected in cases:
        parsed = money.parse_money(text)
        ok = parsed is not None and parsed.amount == expected
        check(ok, f"{text!r} -> {expected} [got {parsed.amount if parsed else None}]")

    check(money.parse_money("Batch 4570") is None, "bare number is not read as a price")
    check(money.parse_money("Net 500 g") is None, "quantity is not read as a price")

    print("decimal exactness")
    total = money.parse_money("Rs 0.10")
    other = money.parse_money("Rs 0.20")
    assert total and other
    check(
        total.amount + other.amount == Decimal("0.30"),
        f"0.10 + 0.20 is exactly 0.30 (got {total.amount + other.amount})",
    )

    print("unit sale price arithmetic")
    exact, rounded, steps = money.unit_sale_price(
        Decimal("45.00"), Decimal("250"), per_units=Decimal("100")
    )
    check(exact == Decimal("18.000"), f"45.00 per 250 g is exactly 18 per 100 g (got {exact})")
    check(rounded == Decimal("18.00"), f"rounds to 18.00 (got {rounded})")
    check(len(steps) == 5, f"calculation records every step (got {len(steps)})")

    exact, rounded, _ = money.unit_sale_price(
        Decimal("10.00"), Decimal("3"), per_units=Decimal("1")
    )
    check(
        rounded == Decimal("3.33"),
        f"10/3 rounds half-up to 3.33 (got {rounded})",
    )
    exact, rounded, _ = money.unit_sale_price(
        Decimal("1.005"), Decimal("1"), per_units=Decimal("1")
    )
    check(rounded == Decimal("1.01"), f"1.005 rounds half-up to 1.01, not 1.00 (got {rounded})")

    print("tolerance")
    check(money.amounts_match(Decimal("18.00"), Decimal("18.01")), "one paisa difference matches")
    check(
        not money.amounts_match(Decimal("18.00"), Decimal("18.05")),
        "five paise difference does not match",
    )

    print("plausibility")
    implausible = money.parse_money("MRP Rs 0.01")
    check(
        implausible is not None and not implausible.plausible, "unrealistically low price flagged"
    )


def verify_dates() -> None:
    print("date markings")
    marking = dates.parse_date_marking("Mfg Date: 03/2026")
    check(
        marking is not None
        and marking.year == 2026
        and marking.month == 3
        and marking.day is None
        and marking.precision is Precision.MONTH,
        f"'03/2026' is March 2026 at month precision [got {marking.display if marking else None}]",
    )

    marking = dates.parse_date_marking("Packed on 12 Mar 2026")
    check(
        marking is not None and marking.precision is Precision.DAY and marking.day == 12,
        f"'12 Mar 2026' parsed at day precision [got {marking.display if marking else None}]",
    )

    marking = dates.parse_date_marking("MFG 05/03/2026")
    check(
        marking is not None and marking.is_ambiguous and len(marking.alternatives) == 2,
        f"'05/03/2026' reported as ambiguous [got {marking.alternatives if marking else None}]",
    )

    marking = dates.parse_date_marking("MFG 25/12/2026")
    check(
        marking is not None and not marking.is_ambiguous and marking.day == 25,
        "'25/12/2026' is unambiguous because 25 cannot be a month",
    )

    marking = dates.parse_date_marking("निर्माण तिथि मार्च 2026")
    check(
        marking is not None and marking.month == 3 and marking.year == 2026,
        f"Hindi month name parsed [got {marking.display if marking else None}]",
    )

    marking = dates.parse_date_marking("Mfg 03/24")
    check(
        marking is not None and marking.year == 2024,
        f"two-digit year expands to 2024 [got {marking.year if marking else None}]",
    )

    check(dates.parse_date_marking("Batch AB12") is None, "batch code is not read as a date")

    first = dates.parse_date_marking("01/2026")
    second = dates.parse_date_marking("07/2026")
    assert first and second
    check(dates.months_between(first, second) == 6, "six months between Jan and Jul 2026")


def _fake_ocr(lines: list[str]) -> OcrOutcome:
    """Build an OcrOutcome with plausible geometry for pipeline testing."""
    words: list[Word] = []
    line_objects: list[Line] = []
    for line_number, line_text in enumerate(lines):
        indexes: list[int] = []
        cursor_x = 20
        top = 20 + line_number * 40
        for token in line_text.split(" "):
            width = max(8, len(token) * 11)
            words.append(
                Word(
                    text=token,
                    confidence=0.93,
                    box=[cursor_x, top, cursor_x + width, top + 26],
                    line_index=line_number,
                    block_index=0,
                    height_px=26.0,
                    script="latin",
                )
            )
            indexes.append(len(words) - 1)
            cursor_x += width + 11
        line_objects.append(
            Line(
                index=line_number,
                text=line_text,
                box=[20, top, cursor_x, top + 26],
                word_indexes=indexes,
                mean_confidence=0.93,
                script="latin",
            )
        )
    return OcrOutcome(
        profile="default",
        languages="eng+hin",
        engine="tesseract",
        engine_version="5.3.0",
        raw_text="\n".join(lines),
        words=words,
        lines=line_objects,
        mean_confidence=0.93,
        duration_ms=100,
    )


def verify_pipeline() -> None:
    print("declaration detection")
    outcome = _fake_ocr(
        [
            "SUNRISE ATTA",
            "Common Name: Whole Wheat Flour",
            "Net Quantity: 1 kg",
            "M.R.P. Rs. 58.00 (incl. of all taxes)",
            "Mfd. by: Sunrise Foods Private Limited",
            "Plot 12, Industrial Area, Gurugram, Haryana 122001",
            "Consumer care: care@sunrisefoods.example  1800 200 1234",
            "Country of Origin: India",
            "Batch No: SA2026C",
            "Mfg Date: 03/2026",
            "Best Before 9 months from packaging",
        ]
    )
    matches = extract_matches(outcome)
    found = {item.declaration_type: item for item in matches if item.parsed}

    expectations = [
        (DeclarationType.MRP, "58.00"),
        (DeclarationType.NET_QUANTITY, "1"),
        (DeclarationType.MANUFACTURER, "Sunrise Foods Private Limited"),
        (DeclarationType.COUNTRY_OF_ORIGIN, "India"),
        (DeclarationType.BATCH_NUMBER, "SA2026C"),
        (DeclarationType.COMMON_GENERIC_NAME, "Whole Wheat Flour"),
    ]
    for declaration_type, expected_fragment in expectations:
        item = found.get(declaration_type)
        value = ""
        if item:
            value = str(
                item.normalised_value.get("amount")
                or item.normalised_value.get("value")
                or item.normalised_value.get("display")
                or ""
            )
        check(
            item is not None and expected_fragment in value,
            f"{declaration_type.value} located as {expected_fragment!r} [got {value!r}]",
        )

    email = found.get(DeclarationType.CONSUMER_CARE_EMAIL)
    check(
        email is not None and email.normalised_value.get("value") == "care@sunrisefoods.example",
        f"consumer care email located [got {email.normalised_value.get('value') if email else None}]",
    )
    phone = found.get(DeclarationType.CONSUMER_CARE_PHONE)
    check(
        phone is not None and phone.normalised_value.get("value") == "18002001234",
        f"consumer care phone located [got {phone.normalised_value.get('value') if phone else None}]",
    )
    mfg = found.get(DeclarationType.DATE_OF_MANUFACTURE)
    check(
        mfg is not None and mfg.normalised_value.get("month") == 3,
        f"manufacture date located [got {mfg.normalised_value.get('display') if mfg else None}]",
    )

    print("regions and confidence")
    mrp = found[DeclarationType.MRP]
    check(
        mrp.span is not None and mrp.line_index is not None,
        "match records the line and character span",
    )
    check(0.0 < mrp.parse_confidence <= 1.0, f"confidence in range (got {mrp.parse_confidence})")
    check(mrp.label_found is not None, f"label recorded ({mrp.label_found!r})")
    check(
        "58.00" in mrp.matched_text or "58" in mrp.matched_text,
        f"verbatim text kept ({mrp.matched_text!r})",
    )

    print("label without value is still recorded")
    outcome = _fake_ocr(["Net Quantity:", "Batch No: XY99"])
    matches = extract_matches(outcome)
    quantity_matches = [
        item for item in matches if item.declaration_type == DeclarationType.NET_QUANTITY
    ]
    check(bool(quantity_matches), "a label with no readable value produces a match")
    check(
        quantity_matches and not quantity_matches[0].parsed,
        "that match is marked unparsed rather than invented",
    )

    print("MRP is not confused with unit sale price")
    outcome = _fake_ocr(["Unit Sale Price: Rs 23.20 per 100 g", "MRP Rs 58.00"])
    matches = extract_matches(outcome)
    by_type = {item.declaration_type: item for item in matches if item.parsed}
    mrp_value = by_type.get(DeclarationType.MRP)
    usp_value = by_type.get(DeclarationType.UNIT_SALE_PRICE)
    check(
        mrp_value is not None and str(mrp_value.normalised_value.get("amount")) == "58.00",
        f"MRP read as 58.00 [got {mrp_value.normalised_value.get('amount') if mrp_value else None}]",
    )
    check(
        usp_value is not None and str(usp_value.normalised_value.get("amount")) == "23.20",
        f"unit sale price read as 23.20 [got {usp_value.normalised_value.get('amount') if usp_value else None}]",
    )

    print("no text yields no declarations")
    check(extract_matches(_fake_ocr([])) == [], "empty OCR result produces no matches")

    print("declaration table coverage")
    check(len(SPECS_BY_TYPE) >= 20, f"{len(SPECS_BY_TYPE)} declaration types have parsers")


def main() -> int:
    verify_units()
    verify_money()
    verify_dates()
    verify_pipeline()
    print()
    if failures:
        print(f"{len(failures)} of {checks} checks FAILED")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"all {checks} extraction checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
