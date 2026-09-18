"""Verify the rule engine: checks, selection, boundaries and the simulator.

Runs in memory against unsaved RuleVersion objects, so no database is needed.

    docker compose run --rm --no-deps --entrypoint python api scripts/verify_rules.py
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal

from app.domain.enums import LegalOutcome, QuantityKind, RuleStatus
from app.models.rule import RuleVersion
from app.services.rules import checks, selection, simulator

failures: list[str] = []
count = 0


def check(condition: bool, description: str) -> None:
    global count
    count += 1
    if condition:
        print(f"  ok    {description}")
    else:
        failures.append(description)
        print(f"  FAIL  {description}")


def money(amount: str) -> dict[str, object]:
    return {"kind": "money", "amount": amount, "currency": "INR", "display": f"₹{amount}"}


def quantity(amount: str, unit: str, base: str, dimension: str = "weight") -> dict[str, object]:
    return {
        "kind": "quantity",
        "amount": amount,
        "unit": unit,
        "dimension": dimension,
        "base_amount": base,
        "base_unit": "g" if dimension == "weight" else "ml",
        "display": f"{amount} {unit}",
    }


def make_rule(**overrides: object) -> RuleVersion:
    defaults: dict[str, object] = {
        "code": "TEST-RULE",
        "version": 1,
        "title": "Test rule",
        "citation": "Test citation, requires verification",
        "source_url": "https://consumeraffairs.gov.in/pages/legal-metrology-act",
        "effective_from": date(2011, 4, 1),
        "effective_to": None,
        "status": RuleStatus.ACTIVE.value,
        "test_kind": "declaration_present",
        "test_specification": {"declaration": "mrp", "label": "Retail sale price"},
        "required_inputs": [],
        "plain_explanation": "A test rule.",
        "interpretation_note": "For verification only.",
        "commodity_scope": [],
        "package_scope": [],
        "exceptions": [],
        "legal_authority_confirmed": False,
    }
    defaults.update(overrides)
    return RuleVersion(**defaults)  # type: ignore[arg-type]


def verify_presence_check() -> None:
    print("declaration presence: absent evidence is never a violation")
    parameters = {"declaration": "mrp", "label": "Retail sale price"}

    result = checks.run_check(
        "declaration_present",
        checks.CheckInput(
            values={"mrp": money("58.00")}, parameters=parameters, evidence_complete=True
        ),
    )
    check(
        result.outcome is LegalOutcome.COMPLIANT,
        f"present declaration is compliant ({result.outcome})",
    )

    result = checks.run_check(
        "declaration_present",
        checks.CheckInput(
            values={}, parameters=parameters, evidence_complete=False, missing_faces=["back_panel"]
        ),
    )
    check(
        result.outcome is LegalOutcome.ADDITIONAL_EVIDENCE_REQUIRED,
        f"missing with incomplete evidence asks for more evidence ({result.outcome})",
    )
    check("back_panel" in result.explanation, "the outstanding face is named in the message")

    result = checks.run_check(
        "declaration_present",
        checks.CheckInput(values={}, parameters=parameters, evidence_complete=True),
    )
    check(
        result.outcome is LegalOutcome.NON_COMPLIANT,
        f"missing with complete evidence is non-compliant ({result.outcome})",
    )

    result = checks.run_check(
        "declaration_present",
        checks.CheckInput(
            values={},
            label_seen_without_value={"mrp": True},
            parameters=parameters,
            evidence_complete=True,
        ),
    )
    check(
        result.outcome is LegalOutcome.UNABLE_TO_DETERMINE,
        f"label present but value unreadable is undetermined ({result.outcome})",
    )

    print("alternatives")
    result = checks.run_check(
        "any_declaration_present",
        checks.CheckInput(
            values={"packer": {"kind": "text", "value": "Acme Packers", "display": "Acme Packers"}},
            parameters={
                "declarations": ["manufacturer", "packer", "importer"],
                "label": "Name and address of the manufacturer, packer or importer",
            },
            evidence_complete=True,
        ),
    )
    check(
        result.outcome is LegalOutcome.COMPLIANT,
        f"any-of satisfied by one alternative ({result.outcome})",
    )


def verify_unit_price_check() -> None:
    print("unit sale price consistency")
    parameters = {"per_units": 100, "tolerance_paise": 1}

    result = checks.run_check(
        "unit_sale_price_consistency",
        checks.CheckInput(
            values={
                "mrp": money("45.00"),
                "net_quantity": quantity("250", "g", "250"),
                "unit_sale_price": money("18.00"),
            },
            parameters=parameters,
            evidence_complete=True,
        ),
    )
    check(
        result.outcome is LegalOutcome.COMPLIANT,
        f"correct unit price is compliant ({result.outcome})",
    )
    check(
        any("18.00" in step for step in result.calculation), "calculation shows the derived value"
    )
    check(
        len(result.calculation) >= 5, f"every arithmetic step recorded ({len(result.calculation)})"
    )

    result = checks.run_check(
        "unit_sale_price_consistency",
        checks.CheckInput(
            values={
                "mrp": money("45.00"),
                "net_quantity": quantity("250", "g", "250"),
                "unit_sale_price": money("15.00"),
            },
            parameters=parameters,
            evidence_complete=True,
        ),
    )
    check(
        result.outcome is LegalOutcome.NON_COMPLIANT,
        f"wrong unit price is non-compliant ({result.outcome})",
    )
    check(result.expected_value is not None, f"expected value stated ({result.expected_value})")

    result = checks.run_check(
        "unit_sale_price_consistency",
        checks.CheckInput(
            values={
                "mrp": money("45.00"),
                "net_quantity": quantity("250", "g", "250"),
                "unit_sale_price": money("18.01"),
            },
            parameters=parameters,
            evidence_complete=True,
        ),
    )
    check(
        result.outcome is LegalOutcome.COMPLIANT, f"one paisa inside tolerance ({result.outcome})"
    )

    result = checks.run_check(
        "unit_sale_price_consistency",
        checks.CheckInput(
            values={"net_quantity": quantity("250", "g", "250")},
            parameters=parameters,
            evidence_complete=True,
        ),
    )
    check(
        result.outcome is LegalOutcome.UNABLE_TO_DETERMINE,
        f"missing price gives undetermined, not a violation ({result.outcome})",
    )

    result = checks.run_check(
        "unit_sale_price_consistency",
        checks.CheckInput(
            values={
                "mrp": money("100.00"),
                "net_quantity": quantity("20", "unit", "20", dimension="count"),
            },
            parameters=parameters,
            evidence_complete=True,
        ),
    )
    check(
        result.outcome is LegalOutcome.NOT_APPLICABLE,
        f"count-based package is out of scope for a per-weight unit price ({result.outcome})",
    )

    result = checks.run_check(
        "unit_sale_price_consistency",
        checks.CheckInput(
            values={"mrp": money("45.00"), "net_quantity": quantity("0", "g", "0")},
            parameters=parameters,
            evidence_complete=True,
        ),
    )
    check(
        result.outcome is LegalOutcome.UNABLE_TO_DETERMINE,
        f"zero quantity does not divide by zero ({result.outcome})",
    )


def verify_character_height_check() -> None:
    print("character height: no measurement means no conclusion")
    parameters = {
        "minimum_height_mm": "1.0",
        "uncertainty_mm": "0.2",
        "declaration": "net_quantity",
    }

    result = checks.run_check(
        "character_height_minimum",
        checks.CheckInput(values={}, parameters=parameters, context={}, evidence_complete=True),
    )
    check(
        result.outcome is LegalOutcome.ADDITIONAL_EVIDENCE_REQUIRED,
        f"height is never guessed from pixels ({result.outcome})",
    )
    check("millimetres" in result.explanation, "explains why a photograph alone is insufficient")

    def with_measurement(observed: str, uncertainty: str = "0.2") -> checks.CheckResult:
        return checks.run_check(
            "character_height_minimum",
            checks.CheckInput(
                values={},
                parameters=parameters,
                context={
                    "character_height_measurements": {
                        "net_quantity": {
                            "observed_mm": observed,
                            "uncertainty_mm": uncertainty,
                            "method": "steel rule",
                        }
                    }
                },
                evidence_complete=True,
            ),
        )

    check(
        with_measurement("1.5").outcome is LegalOutcome.COMPLIANT, "1.5 mm clears a 1.0 mm minimum"
    )
    check(
        with_measurement("0.6").outcome is LegalOutcome.NON_COMPLIANT,
        "0.6 mm fails a 1.0 mm minimum",
    )
    check(
        with_measurement("1.0").outcome is LegalOutcome.UNABLE_TO_DETERMINE,
        "exactly at the threshold with ±0.2 mm is undetermined, not decided",
    )
    check(
        with_measurement("1.25").outcome is LegalOutcome.COMPLIANT,
        "1.25 mm ± 0.2 clears the threshold at its lower bound",
    )
    check(
        with_measurement("0.79").outcome is LegalOutcome.NON_COMPLIANT,
        "0.79 mm ± 0.2 fails even at its upper bound",
    )


def verify_selection() -> None:
    print("rule selection: dates")
    rule = make_rule(effective_from=date(2022, 4, 1), effective_to=date(2026, 3, 31))

    def applies(day: date) -> bool:
        return selection.evaluate(rule, selection.SelectionContext(inspection_date=day)).applies

    check(not applies(date(2022, 3, 31)), "the day before effective_from does not apply")
    check(applies(date(2022, 4, 1)), "effective_from itself applies (inclusive)")
    check(applies(date(2026, 3, 31)), "effective_to itself applies (inclusive)")
    check(not applies(date(2026, 4, 1)), "the day after effective_to does not apply")

    print("rule selection: only approved or active versions")
    for status, expected in (
        (RuleStatus.DRAFT.value, False),
        (RuleStatus.UNDER_REVIEW.value, False),
        (RuleStatus.CHANGES_REQUIRED.value, False),
        (RuleStatus.APPROVED.value, True),
        (RuleStatus.ACTIVE.value, True),
        (RuleStatus.SUPERSEDED.value, False),
        (RuleStatus.WITHDRAWN.value, False),
    ):
        candidate = make_rule(status=status)
        result = selection.evaluate(
            candidate, selection.SelectionContext(inspection_date=date(2026, 1, 1))
        )
        check(result.applies is expected, f"status {status} applies={expected}")

    print("rule selection: quantity band boundaries")
    banded = make_rule(
        quantity_kind=QuantityKind.WEIGHT.value,
        quantity_min_base=Decimal("10"),
        quantity_max_base=Decimal("1000"),
    )

    def band_applies(value: str) -> bool:
        return selection.evaluate(
            banded,
            selection.SelectionContext(
                inspection_date=date(2026, 1, 1),
                quantity_kind=QuantityKind.WEIGHT.value,
                quantity_base=Decimal(value),
            ),
        ).applies

    check(not band_applies("9.999"), "just below the lower bound is excluded")
    check(band_applies("10"), "exactly the lower bound is included")
    check(band_applies("999.999"), "just below the upper bound is included")
    check(not band_applies("1000"), "exactly the upper bound is excluded")
    check(not band_applies("1000.001"), "above the upper bound is excluded")

    print("rule selection: adjacent bands do not overlap")
    lower_band = make_rule(
        code="BAND",
        version=1,
        quantity_kind=QuantityKind.WEIGHT.value,
        quantity_min_base=Decimal("0"),
        quantity_max_base=Decimal("1000"),
    )
    upper_band = make_rule(
        code="BAND2",
        version=1,
        quantity_kind=QuantityKind.WEIGHT.value,
        quantity_min_base=Decimal("1000"),
        quantity_max_base=Decimal("10000"),
    )
    context = selection.SelectionContext(
        inspection_date=date(2026, 1, 1),
        quantity_kind=QuantityKind.WEIGHT.value,
        quantity_base=Decimal("1000"),
    )
    selected, _ = selection.select([lower_band, upper_band], context)
    check(
        len(selected) == 1 and selected[0].rule_version.code == "BAND2",
        f"1000 g falls in exactly one band ({[item.rule_version.code for item in selected]})",
    )

    print("rule selection: scope")
    scoped = make_rule(commodity_scope=["food"], package_scope=["pouch"])
    result = selection.evaluate(
        scoped,
        selection.SelectionContext(
            inspection_date=date(2026, 1, 1), commodity_category="food", package_type="pouch"
        ),
    )
    check(result.applies, "matching commodity and package apply")
    result = selection.evaluate(
        scoped,
        selection.SelectionContext(
            inspection_date=date(2026, 1, 1), commodity_category="cement", package_type="pouch"
        ),
    )
    check(not result.applies, "non-matching commodity does not apply")
    check(
        result.blocking_reason is not None and "commodity" in result.blocking_reason.lower(),
        f"reason names the commodity mismatch ({result.blocking_reason})",
    )

    print("rule selection: import and exception")
    imported_rule = make_rule(applies_to_imported=True)
    check(
        selection.evaluate(
            imported_rule,
            selection.SelectionContext(inspection_date=date(2026, 1, 1), is_imported=True),
        ).applies,
        "imported-only rule applies to an imported package",
    )
    check(
        not selection.evaluate(
            imported_rule,
            selection.SelectionContext(inspection_date=date(2026, 1, 1), is_imported=False),
        ).applies,
        "imported-only rule does not apply to a domestic package",
    )

    excepted = make_rule(
        exceptions=[{"key": "below_10g", "description": "Packages of 10 g or less"}]
    )
    result = selection.evaluate(
        excepted,
        selection.SelectionContext(
            inspection_date=date(2026, 1, 1), claimed_exceptions=("below_10g",)
        ),
    )
    check(not result.applies, "a recorded exception removes the requirement")
    check(
        result.blocking_reason is not None and "10 g" in result.blocking_reason,
        f"reason quotes the exception ({result.blocking_reason})",
    )

    print("rule selection: two in-force versions of one code")
    old = make_rule(code="DUP", version=1, effective_from=date(2011, 4, 1))
    new = make_rule(code="DUP", version=2, effective_from=date(2024, 1, 1))
    selected, rejected = selection.select(
        [old, new], selection.SelectionContext(inspection_date=date(2026, 1, 1))
    )
    check(
        len(selected) == 1 and selected[0].rule_version.version == 2,
        f"the later effective date wins ({[item.rule_version.version for item in selected]})",
    )
    check(
        any("Superseded" in (item.blocking_reason or "") for item in rejected),
        "the losing version records why it was not used",
    )

    print("rule selection: inspection date, not today")
    historical = make_rule(effective_from=date(2011, 4, 1), effective_to=date(2015, 12, 31))
    check(
        selection.evaluate(
            historical, selection.SelectionContext(inspection_date=date(2014, 6, 1))
        ).applies,
        "a rule in force in 2014 applies to a 2014 inspection",
    )


def verify_simulator() -> None:
    print("simulator: coverage gates approval")
    rule = make_rule(
        quantity_kind=QuantityKind.WEIGHT.value,
        quantity_min_base=Decimal("10"),
        quantity_max_base=Decimal("1000"),
        effective_to=date(2030, 12, 31),
        exceptions=[{"key": "loose_sale", "description": "Sold loose"}],
    )
    required = simulator.required_kinds_for(rule)
    for kind in (
        simulator.ScenarioKind.COMPLIANT,
        simulator.ScenarioKind.NON_COMPLIANT,
        simulator.ScenarioKind.MISSING_EVIDENCE,
        simulator.ScenarioKind.NOT_YET_EFFECTIVE,
        simulator.ScenarioKind.BOUNDARY_EXACT,
        simulator.ScenarioKind.BOUNDARY_BELOW,
        simulator.ScenarioKind.BOUNDARY_ABOVE,
        simulator.ScenarioKind.EXPIRED,
        simulator.ScenarioKind.EXCEPTION_APPLIES,
    ):
        check(kind in required, f"{kind} is required for a banded rule with an end date")

    thin = simulator.simulate(
        rule,
        [
            simulator.Scenario(
                name="only a compliant case",
                kind=simulator.ScenarioKind.COMPLIANT,
                values={"mrp": money("58.00")},
                quantity_kind=QuantityKind.WEIGHT.value,
                quantity_base="500",
                expected_outcome=LegalOutcome.COMPLIANT.value,
            )
        ],
    )
    check(thin.all_passed, "the single scenario itself passes")
    check(not thin.approval_ready, "one scenario is not enough for approval")
    check(len(thin.missing_kinds) >= 7, f"missing kinds reported ({len(thin.missing_kinds)})")

    print("simulator: generated date scenarios")
    generated = simulator.default_scenarios(rule)
    report = simulator.simulate(rule, generated)
    check(
        report.all_passed,
        f"generated date scenarios all pass ({report.failed_count} failed)",
    )
    kinds = {item.kind for item in generated}
    check(
        simulator.ScenarioKind.NOT_YET_EFFECTIVE in kinds, "a not-yet-effective case is generated"
    )
    check(simulator.ScenarioKind.EXPIRED in kinds, "an expired case is generated")
    check(simulator.ScenarioKind.EXCEPTION_APPLIES in kinds, "an exception case is generated")

    print("simulator: generated boundary scenarios")
    template = simulator.Scenario(
        name="template",
        kind=simulator.ScenarioKind.COMPLIANT,
        values={"mrp": money("58.00")},
        quantity_kind=QuantityKind.WEIGHT.value,
        quantity_base="500",
        expected_outcome=LegalOutcome.COMPLIANT.value,
    )
    boundaries = simulator.boundary_scenarios(rule, template)
    boundary_report = simulator.simulate(rule, boundaries)
    check(len(boundaries) == 4, f"four boundary cases generated ({len(boundaries)})")
    check(
        boundary_report.all_passed,
        f"boundary cases behave as declared ({[item.name for item in boundary_report.results if not item.passed]})",
    )

    print("simulator: a failing scenario blocks approval")
    failing = simulator.simulate(
        rule,
        [
            simulator.Scenario(
                name="wrongly expects non-compliant",
                kind=simulator.ScenarioKind.NON_COMPLIANT,
                values={"mrp": money("58.00")},
                quantity_kind=QuantityKind.WEIGHT.value,
                quantity_base="500",
                expected_outcome=LegalOutcome.NON_COMPLIANT.value,
            )
        ],
    )
    check(not failing.all_passed, "a mismatch between expected and actual fails")
    check(not failing.approval_ready, "a failing scenario blocks approval")

    print("engine safety")
    unknown = checks.run_check("no_such_check", checks.CheckInput())
    check(
        unknown.outcome is LegalOutcome.UNABLE_TO_DETERMINE,
        f"an unimplemented test kind is undetermined, never non-compliant ({unknown.outcome})",
    )
    check(
        len(checks.available_checks()) >= 7, f"{len(checks.available_checks())} checks registered"
    )


def main() -> int:
    verify_presence_check()
    verify_unit_price_check()
    verify_character_height_check()
    verify_selection()
    verify_simulator()
    print()
    if failures:
        print(f"{len(failures)} of {count} checks FAILED")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"all {count} rule engine checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
