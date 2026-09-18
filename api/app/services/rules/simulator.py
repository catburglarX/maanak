"""Rule simulator.

A rule version cannot be approved until it has been run against a set of scenarios
covering the cases that matter: a compliant package, a non-compliant package, the
exact boundary of any quantity band, either side of that boundary, missing evidence,
and dates before, during and after the effective window.

The simulator runs entirely in memory against a rule version and a scenario
description. It touches no inspection and writes no finding, so an author can
iterate freely.

Coverage is enforced rather than suggested: ``approval_blockers`` returns the list of
mandatory scenario kinds that have not been exercised, and the approval endpoint
refuses while that list is non-empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from ...domain.enums import LegalOutcome
from ...models.rule import RuleVersion
from .checks import CheckInput, run_check
from .selection import SelectionContext, evaluate


class ScenarioKind:
    COMPLIANT = "compliant"
    NON_COMPLIANT = "non_compliant"
    BOUNDARY_EXACT = "boundary_exact"
    BOUNDARY_BELOW = "boundary_below"
    BOUNDARY_ABOVE = "boundary_above"
    MISSING_EVIDENCE = "missing_evidence"
    NOT_YET_EFFECTIVE = "not_yet_effective"
    EXPIRED = "expired"
    EXCEPTION_APPLIES = "exception_applies"


#: Every rule version must exercise these before approval.
MANDATORY_KINDS: tuple[str, ...] = (
    ScenarioKind.COMPLIANT,
    ScenarioKind.NON_COMPLIANT,
    ScenarioKind.MISSING_EVIDENCE,
    ScenarioKind.NOT_YET_EFFECTIVE,
)

#: Additionally required when the rule declares a quantity band.
BOUNDARY_KINDS: tuple[str, ...] = (
    ScenarioKind.BOUNDARY_EXACT,
    ScenarioKind.BOUNDARY_BELOW,
    ScenarioKind.BOUNDARY_ABOVE,
)


@dataclass
class Scenario:
    """One simulator case."""

    name: str
    kind: str
    #: declaration type -> normalised value dict
    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    label_seen_without_value: dict[str, bool] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    evidence_complete: bool = True
    missing_faces: list[str] = field(default_factory=list)
    inspection_date: date | None = None
    commodity_category: str | None = None
    package_type: str | None = None
    quantity_kind: str | None = None
    quantity_base: str | None = None
    is_imported: bool = False
    is_ecommerce: bool = False
    is_multipiece: bool = False
    claimed_exceptions: tuple[str, ...] = ()
    #: Expected outcome, or "not_applicable_by_scope" when the rule should not apply.
    expected_outcome: str = LegalOutcome.COMPLIANT.value

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Scenario:
        inspection_date = payload.get("inspection_date")
        parsed_date = (
            date.fromisoformat(inspection_date) if isinstance(inspection_date, str) else None
        )
        return cls(
            name=str(payload.get("name", "unnamed")),
            kind=str(payload.get("kind", ScenarioKind.COMPLIANT)),
            values=dict(payload.get("values", {})),
            label_seen_without_value=dict(payload.get("label_seen_without_value", {})),
            context=dict(payload.get("context", {})),
            evidence_complete=bool(payload.get("evidence_complete", True)),
            missing_faces=list(payload.get("missing_faces", [])),
            inspection_date=parsed_date,
            commodity_category=payload.get("commodity_category"),
            package_type=payload.get("package_type"),
            quantity_kind=payload.get("quantity_kind"),
            quantity_base=payload.get("quantity_base"),
            is_imported=bool(payload.get("is_imported", False)),
            is_ecommerce=bool(payload.get("is_ecommerce", False)),
            is_multipiece=bool(payload.get("is_multipiece", False)),
            claimed_exceptions=tuple(payload.get("claimed_exceptions", [])),
            expected_outcome=str(payload.get("expected_outcome", LegalOutcome.COMPLIANT.value)),
        )


#: Returned when the rule's scope excludes the scenario entirely.
SCOPE_EXCLUDED = "not_applicable_by_scope"


@dataclass
class ScenarioResult:
    name: str
    kind: str
    expected: str
    actual: str
    passed: bool
    explanation: str
    calculation: list[str] = field(default_factory=list)
    selection_reasons: list[str] = field(default_factory=list)
    blocking_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "expected": self.expected,
            "actual": self.actual,
            "passed": self.passed,
            "explanation": self.explanation,
            "calculation": self.calculation,
            "selection_reasons": self.selection_reasons,
            "blocking_reason": self.blocking_reason,
        }


@dataclass
class SimulationReport:
    results: list[ScenarioResult]
    covered_kinds: list[str]
    required_kinds: list[str]

    @property
    def passed_count(self) -> int:
        return sum(1 for item in self.results if item.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.results if not item.passed)

    @property
    def all_passed(self) -> bool:
        return bool(self.results) and self.failed_count == 0

    @property
    def missing_kinds(self) -> list[str]:
        return [kind for kind in self.required_kinds if kind not in self.covered_kinds]

    @property
    def approval_ready(self) -> bool:
        return self.all_passed and not self.missing_kinds

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenarios": [item.as_dict() for item in self.results],
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "all_passed": self.all_passed,
            "covered_kinds": self.covered_kinds,
            "required_kinds": self.required_kinds,
            "missing_kinds": self.missing_kinds,
            "approval_ready": self.approval_ready,
        }


def required_kinds_for(rule: RuleVersion) -> list[str]:
    required = list(MANDATORY_KINDS)
    if rule.quantity_min_base is not None or rule.quantity_max_base is not None:
        required.extend(BOUNDARY_KINDS)
    if rule.effective_to is not None:
        required.append(ScenarioKind.EXPIRED)
    if rule.exceptions:
        required.append(ScenarioKind.EXCEPTION_APPLIES)
    return required


def run_scenario(rule: RuleVersion, scenario: Scenario) -> ScenarioResult:
    """Run one scenario against a rule version."""
    inspection_date = scenario.inspection_date or rule.effective_from
    quantity_base = Decimal(scenario.quantity_base) if scenario.quantity_base is not None else None

    selection_context = SelectionContext(
        inspection_date=inspection_date,
        commodity_category=scenario.commodity_category,
        package_type=scenario.package_type,
        quantity_kind=scenario.quantity_kind,
        quantity_base=quantity_base,
        is_imported=scenario.is_imported,
        is_ecommerce=scenario.is_ecommerce,
        is_multipiece=scenario.is_multipiece,
        claimed_exceptions=scenario.claimed_exceptions,
    )
    # The status gate is skipped: a rule must be testable while it is still a draft,
    # since passing these tests is a precondition of approval. Every other scope
    # condition (dates, commodity, quantity band, import, exceptions) is applied
    # exactly as it would be on a live inspection.
    applicability = evaluate(rule, selection_context, ignore_status=True)

    if not applicability.applies:
        actual = SCOPE_EXCLUDED
        return ScenarioResult(
            name=scenario.name,
            kind=scenario.kind,
            expected=scenario.expected_outcome,
            actual=actual,
            passed=scenario.expected_outcome == actual,
            explanation=(
                "The rule does not apply to this scenario, so no test was run. "
                f"{applicability.blocking_reason}"
            ),
            selection_reasons=applicability.reasons,
            blocking_reason=applicability.blocking_reason,
        )

    payload = CheckInput(
        values=scenario.values,
        label_seen_without_value=scenario.label_seen_without_value,
        context=scenario.context,
        parameters=dict(rule.test_specification or {}),
        evidence_complete=scenario.evidence_complete,
        missing_faces=scenario.missing_faces,
    )
    outcome = run_check(rule.test_kind, payload)

    return ScenarioResult(
        name=scenario.name,
        kind=scenario.kind,
        expected=scenario.expected_outcome,
        actual=outcome.outcome.value,
        passed=outcome.outcome.value == scenario.expected_outcome,
        explanation=outcome.explanation,
        calculation=outcome.calculation,
        selection_reasons=applicability.reasons,
    )


def simulate(rule: RuleVersion, scenarios: list[Scenario]) -> SimulationReport:
    results = [run_scenario(rule, scenario) for scenario in scenarios]
    covered = sorted({scenario.kind for scenario in scenarios})
    return SimulationReport(
        results=results, covered_kinds=covered, required_kinds=required_kinds_for(rule)
    )


def default_scenarios(rule: RuleVersion) -> list[Scenario]:
    """Generate the date-window scenarios every rule needs.

    Content scenarios (what a compliant or non-compliant package looks like) depend
    on the specific test and are supplied by the rule author. The date-window cases
    are mechanical and are generated here so an author cannot forget them.
    """
    scenarios: list[Scenario] = [
        Scenario(
            name="Inspected the day before the rule takes effect",
            kind=ScenarioKind.NOT_YET_EFFECTIVE,
            inspection_date=rule.effective_from - timedelta(days=1),
            commodity_category=_first(rule.commodity_scope),
            package_type=_first(rule.package_scope),
            quantity_kind=rule.quantity_kind,
            quantity_base=_in_band(rule),
            is_imported=bool(rule.applies_to_imported),
            is_ecommerce=bool(rule.applies_to_ecommerce),
            is_multipiece=bool(rule.applies_to_multipiece),
            expected_outcome=SCOPE_EXCLUDED,
        )
    ]

    if rule.effective_to is not None:
        scenarios.append(
            Scenario(
                name="Inspected the day after the rule ceases to apply",
                kind=ScenarioKind.EXPIRED,
                inspection_date=rule.effective_to + timedelta(days=1),
                commodity_category=_first(rule.commodity_scope),
                package_type=_first(rule.package_scope),
                quantity_kind=rule.quantity_kind,
                quantity_base=_in_band(rule),
                is_imported=bool(rule.applies_to_imported),
                is_ecommerce=bool(rule.applies_to_ecommerce),
                is_multipiece=bool(rule.applies_to_multipiece),
                expected_outcome=SCOPE_EXCLUDED,
            )
        )

    if rule.exceptions:
        first = rule.exceptions[0]
        key = str(first.get("key") if isinstance(first, dict) else first)
        scenarios.append(
            Scenario(
                name=f"Recorded exception {key} removes the requirement",
                kind=ScenarioKind.EXCEPTION_APPLIES,
                inspection_date=rule.effective_from,
                commodity_category=_first(rule.commodity_scope),
                package_type=_first(rule.package_scope),
                quantity_kind=rule.quantity_kind,
                quantity_base=_in_band(rule),
                claimed_exceptions=(key,),
                expected_outcome=SCOPE_EXCLUDED,
            )
        )

    return scenarios


def _first(scope: list[Any] | None) -> str | None:
    if not scope:
        return None
    return str(scope[0])


def _in_band(rule: RuleVersion) -> str | None:
    """A quantity that sits inside the rule's band, for the date scenarios."""
    if rule.quantity_min_base is None and rule.quantity_max_base is None:
        return None
    if rule.quantity_min_base is not None:
        return format(Decimal(str(rule.quantity_min_base)), "f")
    upper = Decimal(str(rule.quantity_max_base))
    return format(upper / 2, "f")


def boundary_scenarios(rule: RuleVersion, template: Scenario) -> list[Scenario]:
    """Exact, below and above scenarios for a rule with a quantity band.

    ``template`` supplies the declaration values; only the quantity moves. The lower
    bound is inclusive and the upper bound exclusive, matching
    ``selection.evaluate``.
    """
    if rule.quantity_min_base is None and rule.quantity_max_base is None:
        return []

    results: list[Scenario] = []
    step = Decimal("0.001")

    if rule.quantity_min_base is not None:
        lower = Decimal(str(rule.quantity_min_base))
        results.append(
            _clone(
                template,
                name=f"Net quantity exactly at the lower bound ({lower})",
                kind=ScenarioKind.BOUNDARY_EXACT,
                quantity_base=format(lower, "f"),
                expected_outcome=template.expected_outcome,
            )
        )
        results.append(
            _clone(
                template,
                name=f"Net quantity just below the lower bound ({lower - step})",
                kind=ScenarioKind.BOUNDARY_BELOW,
                quantity_base=format(lower - step, "f"),
                expected_outcome=SCOPE_EXCLUDED,
            )
        )

    if rule.quantity_max_base is not None:
        upper = Decimal(str(rule.quantity_max_base))
        results.append(
            _clone(
                template,
                name=f"Net quantity just below the upper bound ({upper - step})",
                kind=ScenarioKind.BOUNDARY_EXACT
                if rule.quantity_min_base is None
                else ScenarioKind.BOUNDARY_ABOVE,
                quantity_base=format(upper - step, "f"),
                expected_outcome=template.expected_outcome,
            )
        )
        results.append(
            _clone(
                template,
                name=f"Net quantity exactly at the upper bound ({upper}), which is excluded",
                kind=ScenarioKind.BOUNDARY_ABOVE,
                quantity_base=format(upper, "f"),
                expected_outcome=SCOPE_EXCLUDED,
            )
        )

    return results


def _clone(template: Scenario, **overrides: Any) -> Scenario:
    payload = {
        "name": template.name,
        "kind": template.kind,
        "values": dict(template.values),
        "label_seen_without_value": dict(template.label_seen_without_value),
        "context": dict(template.context),
        "evidence_complete": template.evidence_complete,
        "missing_faces": list(template.missing_faces),
        "inspection_date": (
            template.inspection_date.isoformat() if template.inspection_date else None
        ),
        "commodity_category": template.commodity_category,
        "package_type": template.package_type,
        "quantity_kind": template.quantity_kind,
        "quantity_base": template.quantity_base,
        "is_imported": template.is_imported,
        "is_ecommerce": template.is_ecommerce,
        "is_multipiece": template.is_multipiece,
        "claimed_exceptions": list(template.claimed_exceptions),
        "expected_outcome": template.expected_outcome,
    }
    payload.update(overrides)
    return Scenario.from_dict(payload)
