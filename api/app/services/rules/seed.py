"""Starter rule set.

**Read this before relying on any citation below.**

These definitions encode the *mechanics* of checks that the Legal Metrology
(Packaged Commodities) Rules, 2011 require: that certain declarations appear, that a
date marking states month and year, that a unit sale price is arithmetically
consistent with the declared price and quantity, and so on. The mechanics are
implemented and tested.

The *citations* are recorded as supplied text, not as verified references. At the time
this seed set was written the primary rule text could not be retrieved from an
authoritative source programmatically, so every entry is created with:

* ``legal_authority_confirmed = False``;
* an ``uncertainty_note`` stating exactly what needs checking;
* ``status = draft``, so nothing here can influence an inspection until a rule
  administrator has reviewed it, run the simulator, and approved it.

A qualified authority must verify each citation and its text against the gazette
notification in force on the inspection date before these rules are approved. The
interface shows the unconfirmed status on every finding that cites one of them, and
``docs/LEGAL_SOURCES.md`` records the same caveat.

Numbers that appear as thresholds (for example a minimum character height) are
placeholders drawn from the definition text and are flagged in the same way. They
must be confirmed, and adjusted if wrong, before approval.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.enums import DeclarationType, QuantityKind, RuleStatus
from ...models.rule import RuleVersion
from ...observability import get_logger
from .simulator import ScenarioKind

logger = get_logger(__name__)

OFFICIAL_SOURCE = "https://consumeraffairs.gov.in/pages/legal-metrology-act"
LM_EBOOK_SOURCE = "https://doca.gov.in/lm-ebook/"

#: The commencement date of the 2011 rules as commonly stated. Recorded as the
#: default effective date and flagged for confirmation like every other value here.
DEFAULT_EFFECTIVE_FROM = date(2011, 4, 1)

VERIFICATION_REQUIRED = (
    "The citation, the quoted requirement and the effective date in this rule version "
    "have NOT been verified against the gazette notification. A qualified authority "
    "must confirm them, and correct them where wrong, before this version is approved. "
    "Until then this version must not influence any inspection."
)


@dataclass
class SeedScenario:
    """A simulator case shipped with a seed rule."""

    name: str
    kind: str
    expected_outcome: str
    values: dict[str, Any] = field(default_factory=dict)
    label_seen_without_value: dict[str, bool] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    evidence_complete: bool = True
    missing_faces: list[str] = field(default_factory=list)
    quantity_kind: str | None = None
    quantity_base: str | None = None
    is_imported: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "expected_outcome": self.expected_outcome,
            "values": self.values,
            "label_seen_without_value": self.label_seen_without_value,
            "context": self.context,
            "evidence_complete": self.evidence_complete,
            "missing_faces": self.missing_faces,
            "quantity_kind": self.quantity_kind,
            "quantity_base": self.quantity_base,
            "is_imported": self.is_imported,
        }


@dataclass
class SeedRule:
    code: str
    title: str
    citation: str
    test_kind: str
    test_specification: dict[str, Any]
    plain_explanation: str
    interpretation_note: str
    required_inputs: list[str] = field(default_factory=list)
    commodity_scope: list[str] = field(default_factory=list)
    package_scope: list[str] = field(default_factory=list)
    quantity_kind: str | None = None
    quantity_min_base: Decimal | None = None
    quantity_max_base: Decimal | None = None
    applies_to_imported: bool | None = None
    applies_to_ecommerce: bool | None = None
    applies_to_multipiece: bool | None = None
    exceptions: list[dict[str, str]] = field(default_factory=list)
    scenarios: list[SeedScenario] = field(default_factory=list)
    source_url: str = OFFICIAL_SOURCE
    effective_from: date = DEFAULT_EFFECTIVE_FROM


def _money(amount: str) -> dict[str, Any]:
    return {"kind": "money", "amount": amount, "currency": "INR", "display": f"₹{amount}"}


def _quantity(amount: str, unit: str, base: str, dimension: str = "weight") -> dict[str, Any]:
    return {
        "kind": "quantity",
        "amount": amount,
        "unit": unit,
        "dimension": dimension,
        "base_amount": base,
        "base_unit": "g" if dimension == "weight" else "ml",
        "display": f"{amount} {unit}",
    }


def _text(value: str) -> dict[str, Any]:
    return {"kind": "text", "value": value, "display": value}


def _date(year: int, month: int, precision: str = "month") -> dict[str, Any]:
    return {
        "kind": "date",
        "year": year,
        "month": month,
        "day": None,
        "precision": precision,
        "display": f"{month:02d}/{year}",
        "ambiguous": False,
        "alternatives": [],
        "source_text": f"{month:02d}/{year}",
    }


def _presence_scenarios(
    declaration: str, label: str, present_value: dict[str, Any]
) -> list[SeedScenario]:
    """The four mandatory cases for a presence check."""
    return [
        SeedScenario(
            name=f"{label} is declared",
            kind=ScenarioKind.COMPLIANT,
            expected_outcome="compliant",
            values={declaration: present_value},
        ),
        SeedScenario(
            name=f"{label} is absent and every face was captured",
            kind=ScenarioKind.NON_COMPLIANT,
            expected_outcome="non_compliant",
            values={},
        ),
        SeedScenario(
            name=f"{label} is absent but the package was not fully photographed",
            kind=ScenarioKind.MISSING_EVIDENCE,
            expected_outcome="additional_evidence_required",
            values={},
            evidence_complete=False,
            missing_faces=["back_panel"],
        ),
    ]


SEED_RULES: tuple[SeedRule, ...] = (
    SeedRule(
        code="LMPC-DECL-RESPONSIBLE-PARTY",
        title="Name and address of the manufacturer, packer or importer",
        citation="Rule 6(1)(a), Legal Metrology (Packaged Commodities) Rules, 2011",
        test_kind="any_declaration_present",
        test_specification={
            "declarations": [
                DeclarationType.MANUFACTURER.value,
                DeclarationType.PACKER.value,
                DeclarationType.IMPORTER.value,
            ],
            "label": "Name and address of the manufacturer, packer or importer",
        },
        plain_explanation=(
            "Every retail package must identify who is responsible for it: the "
            "manufacturer, the packer, or the importer where the goods are imported."
        ),
        interpretation_note=(
            "Read as satisfied by any one of manufacturer, packer or importer being "
            "declared, since the rule lists them as alternatives depending on who "
            "placed the package on the market. The check does not attempt to judge "
            "whether the address is complete or genuine; that remains an officer "
            "judgement recorded against the finding."
        ),
        scenarios=[
            SeedScenario(
                name="Manufacturer declared",
                kind=ScenarioKind.COMPLIANT,
                expected_outcome="compliant",
                values={DeclarationType.MANUFACTURER.value: _text("Example Foods Private Limited")},
            ),
            SeedScenario(
                name="Packer declared instead of manufacturer",
                kind=ScenarioKind.COMPLIANT,
                expected_outcome="compliant",
                values={DeclarationType.PACKER.value: _text("Example Packers, Sample City")},
            ),
            SeedScenario(
                name="None of the three declared, all faces captured",
                kind=ScenarioKind.NON_COMPLIANT,
                expected_outcome="non_compliant",
            ),
            SeedScenario(
                name="None declared but the package was not fully photographed",
                kind=ScenarioKind.MISSING_EVIDENCE,
                expected_outcome="additional_evidence_required",
                evidence_complete=False,
                missing_faces=["back_panel"],
            ),
        ],
    ),
    SeedRule(
        code="LMPC-DECL-COMMON-NAME",
        title="Common or generic name of the commodity",
        citation="Rule 6(1)(b), Legal Metrology (Packaged Commodities) Rules, 2011",
        test_kind="declaration_present",
        test_specification={
            "declaration": DeclarationType.COMMON_GENERIC_NAME.value,
            "label": "Common or generic name of the commodity",
        },
        plain_explanation=(
            "The package must state what the commodity actually is, in ordinary "
            "language, and not only its brand name."
        ),
        interpretation_note=(
            "Tested as the presence of a common or generic name distinct from the "
            "brand. The check cannot judge whether the wording chosen is the accepted "
            "generic name for the commodity; an officer decides that."
        ),
        scenarios=_presence_scenarios(
            DeclarationType.COMMON_GENERIC_NAME.value,
            "Common or generic name",
            _text("Whole wheat flour"),
        ),
    ),
    SeedRule(
        code="LMPC-DECL-NET-QUANTITY",
        title="Net quantity declaration",
        citation="Rule 6(1)(c), Legal Metrology (Packaged Commodities) Rules, 2011",
        test_kind="declaration_present",
        test_specification={
            "declaration": DeclarationType.NET_QUANTITY.value,
            "label": "Net quantity",
        },
        plain_explanation=(
            "The package must declare the net quantity of the commodity it contains, "
            "by weight, measure or number as appropriate."
        ),
        interpretation_note=(
            "Presence only. Whether the declared quantity matches the actual contents "
            "requires physical weighing and is outside what label analysis can "
            "establish; the finding says so rather than implying otherwise."
        ),
        scenarios=_presence_scenarios(
            DeclarationType.NET_QUANTITY.value,
            "Net quantity",
            _quantity("1", "kg", "1000"),
        ),
    ),
    SeedRule(
        code="LMPC-DECL-NET-QUANTITY-UNITS",
        title="Net quantity must use a permitted unit",
        citation=(
            "Rule 8 read with the Second Schedule, "
            "Legal Metrology (Packaged Commodities) Rules, 2011"
        ),
        test_kind="net_quantity_unit_permitted",
        test_specification={
            "permitted_units": {
                QuantityKind.WEIGHT.value: ["g", "kg"],
                QuantityKind.VOLUME.value: ["ml", "l"],
                QuantityKind.LENGTH.value: ["cm", "m"],
                QuantityKind.COUNT.value: ["unit", "pcs"],
            }
        },
        required_inputs=[DeclarationType.NET_QUANTITY.value],
        plain_explanation=(
            "The net quantity must be declared in the units prescribed for that kind of "
            "commodity, so that consumers can compare packages directly."
        ),
        interpretation_note=(
            "The permitted-unit lists here are a starting set covering the units seen in "
            "ordinary retail practice. The prescribed units and the quantity ranges they "
            "apply to must be confirmed against the Second Schedule before approval, and "
            "the lists corrected where they differ."
        ),
        scenarios=[
            SeedScenario(
                name="Declared in grams",
                kind=ScenarioKind.COMPLIANT,
                expected_outcome="compliant",
                values={DeclarationType.NET_QUANTITY.value: _quantity("500", "g", "500")},
            ),
            SeedScenario(
                name="Declared in a non-permitted unit",
                kind=ScenarioKind.NON_COMPLIANT,
                expected_outcome="non_compliant",
                values={DeclarationType.NET_QUANTITY.value: _quantity("2", "tonne", "2000000")},
            ),
            SeedScenario(
                name="No net quantity read from the evidence",
                kind=ScenarioKind.MISSING_EVIDENCE,
                expected_outcome="unable_to_determine",
                values={},
            ),
        ],
    ),
    SeedRule(
        code="LMPC-DECL-DATE-MARKING",
        title="Month and year of manufacture, packing or import",
        citation="Rule 6(1)(d), Legal Metrology (Packaged Commodities) Rules, 2011",
        test_kind="date_marking_completeness",
        test_specification={
            "accepted_declarations": [
                DeclarationType.DATE_OF_MANUFACTURE.value,
                DeclarationType.DATE_OF_PACKING.value,
                DeclarationType.DATE_OF_IMPORT.value,
            ],
            "required_precision": "month",
        },
        plain_explanation=(
            "The package must state at least the month and the year in which the "
            "commodity was manufactured, pre-packed or imported."
        ),
        interpretation_note=(
            "Read as requiring month and year at minimum, so a marking that gives only a "
            "year is treated as incomplete and one that gives a full date satisfies the "
            "requirement. Which of manufacture, packing or import applies depends on the "
            "package; any one of the three is accepted."
        ),
        scenarios=[
            SeedScenario(
                name="Month and year declared",
                kind=ScenarioKind.COMPLIANT,
                expected_outcome="compliant",
                values={DeclarationType.DATE_OF_MANUFACTURE.value: _date(2026, 3)},
            ),
            SeedScenario(
                name="Only a year declared",
                kind=ScenarioKind.NON_COMPLIANT,
                expected_outcome="non_compliant",
                values={
                    DeclarationType.DATE_OF_PACKING.value: {
                        **_date(2026, 1),
                        "month": None,
                        "precision": "year",
                        "display": "2026",
                    }
                },
            ),
            SeedScenario(
                name="No date marking found, all faces captured",
                kind=ScenarioKind.NON_COMPLIANT,
                expected_outcome="non_compliant",
                values={},
            ),
            SeedScenario(
                name="No date marking found, package not fully photographed",
                kind=ScenarioKind.MISSING_EVIDENCE,
                expected_outcome="additional_evidence_required",
                values={},
                evidence_complete=False,
                missing_faces=["back_panel"],
            ),
        ],
    ),
    SeedRule(
        code="LMPC-DECL-RETAIL-SALE-PRICE",
        title="Retail sale price (maximum retail price)",
        citation="Rule 6(1)(e), Legal Metrology (Packaged Commodities) Rules, 2011",
        test_kind="declaration_present",
        test_specification={
            "declaration": DeclarationType.MRP.value,
            "label": "Retail sale price",
        },
        plain_explanation=(
            "The package must declare the retail sale price, the maximum price at which "
            "the commodity may be sold to a consumer."
        ),
        interpretation_note=(
            "Presence of a declared retail sale price. Whether the price charged at the "
            "counter exceeds it is a separate matter established from a bill, not from "
            "the label, and is handled through the complaint workflow."
        ),
        scenarios=_presence_scenarios(
            DeclarationType.MRP.value, "Retail sale price", _money("58.00")
        ),
    ),
    SeedRule(
        code="LMPC-DECL-RETAIL-PRICE-WORDING",
        title="Retail sale price must be expressed in the prescribed manner",
        citation=(
            "Rule 6(1)(e) read with Rule 2(l), "
            "Legal Metrology (Packaged Commodities) Rules, 2011"
        ),
        test_kind="retail_price_wording",
        test_specification={
            "required_phrases": [
                "inclusive of all taxes",
                "incl. of all taxes",
                "incl of all taxes",
                "maximum retail price",
                "mrp",
            ]
        },
        required_inputs=[DeclarationType.MRP.value],
        plain_explanation=(
            "The retail sale price must be printed in the prescribed form, so that a "
            "consumer can see that the figure shown is the maximum price inclusive of "
            "taxes."
        ),
        interpretation_note=(
            "Tested by looking for the prescribed qualifying wording in the text printed "
            "around the price. The accepted phrase list must be confirmed against the "
            "rule before approval; wording that is legally acceptable but absent from the "
            "list would otherwise produce a wrong finding."
        ),
        scenarios=[
            SeedScenario(
                name="Price carries the qualifying wording",
                kind=ScenarioKind.COMPLIANT,
                expected_outcome="compliant",
                values={DeclarationType.MRP.value: _money("58.00")},
                context={"mrp_context_text": "M.R.P. Rs. 58.00 (incl. of all taxes)"},
            ),
            SeedScenario(
                name="Price printed with no qualifying wording",
                kind=ScenarioKind.NON_COMPLIANT,
                expected_outcome="non_compliant",
                values={DeclarationType.MRP.value: _money("58.00")},
                context={"mrp_context_text": "Price 58.00"},
            ),
            SeedScenario(
                name="Surrounding text was not captured",
                kind=ScenarioKind.MISSING_EVIDENCE,
                expected_outcome="unable_to_determine",
                values={DeclarationType.MRP.value: _money("58.00")},
                context={},
            ),
        ],
    ),
    SeedRule(
        code="LMPC-DECL-CONSUMER-CARE",
        title="Consumer care details",
        citation="Rule 6(1)(f), Legal Metrology (Packaged Commodities) Rules, 2011",
        test_kind="consumer_care_completeness",
        test_specification={
            "require_any_of": [
                DeclarationType.CONSUMER_CARE_EMAIL.value,
                DeclarationType.CONSUMER_CARE_PHONE.value,
                DeclarationType.CONSUMER_CARE_NAME.value,
            ]
        },
        plain_explanation=(
            "The package must give consumer care details so that a consumer with a "
            "complaint has a way to make contact."
        ),
        interpretation_note=(
            "Read as satisfied by any usable contact route: a named person or office, a "
            "telephone number, or an email address. Whether the rule requires a specific "
            "combination of these must be confirmed before approval."
        ),
        scenarios=[
            SeedScenario(
                name="Email and telephone declared",
                kind=ScenarioKind.COMPLIANT,
                expected_outcome="compliant",
                values={
                    DeclarationType.CONSUMER_CARE_EMAIL.value: {
                        "kind": "email",
                        "value": "care@example.org",
                        "display": "care@example.org",
                    }
                },
            ),
            SeedScenario(
                name="No contact route declared, all faces captured",
                kind=ScenarioKind.NON_COMPLIANT,
                expected_outcome="non_compliant",
            ),
            SeedScenario(
                name="No contact route declared, package not fully photographed",
                kind=ScenarioKind.MISSING_EVIDENCE,
                expected_outcome="additional_evidence_required",
                evidence_complete=False,
                missing_faces=["back_panel"],
            ),
        ],
    ),
    SeedRule(
        code="LMPC-DECL-COUNTRY-OF-ORIGIN-IMPORTED",
        title="Country of origin on imported packages",
        citation="Rule 6(1), Legal Metrology (Packaged Commodities) Rules, 2011, as amended",
        test_kind="country_of_origin_required",
        test_specification={},
        applies_to_imported=True,
        plain_explanation=(
            "An imported package must declare the country of origin of the commodity."
        ),
        interpretation_note=(
            "Applied only where the inspection records the package as imported. Import "
            "status is taken from the officer's record and is never inferred from a "
            "barcode prefix, because a GS1 prefix identifies the issuing organisation "
            "rather than the place of manufacture. The amendment reference and its "
            "commencement date must be confirmed before approval."
        ),
        scenarios=[
            SeedScenario(
                name="Imported package declares country of origin",
                kind=ScenarioKind.COMPLIANT,
                expected_outcome="compliant",
                values={DeclarationType.COUNTRY_OF_ORIGIN.value: _text("Nepal")},
                context={"is_imported": True},
                is_imported=True,
            ),
            SeedScenario(
                name="Imported package with no country of origin",
                kind=ScenarioKind.NON_COMPLIANT,
                expected_outcome="non_compliant",
                context={"is_imported": True},
                is_imported=True,
            ),
            SeedScenario(
                name="Imported package, not fully photographed",
                kind=ScenarioKind.MISSING_EVIDENCE,
                expected_outcome="additional_evidence_required",
                context={"is_imported": True},
                is_imported=True,
                evidence_complete=False,
                missing_faces=["importer_label"],
            ),
        ],
    ),
    SeedRule(
        code="LMPC-UNIT-SALE-PRICE-CONSISTENCY",
        title="Unit sale price consistent with price and net quantity",
        citation="Rule 6 read with Rule 2, Legal Metrology (Packaged Commodities) Rules, 2011",
        test_kind="unit_sale_price_consistency",
        test_specification={"per_units": 100, "tolerance_paise": 1, "skip_for_count": True},
        required_inputs=[DeclarationType.MRP.value, DeclarationType.NET_QUANTITY.value],
        plain_explanation=(
            "Where a unit sale price is printed, it must agree with the declared retail "
            "sale price divided by the declared net quantity."
        ),
        interpretation_note=(
            "Arithmetic is exact and performed in Decimal; the comparison allows one "
            "paisa to absorb the packer's own rounding. Whether a unit sale price is "
            "required on a given package, and the reference quantity it must be "
            "expressed against, must be confirmed before approval. The tolerance is an "
            "engineering choice and is stated on every finding."
        ),
        scenarios=[
            SeedScenario(
                name="Printed unit price matches the calculation",
                kind=ScenarioKind.COMPLIANT,
                expected_outcome="compliant",
                values={
                    DeclarationType.MRP.value: _money("45.00"),
                    DeclarationType.NET_QUANTITY.value: _quantity("250", "g", "250"),
                    DeclarationType.UNIT_SALE_PRICE.value: _money("18.00"),
                },
            ),
            SeedScenario(
                name="Printed unit price does not match",
                kind=ScenarioKind.NON_COMPLIANT,
                expected_outcome="non_compliant",
                values={
                    DeclarationType.MRP.value: _money("45.00"),
                    DeclarationType.NET_QUANTITY.value: _quantity("250", "g", "250"),
                    DeclarationType.UNIT_SALE_PRICE.value: _money("15.00"),
                },
            ),
            SeedScenario(
                name="Net quantity not established",
                kind=ScenarioKind.MISSING_EVIDENCE,
                expected_outcome="unable_to_determine",
                values={DeclarationType.MRP.value: _money("45.00")},
            ),
        ],
    ),
    SeedRule(
        code="LMPC-CHAR-HEIGHT-NET-QUANTITY",
        title="Minimum character height for the net quantity declaration",
        citation="Rule 9, Legal Metrology (Packaged Commodities) Rules, 2011",
        test_kind="character_height_minimum",
        test_specification={
            "declaration": DeclarationType.NET_QUANTITY.value,
            "minimum_height_mm": "1.0",
            "uncertainty_mm": "0.2",
        },
        plain_explanation=(
            "Mandatory declarations must be printed at or above a minimum character "
            "height so that they are legible to a consumer."
        ),
        interpretation_note=(
            "THE THRESHOLD IN THIS VERSION IS A PLACEHOLDER. The prescribed minimum "
            "height varies with the area of the principal display panel and with the "
            "declaration concerned, and the applicable table must be transcribed from "
            "Rule 9 and entered here before approval. The check itself refuses to derive "
            "a height from pixels and requires an officer measurement in millimetres, so "
            "an unconfirmed threshold cannot silently produce a violation: without a "
            "measurement the outcome is 'additional evidence required'."
        ),
        scenarios=[
            SeedScenario(
                name="Measured height clears the minimum",
                kind=ScenarioKind.COMPLIANT,
                expected_outcome="compliant",
                context={
                    "character_height_measurements": {
                        DeclarationType.NET_QUANTITY.value: {
                            "observed_mm": "1.6",
                            "uncertainty_mm": "0.2",
                            "method": "steel rule against the package",
                        }
                    }
                },
            ),
            SeedScenario(
                name="Measured height is below the minimum",
                kind=ScenarioKind.NON_COMPLIANT,
                expected_outcome="non_compliant",
                context={
                    "character_height_measurements": {
                        DeclarationType.NET_QUANTITY.value: {
                            "observed_mm": "0.5",
                            "uncertainty_mm": "0.2",
                            "method": "steel rule against the package",
                        }
                    }
                },
            ),
            SeedScenario(
                name="No measurement recorded",
                kind=ScenarioKind.MISSING_EVIDENCE,
                expected_outcome="additional_evidence_required",
                context={},
            ),
        ],
    ),
)


async def seed_rules(db: AsyncSession, *, author_id: uuid.UUID | None = None) -> dict[str, Any]:
    """Insert the starter rule set as drafts. Existing codes are left untouched.

    Everything is created with ``status = draft`` and
    ``legal_authority_confirmed = False``, so no seeded interpretation can affect an
    inspection until a rule administrator has reviewed, simulated and approved it.
    """
    created: list[str] = []
    skipped: list[str] = []

    for definition in SEED_RULES:
        existing = await db.scalar(
            select(RuleVersion).where(RuleVersion.code == definition.code, RuleVersion.version == 1)
        )
        if existing is not None:
            skipped.append(definition.code)
            continue

        db.add(
            RuleVersion(
                id=uuid.uuid4(),
                code=definition.code,
                version=1,
                title=definition.title,
                citation=definition.citation,
                source_url=definition.source_url,
                source_document_title=(
                    "Legal Metrology (Packaged Commodities) Rules, 2011 "
                    "(citation not yet verified against the gazette)"
                ),
                gazette_reference=None,
                source_retrieved_on=None,
                effective_from=definition.effective_from,
                effective_to=None,
                commodity_scope=definition.commodity_scope,
                package_scope=definition.package_scope,
                quantity_kind=definition.quantity_kind,
                quantity_min_base=definition.quantity_min_base,
                quantity_max_base=definition.quantity_max_base,
                applies_to_imported=definition.applies_to_imported,
                applies_to_ecommerce=definition.applies_to_ecommerce,
                applies_to_multipiece=definition.applies_to_multipiece,
                exceptions=definition.exceptions,
                transition_conditions={},
                required_inputs=definition.required_inputs,
                test_kind=definition.test_kind,
                test_specification=definition.test_specification,
                plain_explanation=definition.plain_explanation,
                interpretation_note=definition.interpretation_note,
                uncertainty_note=VERIFICATION_REQUIRED,
                status=RuleStatus.DRAFT,
                author_id=author_id,
                legal_authority_confirmed=False,
                legal_authority_note=(
                    "Not confirmed. This interpretation has not been endorsed by any "
                    "statutory authority."
                ),
                test_results={
                    "seeded_scenarios": [item.as_dict() for item in definition.scenarios]
                },
            )
        )
        created.append(definition.code)

    await db.flush()
    logger.info("rules_seeded", created=len(created), skipped=len(skipped))
    return {
        "created": created,
        "skipped": skipped,
        "status": RuleStatus.DRAFT.value,
        "note": (
            "Seeded as drafts. Each version must be reviewed, simulated and approved by "
            "a rule administrator before it can affect an inspection, and its citation "
            "must be verified against the gazette by a qualified authority."
        ),
    }


def scenarios_for(code: str) -> list[dict[str, Any]]:
    """The shipped simulator scenarios for a seed rule code."""
    for definition in SEED_RULES:
        if definition.code == code:
            return [item.as_dict() for item in definition.scenarios]
    return []
