"""Deterministic checks.

Each check is a pure function: given inputs it returns the same outcome every time,
with the calculation recorded. No check consults the network, the clock or a model.

Two rules govern every check in this module:

1. **Absent evidence is not a violation.** When a required input is missing the
   outcome is ``unable_to_determine`` or ``additional_evidence_required``, never
   ``non_compliant``. "We could not read the MRP" and "the package has no MRP" are
   different findings, and only an officer who has looked at the package can turn
   the first into the second.

2. **Every number is a Decimal.** Prices and quantities never touch binary floating
   point, and any rounding is stated in the calculation steps.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ...domain.enums import DeclarationType, LegalOutcome
from ..extraction import money as money_utils
from ..extraction import units as unit_utils

ENGINE_VERSION = "1"


@dataclass
class CheckInput:
    """Everything a check may read.

    ``values`` holds the officer-reviewed value for each declaration type. A
    declaration that was located but rejected by the officer is absent here, which
    is the correct behaviour: a rejected reading must not support a conclusion.
    """

    #: declaration type -> normalised value dict (officer-corrected where applicable)
    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: declaration type -> True when a label was seen but no value could be read
    label_seen_without_value: dict[str, bool] = field(default_factory=dict)
    #: Product and inspection context used for scope and for some checks.
    context: dict[str, Any] = field(default_factory=dict)
    #: Parameters from the rule version's ``test_specification``.
    parameters: dict[str, Any] = field(default_factory=dict)
    #: True when every required package face was captured or explicitly accounted for.
    evidence_complete: bool = False
    #: Faces that were not captured, so a check can say what is still needed.
    missing_faces: list[str] = field(default_factory=list)

    def value(self, declaration: DeclarationType | str) -> dict[str, Any] | None:
        return self.values.get(str(declaration))

    def has(self, declaration: DeclarationType | str) -> bool:
        return bool(self.values.get(str(declaration)))

    def label_only(self, declaration: DeclarationType | str) -> bool:
        return bool(self.label_seen_without_value.get(str(declaration)))

    def parameter(self, name: str, default: Any = None) -> Any:
        return self.parameters.get(name, default)


@dataclass
class CheckResult:
    """The outcome of one check."""

    outcome: LegalOutcome
    explanation: str
    calculation: list[str] = field(default_factory=list)
    test_inputs: dict[str, Any] = field(default_factory=dict)
    expected_value: str | None = None
    observed_value: str | None = None
    #: Declaration whose region should be highlighted for this finding.
    focus_declaration: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "explanation": self.explanation,
            "calculation": self.calculation,
            "test_inputs": self.test_inputs,
            "expected_value": self.expected_value,
            "observed_value": self.observed_value,
            "focus_declaration": self.focus_declaration,
        }


CheckFunction = Callable[[CheckInput], CheckResult]
_REGISTRY: dict[str, CheckFunction] = {}


def register(kind: str) -> Callable[[CheckFunction], CheckFunction]:
    def decorator(function: CheckFunction) -> CheckFunction:
        _REGISTRY[kind] = function
        return function

    return decorator


def available_checks() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def run_check(kind: str, payload: CheckInput) -> CheckResult:
    check = _REGISTRY.get(kind)
    if check is None:
        return CheckResult(
            outcome=LegalOutcome.UNABLE_TO_DETERMINE,
            explanation=(
                f"This rule version names a test ({kind}) that this version of the "
                "software does not implement. The rule cannot be applied."
            ),
            test_inputs={"requested_check": kind},
        )
    return check(payload)


def _display(value: dict[str, Any] | None) -> str | None:
    if not value:
        return None
    return str(value.get("display") or value.get("value") or "")


# --------------------------------------------------------------------------
# Presence of a declaration
# --------------------------------------------------------------------------
@register("declaration_present")
def check_declaration_present(payload: CheckInput) -> CheckResult:
    """A named declaration must appear on the package.

    Parameters
    ----------
    declaration
        The declaration type that must be present.
    label
        Human-readable name used in the explanation.
    """
    declaration = str(payload.parameter("declaration", ""))
    label = str(payload.parameter("label", declaration.replace("_", " ")))
    value = payload.value(declaration)

    inputs = {
        "declaration": declaration,
        "present": bool(value),
        "evidence_complete": payload.evidence_complete,
        "label_seen_without_value": payload.label_only(declaration),
    }

    if value:
        return CheckResult(
            outcome=LegalOutcome.COMPLIANT,
            explanation=f"{label} is declared on the package as {_display(value)}.",
            test_inputs=inputs,
            observed_value=_display(value),
            focus_declaration=declaration,
            calculation=[f"{label} found: {_display(value)}"],
        )

    if payload.label_only(declaration):
        return CheckResult(
            outcome=LegalOutcome.UNABLE_TO_DETERMINE,
            explanation=(
                f"A {label} label is printed on the package but its value could not "
                "be read from the evidence supplied. Read the value from the package "
                "and record it, or capture a clearer image of that panel."
            ),
            test_inputs=inputs,
            focus_declaration=declaration,
            calculation=[f"{label} label located, value unreadable"],
        )

    if not payload.evidence_complete:
        missing = ", ".join(payload.missing_faces) if payload.missing_faces else "some faces"
        return CheckResult(
            outcome=LegalOutcome.ADDITIONAL_EVIDENCE_REQUIRED,
            explanation=(
                f"{label} was not found, but the package has not been fully "
                f"photographed ({missing} outstanding). Capture the remaining faces "
                "before concluding that the declaration is absent."
            ),
            test_inputs=inputs,
            calculation=[f"{label} not found", "evidence coverage incomplete"],
        )

    return CheckResult(
        outcome=LegalOutcome.NON_COMPLIANT,
        explanation=(
            f"{label} was not found on any captured face of the package, and every "
            "required face has been accounted for."
        ),
        test_inputs=inputs,
        expected_value=f"{label} declared",
        observed_value="not found",
        calculation=[
            f"{label} not found in any reviewed declaration",
            "all required package faces captured or accounted for",
        ],
    )


@register("any_declaration_present")
def check_any_declaration_present(payload: CheckInput) -> CheckResult:
    """At least one of a set of declarations must be present.

    Used where the rules accept alternatives, for example a manufacturer, packer or
    importer name.
    """
    declarations = [str(item) for item in payload.parameter("declarations", [])]
    label = str(payload.parameter("label", "one of the required declarations"))
    present = [item for item in declarations if payload.has(item)]

    inputs = {
        "declarations": declarations,
        "present": present,
        "evidence_complete": payload.evidence_complete,
    }

    if present:
        found = present[0]
        return CheckResult(
            outcome=LegalOutcome.COMPLIANT,
            explanation=(
                f"{label} is declared: {found.replace('_', ' ')} recorded as "
                f"{_display(payload.value(found))}."
            ),
            test_inputs=inputs,
            observed_value=_display(payload.value(found)),
            focus_declaration=found,
            calculation=[f"accepted alternatives: {', '.join(declarations)}", f"found: {found}"],
        )

    if any(payload.label_only(item) for item in declarations):
        return CheckResult(
            outcome=LegalOutcome.UNABLE_TO_DETERMINE,
            explanation=(
                f"A label for {label} is printed but its value could not be read. "
                "Record the printed value or capture a clearer image."
            ),
            test_inputs=inputs,
        )

    if not payload.evidence_complete:
        return CheckResult(
            outcome=LegalOutcome.ADDITIONAL_EVIDENCE_REQUIRED,
            explanation=(
                f"{label} was not found, but the package has not been fully "
                "photographed. Capture the remaining faces before concluding."
            ),
            test_inputs=inputs,
        )

    return CheckResult(
        outcome=LegalOutcome.NON_COMPLIANT,
        explanation=f"{label} was not found on any captured face of the package.",
        test_inputs=inputs,
        expected_value=f"one of: {', '.join(declarations)}",
        observed_value="none found",
    )


# --------------------------------------------------------------------------
# Unit sale price consistency
# --------------------------------------------------------------------------
@register("unit_sale_price_consistency")
def check_unit_sale_price(payload: CheckInput) -> CheckResult:
    """The printed unit sale price must agree with price divided by quantity.

    Parameters
    ----------
    per_units
        How many base units the unit price refers to, for example 100 for a price
        per 100 g. Default 100.
    tolerance_paise
        Permitted difference in paise. Default 1, absorbing the packer's own rounding.
    """
    price_value = payload.value(DeclarationType.MRP)
    quantity_value = payload.value(DeclarationType.NET_QUANTITY)
    printed_value = payload.value(DeclarationType.UNIT_SALE_PRICE)

    per_units = Decimal(str(payload.parameter("per_units", 100)))
    tolerance = Decimal(str(payload.parameter("tolerance_paise", 1))) / Decimal(100)

    inputs = {
        "mrp": price_value,
        "net_quantity": quantity_value,
        "printed_unit_sale_price": printed_value,
        "per_units": str(per_units),
        "tolerance": str(tolerance),
    }

    if not price_value or not quantity_value:
        missing = []
        if not price_value:
            missing.append("retail sale price")
        if not quantity_value:
            missing.append("net quantity")
        return CheckResult(
            outcome=LegalOutcome.UNABLE_TO_DETERMINE,
            explanation=(
                "The unit sale price cannot be checked because the "
                f"{' and '.join(missing)} could not be read from the evidence."
            ),
            test_inputs=inputs,
        )

    try:
        price = Decimal(str(price_value["amount"]))
        quantity_base = Decimal(str(quantity_value["base_amount"]))
        base_unit = str(quantity_value.get("base_unit", ""))
    except (KeyError, TypeError, ValueError):
        return CheckResult(
            outcome=LegalOutcome.UNABLE_TO_DETERMINE,
            explanation="The recorded price or quantity is not in a form this test can use.",
            test_inputs=inputs,
        )

    if quantity_base <= 0:
        return CheckResult(
            outcome=LegalOutcome.UNABLE_TO_DETERMINE,
            explanation=(
                "The recorded net quantity is zero or negative, so no unit price " "can be derived."
            ),
            test_inputs=inputs,
        )

    # A count-based package has no meaningful price per 100 units of weight.
    if quantity_value.get("dimension") == "count" and payload.parameter("skip_for_count", True):
        return CheckResult(
            outcome=LegalOutcome.NOT_APPLICABLE,
            explanation=(
                "The net quantity is declared as a number of pieces, so a unit sale "
                "price per unit of weight or volume does not apply."
            ),
            test_inputs=inputs,
        )

    exact, rounded, steps = money_utils.unit_sale_price(price, quantity_base, per_units=per_units)
    expected_display = f"₹{rounded} per {per_units} {base_unit}"

    if not printed_value:
        if payload.label_only(DeclarationType.UNIT_SALE_PRICE):
            return CheckResult(
                outcome=LegalOutcome.UNABLE_TO_DETERMINE,
                explanation=(
                    "A unit sale price label is printed but its value could not be "
                    f"read. The expected value from the declared price and quantity "
                    f"is {expected_display}."
                ),
                calculation=steps,
                test_inputs=inputs,
                expected_value=expected_display,
            )
        return CheckResult(
            outcome=LegalOutcome.NON_COMPLIANT,
            explanation=(
                "No unit sale price is declared. From the declared retail sale price "
                f"and net quantity it should be {expected_display}."
            ),
            calculation=steps,
            test_inputs=inputs,
            expected_value=expected_display,
            observed_value="not declared",
            focus_declaration=str(DeclarationType.MRP),
        )

    try:
        printed = Decimal(str(printed_value["amount"]))
    except (KeyError, TypeError, ValueError):
        return CheckResult(
            outcome=LegalOutcome.UNABLE_TO_DETERMINE,
            explanation="The printed unit sale price is not in a form this test can use.",
            test_inputs=inputs,
        )

    difference = abs(money_utils.quantise_paise(printed) - rounded)
    steps = [
        *steps,
        f"printed unit sale price = {format(printed, 'f')}",
        f"difference = |{format(money_utils.quantise_paise(printed), 'f')} - "
        f"{format(rounded, 'f')}| = {format(difference, 'f')}",
        f"tolerance = {format(tolerance, 'f')}",
    ]
    matched = difference <= tolerance

    return CheckResult(
        outcome=LegalOutcome.COMPLIANT if matched else LegalOutcome.NON_COMPLIANT,
        explanation=(
            f"The printed unit sale price of ₹{printed} agrees with the declared "
            f"price and net quantity ({expected_display})."
            if matched
            else (
                f"The printed unit sale price of ₹{printed} does not agree with the "
                f"declared retail sale price and net quantity, which give "
                f"{expected_display}. The difference is ₹{difference}."
            )
        ),
        calculation=steps,
        test_inputs=inputs,
        expected_value=expected_display,
        observed_value=f"₹{printed}",
        focus_declaration=str(DeclarationType.UNIT_SALE_PRICE),
    )


# --------------------------------------------------------------------------
# Net quantity unit
# --------------------------------------------------------------------------
@register("net_quantity_unit_permitted")
def check_net_quantity_unit(payload: CheckInput) -> CheckResult:
    """The net quantity must be declared in a permitted unit for its dimension.

    Parameters
    ----------
    permitted_units
        Mapping of dimension to the list of acceptable unit symbols.
    """
    quantity_value = payload.value(DeclarationType.NET_QUANTITY)
    permitted: dict[str, list[str]] = payload.parameter("permitted_units", {})

    inputs = {"net_quantity": quantity_value, "permitted_units": permitted}

    if not quantity_value:
        return CheckResult(
            outcome=LegalOutcome.UNABLE_TO_DETERMINE,
            explanation="The net quantity could not be read, so its unit cannot be checked.",
            test_inputs=inputs,
        )

    dimension = str(quantity_value.get("dimension", ""))
    unit = str(quantity_value.get("unit", ""))
    allowed = permitted.get(dimension)

    if allowed is None:
        return CheckResult(
            outcome=LegalOutcome.NOT_APPLICABLE,
            explanation=(
                f"This rule version lists no permitted units for a {dimension} "
                "declaration, so it does not apply to this package."
            ),
            test_inputs=inputs,
        )

    canonical = unit_utils.normalise_unit(unit) or unit
    acceptable = {unit_utils.normalise_unit(item) or item for item in allowed}
    permitted_display = ", ".join(sorted(allowed))

    if canonical in acceptable:
        return CheckResult(
            outcome=LegalOutcome.COMPLIANT,
            explanation=(
                f"The net quantity is declared in {canonical}, which is a permitted "
                f"unit for a {dimension} declaration."
            ),
            test_inputs=inputs,
            expected_value=permitted_display,
            observed_value=canonical,
            focus_declaration=str(DeclarationType.NET_QUANTITY),
            calculation=[f"declared unit = {canonical}", f"permitted = {permitted_display}"],
        )

    return CheckResult(
        outcome=LegalOutcome.NON_COMPLIANT,
        explanation=(
            f"The net quantity is declared in {canonical}. For a {dimension} "
            f"declaration the permitted units are {permitted_display}."
        ),
        test_inputs=inputs,
        expected_value=permitted_display,
        observed_value=canonical,
        focus_declaration=str(DeclarationType.NET_QUANTITY),
        calculation=[f"declared unit = {canonical}", f"permitted = {permitted_display}"],
    )


# --------------------------------------------------------------------------
# Date marking
# --------------------------------------------------------------------------
@register("date_marking_completeness")
def check_date_marking(payload: CheckInput) -> CheckResult:
    """A date marking must state at least the month and the year.

    Parameters
    ----------
    accepted_declarations
        Which date declarations satisfy the rule, in order of preference.
    required_precision
        "month" (default) or "day".
    """
    accepted = [
        str(item)
        for item in payload.parameter(
            "accepted_declarations",
            [
                DeclarationType.DATE_OF_MANUFACTURE,
                DeclarationType.DATE_OF_PACKING,
                DeclarationType.DATE_OF_IMPORT,
            ],
        )
    ]
    required_precision = str(payload.parameter("required_precision", "month"))
    precision_rank = {"year": 1, "month": 2, "day": 3}
    needed = precision_rank.get(required_precision, 2)

    found: tuple[str, dict[str, Any]] | None = None
    for declaration in accepted:
        value = payload.value(declaration)
        if value:
            found = (declaration, value)
            break

    inputs = {
        "accepted_declarations": accepted,
        "required_precision": required_precision,
        "found": found[0] if found else None,
        "value": found[1] if found else None,
    }

    if found is None:
        if any(payload.label_only(item) for item in accepted):
            return CheckResult(
                outcome=LegalOutcome.UNABLE_TO_DETERMINE,
                explanation=(
                    "A date marking label is printed but the date itself could not be "
                    "read. Record the printed date or capture a clearer image."
                ),
                test_inputs=inputs,
            )
        if not payload.evidence_complete:
            return CheckResult(
                outcome=LegalOutcome.ADDITIONAL_EVIDENCE_REQUIRED,
                explanation=(
                    "No date of manufacture, packing or import was found, and the "
                    "package has not been fully photographed."
                ),
                test_inputs=inputs,
            )
        return CheckResult(
            outcome=LegalOutcome.NON_COMPLIANT,
            explanation=(
                "No date of manufacture, packing or import was found on any captured "
                "face of the package."
            ),
            test_inputs=inputs,
            expected_value=f"date stated to {required_precision} precision",
            observed_value="not found",
        )

    declaration, value = found
    actual_precision = str(value.get("precision", "year"))
    actual_rank = precision_rank.get(actual_precision, 1)

    if value.get("ambiguous"):
        return CheckResult(
            outcome=LegalOutcome.UNABLE_TO_DETERMINE,
            explanation=(
                f"The printed date {value.get('source_text')!r} can be read more than "
                "one way. Confirm the intended date before this rule is applied."
            ),
            test_inputs=inputs,
            observed_value=str(value.get("display")),
            focus_declaration=declaration,
        )

    if actual_rank >= needed:
        return CheckResult(
            outcome=LegalOutcome.COMPLIANT,
            explanation=(
                f"{declaration.replace('_', ' ').capitalize()} is declared as "
                f"{value.get('display')}, which states at least the {required_precision}."
            ),
            test_inputs=inputs,
            expected_value=f"at least {required_precision}",
            observed_value=str(value.get("display")),
            focus_declaration=declaration,
            calculation=[
                f"declared precision = {actual_precision}",
                f"required precision = {required_precision}",
            ],
        )

    return CheckResult(
        outcome=LegalOutcome.NON_COMPLIANT,
        explanation=(
            f"The date marking reads {value.get('display')}, which states only the "
            f"{actual_precision}. The {required_precision} is also required."
        ),
        test_inputs=inputs,
        expected_value=f"at least {required_precision}",
        observed_value=str(value.get("display")),
        focus_declaration=declaration,
        calculation=[
            f"declared precision = {actual_precision}",
            f"required precision = {required_precision}",
        ],
    )


# --------------------------------------------------------------------------
# Country of origin for imported packages
# --------------------------------------------------------------------------
@register("country_of_origin_required")
def check_country_of_origin(payload: CheckInput) -> CheckResult:
    """Country of origin must be declared where the package is imported.

    Import status comes from the inspection record, set by the officer, not
    inferred from a barcode prefix.
    """
    is_imported = bool(payload.context.get("is_imported"))
    value = payload.value(DeclarationType.COUNTRY_OF_ORIGIN)
    inputs = {
        "is_imported": is_imported,
        "country_of_origin": value,
        "evidence_complete": payload.evidence_complete,
    }

    if not is_imported:
        return CheckResult(
            outcome=LegalOutcome.NOT_APPLICABLE,
            explanation=(
                "The inspection records this package as not imported, so a country of "
                "origin declaration is not required by this rule."
            ),
            test_inputs=inputs,
            calculation=["import status recorded on the inspection: not imported"],
        )

    if value:
        return CheckResult(
            outcome=LegalOutcome.COMPLIANT,
            explanation=f"Country of origin is declared as {_display(value)}.",
            test_inputs=inputs,
            observed_value=_display(value),
            focus_declaration=str(DeclarationType.COUNTRY_OF_ORIGIN),
        )

    if not payload.evidence_complete:
        return CheckResult(
            outcome=LegalOutcome.ADDITIONAL_EVIDENCE_REQUIRED,
            explanation=(
                "This package is recorded as imported and no country of origin was "
                "found, but the package has not been fully photographed."
            ),
            test_inputs=inputs,
        )

    return CheckResult(
        outcome=LegalOutcome.NON_COMPLIANT,
        explanation=(
            "This package is recorded as imported but no country of origin "
            "declaration was found on any captured face."
        ),
        test_inputs=inputs,
        expected_value="country of origin declared",
        observed_value="not found",
    )


# --------------------------------------------------------------------------
# Consumer care
# --------------------------------------------------------------------------
@register("consumer_care_completeness")
def check_consumer_care(payload: CheckInput) -> CheckResult:
    """Consumer care details must give a usable way to make contact.

    Parameters
    ----------
    require_any_of
        Declarations that each independently satisfy the rule.
    """
    accepted = [
        str(item)
        for item in payload.parameter(
            "require_any_of",
            [
                DeclarationType.CONSUMER_CARE_EMAIL,
                DeclarationType.CONSUMER_CARE_PHONE,
                DeclarationType.CONSUMER_CARE_NAME,
            ],
        )
    ]
    present = {item: payload.value(item) for item in accepted if payload.has(item)}
    inputs = {"accepted": accepted, "present": list(present)}

    if present:
        described = "; ".join(
            f"{name.replace('consumer_care_', '').replace('_', ' ')}: {_display(value)}"
            for name, value in present.items()
        )
        return CheckResult(
            outcome=LegalOutcome.COMPLIANT,
            explanation=f"Consumer care details are declared ({described}).",
            test_inputs=inputs,
            observed_value=described[:240],
            focus_declaration=next(iter(present)),
        )

    if any(payload.label_only(item) for item in accepted):
        return CheckResult(
            outcome=LegalOutcome.UNABLE_TO_DETERMINE,
            explanation=(
                "A consumer care label is printed but no contact detail could be "
                "read next to it. Record the printed detail or capture a clearer image."
            ),
            test_inputs=inputs,
        )

    if not payload.evidence_complete:
        return CheckResult(
            outcome=LegalOutcome.ADDITIONAL_EVIDENCE_REQUIRED,
            explanation=(
                "No consumer care detail was found, but the package has not been "
                "fully photographed."
            ),
            test_inputs=inputs,
        )

    return CheckResult(
        outcome=LegalOutcome.NON_COMPLIANT,
        explanation=(
            "No consumer care contact detail (name, telephone number or email "
            "address) was found on any captured face of the package."
        ),
        test_inputs=inputs,
        expected_value="a consumer care name, telephone number or email address",
        observed_value="none found",
    )


# --------------------------------------------------------------------------
# Character height
# --------------------------------------------------------------------------
@register("character_height_minimum")
def check_character_height(payload: CheckInput) -> CheckResult:
    """Printed characters must reach a minimum height.

    A height in millimetres cannot be derived from an image alone: pixels only
    become millimetres once the scale is known. The officer therefore supplies a
    measurement, either with a physical rule against the package or by calibrating
    against an object of known size in the photograph.

    Without that measurement the outcome is ``additional_evidence_required``. It is
    never guessed from pixel counts, because the same pixel height means different
    millimetres at different camera distances.

    Parameters
    ----------
    minimum_height_mm
        The required height.
    uncertainty_mm
        Measurement uncertainty. Where the measured value lies within this band of
        the threshold the outcome is ``unable_to_determine`` rather than a decision
        that cannot be defended.
    declaration
        Which declaration was measured.
    """
    minimum = Decimal(str(payload.parameter("minimum_height_mm", "1.0")))
    uncertainty = Decimal(str(payload.parameter("uncertainty_mm", "0.2")))
    declaration = str(payload.parameter("declaration", DeclarationType.NET_QUANTITY))

    measurement = payload.context.get("character_height_measurements", {}).get(declaration)
    inputs = {
        "declaration": declaration,
        "minimum_height_mm": str(minimum),
        "uncertainty_mm": str(uncertainty),
        "measurement": measurement,
    }

    if not measurement or measurement.get("observed_mm") in (None, ""):
        return CheckResult(
            outcome=LegalOutcome.ADDITIONAL_EVIDENCE_REQUIRED,
            explanation=(
                "Character height cannot be established from a photograph alone, "
                "because pixels only convert to millimetres once the scale is known. "
                "Measure the printed height of the "
                f"{declaration.replace('_', ' ')} declaration against a rule, or "
                "photograph it beside an object of known size and record the "
                "measurement, then run the checks again."
            ),
            test_inputs=inputs,
            expected_value=f"at least {minimum} mm",
        )

    observed = Decimal(str(measurement["observed_mm"]))
    method = str(measurement.get("method", "unspecified"))
    stated_uncertainty = Decimal(str(measurement.get("uncertainty_mm", uncertainty)))

    steps = [
        f"required minimum height = {format(minimum, 'f')} mm",
        f"measured height = {format(observed, 'f')} mm (method: {method})",
        f"measurement uncertainty = ±{format(stated_uncertainty, 'f')} mm",
        f"lower bound of measurement = {format(observed - stated_uncertainty, 'f')} mm",
        f"upper bound of measurement = {format(observed + stated_uncertainty, 'f')} mm",
    ]

    if observed - stated_uncertainty >= minimum:
        return CheckResult(
            outcome=LegalOutcome.COMPLIANT,
            explanation=(
                f"The measured character height of {observed} mm meets the minimum of "
                f"{minimum} mm even at the lower bound of the measurement uncertainty."
            ),
            calculation=steps,
            test_inputs=inputs,
            expected_value=f"at least {minimum} mm",
            observed_value=f"{observed} mm",
            focus_declaration=declaration,
        )

    if observed + stated_uncertainty < minimum:
        return CheckResult(
            outcome=LegalOutcome.NON_COMPLIANT,
            explanation=(
                f"The measured character height of {observed} mm is below the minimum "
                f"of {minimum} mm even at the upper bound of the measurement "
                "uncertainty."
            ),
            calculation=steps,
            test_inputs=inputs,
            expected_value=f"at least {minimum} mm",
            observed_value=f"{observed} mm",
            focus_declaration=declaration,
        )

    return CheckResult(
        outcome=LegalOutcome.UNABLE_TO_DETERMINE,
        explanation=(
            f"The measured character height of {observed} mm ± {stated_uncertainty} mm "
            f"straddles the {minimum} mm minimum, so the measurement cannot decide the "
            "question either way. Measure again with a finer method before recording "
            "an outcome."
        ),
        calculation=steps,
        test_inputs=inputs,
        expected_value=f"at least {minimum} mm",
        observed_value=f"{observed} mm ± {stated_uncertainty} mm",
        focus_declaration=declaration,
    )


# --------------------------------------------------------------------------
# Price wording
# --------------------------------------------------------------------------
@register("retail_price_wording")
def check_retail_price_wording(payload: CheckInput) -> CheckResult:
    """The retail sale price must be expressed in the prescribed manner.

    Parameters
    ----------
    required_phrases
        Any one of these phrases satisfies the rule.
    """
    required = [
        str(item).lower()
        for item in payload.parameter(
            "required_phrases",
            ["inclusive of all taxes", "incl. of all taxes", "incl of all taxes"],
        )
    ]
    value = payload.value(DeclarationType.MRP)
    context_text = str(payload.context.get("mrp_context_text", "")).lower()

    inputs = {
        "required_phrases": required,
        "mrp": value,
        "context_present": bool(context_text),
    }

    if not value:
        return CheckResult(
            outcome=LegalOutcome.UNABLE_TO_DETERMINE,
            explanation=(
                "The retail sale price could not be read, so its wording cannot " "be checked."
            ),
            test_inputs=inputs,
        )
    if not context_text:
        return CheckResult(
            outcome=LegalOutcome.UNABLE_TO_DETERMINE,
            explanation=(
                "The text printed around the retail sale price was not captured, so "
                "the wording cannot be checked. Capture a closer image of the price."
            ),
            test_inputs=inputs,
            focus_declaration=str(DeclarationType.MRP),
        )

    matched = [phrase for phrase in required if phrase in context_text]
    if matched:
        return CheckResult(
            outcome=LegalOutcome.COMPLIANT,
            explanation=(f"The retail sale price is qualified with {matched[0]!r}, as required."),
            test_inputs=inputs,
            expected_value=" or ".join(required),
            observed_value=matched[0],
            focus_declaration=str(DeclarationType.MRP),
        )

    return CheckResult(
        outcome=LegalOutcome.NON_COMPLIANT,
        explanation=(
            "The retail sale price is printed without the required qualifying wording "
            f"({' or '.join(required)}). The text read around the price was: "
            f"{context_text[:160]!r}"
        ),
        test_inputs=inputs,
        expected_value=" or ".join(required),
        observed_value="qualifying wording not found",
        focus_declaration=str(DeclarationType.MRP),
    )
