"""Unit tests for value parsing and normalisation.

These cover the arithmetic a legal finding rests on, so they assert exact Decimal
values rather than approximate comparisons.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.enums import QuantityKind
from app.services.extraction import dates, money, units
from app.services.extraction.dates import Precision


class TestQuantityParsing:
    @pytest.mark.parametrize(
        ("text", "amount", "unit", "base"),
        [
            ("Net Quantity: 500 g", "500", "g", "500"),
            ("Net Wt. 1.5 kg", "1.5", "kg", "1500.0"),
            ("NET QTY 250ml", "250", "ml", "250"),
            ("Contents: 2 L", "2", "l", "2000"),
            ("Net quantity 100 gm", "100", "g", "100"),
            ("Net quantity 250 mg", "250", "mg", "0.250"),
            ("Length 1.2 m", "1.2", "m", "1.2"),
            ("Net Quantity 20 Nos", "20", "unit", "20"),
        ],
    )
    def test_parses_and_converts_exactly(self, text, amount, unit, base):
        parsed = units.parse_quantity(text)
        assert parsed is not None
        assert parsed.amount == Decimal(amount)
        assert parsed.unit == unit
        assert parsed.base_amount == Decimal(base)

    def test_devanagari_digits_and_hindi_unit(self):
        parsed = units.parse_quantity("शुद्ध मात्रा ५०० ग्राम")
        assert parsed is not None
        assert parsed.amount == Decimal("500")
        assert parsed.unit == "g"

    def test_multipiece_totals_the_package(self):
        parsed = units.parse_quantity("12 x 50 g")
        assert parsed is not None
        assert parsed.pieces == 12
        assert parsed.per_piece_amount == Decimal("50")
        # The rule tests the total, not the per-piece amount.
        assert parsed.amount == Decimal("600")

    def test_weight_and_volume_never_convert_between_each_other(self):
        weight = units.parse_quantity("500 g")
        volume = units.parse_quantity("500 ml")
        assert weight and volume
        assert weight.dimension is QuantityKind.WEIGHT
        assert volume.dimension is QuantityKind.VOLUME
        assert units.to_unit(weight, "ml") is None

    def test_unit_aliases_canonicalise(self):
        for alias in ("gm", "gms", "gram", "grams", "g"):
            assert units.normalise_unit(alias) == "g"

    @pytest.mark.parametrize("text", ["Batch 5A", "", "Lot ABC", "FSSAI 10012345678901"])
    def test_rejects_non_quantities(self, text):
        assert units.parse_quantity(text) is None

    def test_unknown_unit_rejected(self):
        assert units.normalise_unit("furlong") is None


class TestMoneyParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("MRP ₹45.00", "45.00"),
            ("M.R.P. Rs. 120", "120"),
            ("Maximum Retail Price INR 1,250.50", "1250.50"),
            ("Rs 1,50,000", "150000"),
            ("99.99 Rs", "99.99"),
            ("अधिकतम खुदरा मूल्य रु. 60.50", "60.50"),
        ],
    )
    def test_parses_printed_prices(self, text, expected):
        parsed = money.parse_money(text)
        assert parsed is not None
        assert parsed.amount == Decimal(expected)

    @pytest.mark.parametrize("text", ["Batch 4570", "Net 500 g", "Lot 2026"])
    def test_bare_numbers_are_not_prices(self, text):
        assert money.parse_money(text) is None

    def test_decimal_addition_is_exact(self):
        first = money.parse_money("Rs 0.10")
        second = money.parse_money("Rs 0.20")
        assert first and second
        assert first.amount + second.amount == Decimal("0.30")

    def test_unit_sale_price_is_exact_and_shows_its_working(self):
        exact, rounded, steps = money.unit_sale_price(
            Decimal("45.00"), Decimal("250"), per_units=Decimal("100")
        )
        assert exact == Decimal("18.000")
        assert rounded == Decimal("18.00")
        assert len(steps) == 5
        assert any("45.00" in step for step in steps)

    @pytest.mark.parametrize(
        ("price", "quantity", "expected"),
        [
            ("10.00", "3", "3.33"),
            ("1.005", "1", "1.01"),
            ("2.005", "1", "2.01"),
            ("0.125", "1", "0.13"),
        ],
    )
    def test_rounding_is_half_up_to_paise(self, price, quantity, expected):
        _exact, rounded, _steps = money.unit_sale_price(
            Decimal(price), Decimal(quantity), per_units=Decimal("1")
        )
        assert rounded == Decimal(expected)

    def test_one_paisa_tolerance(self):
        assert money.amounts_match(Decimal("18.00"), Decimal("18.01"))
        assert not money.amounts_match(Decimal("18.00"), Decimal("18.05"))

    def test_implausible_price_flagged_not_rejected(self):
        parsed = money.parse_money("MRP Rs 0.01")
        assert parsed is not None
        assert parsed.plausible is False


class TestDateParsing:
    def test_month_year_keeps_month_precision(self):
        marking = dates.parse_date_marking("Mfg Date: 03/2026")
        assert marking is not None
        assert (marking.year, marking.month, marking.day) == (2026, 3, None)
        assert marking.precision is Precision.MONTH

    def test_full_date_keeps_day_precision(self):
        marking = dates.parse_date_marking("Packed on 12 Mar 2026")
        assert marking is not None
        assert marking.precision is Precision.DAY
        assert marking.day == 12

    def test_ambiguous_numeric_date_reports_both_readings(self):
        marking = dates.parse_date_marking("MFG 05/03/2026")
        assert marking is not None
        assert marking.is_ambiguous
        assert set(marking.alternatives) == {date(2026, 3, 5), date(2026, 5, 3)}

    def test_unambiguous_when_first_number_cannot_be_a_month(self):
        marking = dates.parse_date_marking("MFG 25/12/2026")
        assert marking is not None
        assert not marking.is_ambiguous
        assert marking.day == 25

    def test_hindi_month_name(self):
        marking = dates.parse_date_marking("निर्माण तिथि मार्च 2026")
        assert marking is not None
        assert (marking.year, marking.month) == (2026, 3)

    def test_two_digit_year_expands_to_this_century(self):
        marking = dates.parse_date_marking("Mfg 03/24")
        assert marking is not None
        assert marking.year == 2024

    def test_batch_code_is_not_a_date(self):
        assert dates.parse_date_marking("Batch AB12") is None

    def test_months_between(self):
        first = dates.parse_date_marking("01/2026")
        second = dates.parse_date_marking("07/2026")
        assert first and second
        assert dates.months_between(first, second) == 6
