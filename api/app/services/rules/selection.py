"""Rule version selection.

A rule applies to an inspection only when every scope condition holds. The reason a
version was selected, and the reason each rejected version was not, is recorded on
the finding, so an officer can see why one version was used rather than another.

Selection inputs, in the order they are tested:

1. status - only approved or active versions may influence an inspection;
2. effective window - against the inspection date, not today's date;
3. commodity category;
4. package type;
5. net quantity range, compared in SI base units;
6. import status;
7. e-commerce context;
8. multipiece packaging;
9. declared exceptions.

Where two versions of the same rule code both apply, the one with the later
``effective_from`` wins, and the tie is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from ...domain.enums import EFFECTIVE_RULE_STATUSES, RuleStatus
from ...models.rule import RuleVersion


@dataclass
class SelectionContext:
    """The facts a rule's scope is tested against."""

    inspection_date: date
    commodity_category: str | None = None
    package_type: str | None = None
    quantity_kind: str | None = None
    #: Net quantity in the SI base unit for its dimension (g, ml, m, count).
    quantity_base: Decimal | None = None
    is_imported: bool = False
    is_ecommerce: bool = False
    is_multipiece: bool = False
    #: Exception keys an officer has recorded as applying to this package.
    claimed_exceptions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "inspection_date": self.inspection_date.isoformat(),
            "commodity_category": self.commodity_category,
            "package_type": self.package_type,
            "quantity_kind": self.quantity_kind,
            "quantity_base": format(self.quantity_base, "f")
            if self.quantity_base is not None
            else None,
            "is_imported": self.is_imported,
            "is_ecommerce": self.is_ecommerce,
            "is_multipiece": self.is_multipiece,
            "claimed_exceptions": list(self.claimed_exceptions),
        }


@dataclass
class Applicability:
    """Whether one rule version applies, and why."""

    rule_version: RuleVersion
    applies: bool
    reasons: list[str] = field(default_factory=list)
    blocking_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_code": self.rule_version.code,
            "rule_version": self.rule_version.version,
            "applies": self.applies,
            "reasons": self.reasons,
            "blocking_reason": self.blocking_reason,
        }


def evaluate(
    rule: RuleVersion, context: SelectionContext, *, ignore_status: bool = False
) -> Applicability:
    """Test one rule version against a context.

    ``ignore_status`` is used by the simulator only. A rule must be testable before it
    is approved, otherwise it could never satisfy the tests that approval depends on.
    Live evaluation never sets it, so an unapproved version cannot reach an inspection.
    """
    reasons: list[str] = []

    if ignore_status:
        reasons.append(f"status check skipped for simulation (actual status: {rule.status})")
    elif rule.status not in {status.value for status in EFFECTIVE_RULE_STATUSES}:
        return Applicability(
            rule,
            False,
            reasons,
            blocking_reason=(
                f"Rule version status is {rule.status}. Only approved or active "
                "versions may be applied to an inspection."
            ),
        )
    else:
        reasons.append(f"status is {rule.status}")

    if context.inspection_date < rule.effective_from:
        return Applicability(
            rule,
            False,
            reasons,
            blocking_reason=(
                f"Not yet in force on the inspection date: effective from "
                f"{rule.effective_from.isoformat()}, inspected "
                f"{context.inspection_date.isoformat()}."
            ),
        )
    if rule.effective_to is not None and context.inspection_date > rule.effective_to:
        return Applicability(
            rule,
            False,
            reasons,
            blocking_reason=(
                f"No longer in force on the inspection date: ended "
                f"{rule.effective_to.isoformat()}, inspected "
                f"{context.inspection_date.isoformat()}."
            ),
        )
    reasons.append(
        "in force on the inspection date "
        f"({rule.effective_from.isoformat()} to "
        f"{rule.effective_to.isoformat() if rule.effective_to else 'open'})"
    )

    if rule.commodity_scope:
        allowed = {str(item).lower() for item in rule.commodity_scope}
        actual = (context.commodity_category or "").lower()
        if actual not in allowed:
            return Applicability(
                rule,
                False,
                reasons,
                blocking_reason=(
                    f"Commodity category {context.commodity_category!r} is outside this "
                    f"rule's scope ({', '.join(sorted(allowed))})."
                ),
            )
        reasons.append(f"commodity category {actual!r} is in scope")
    else:
        reasons.append("applies to all commodity categories")

    if rule.package_scope:
        allowed = {str(item).lower() for item in rule.package_scope}
        actual = (context.package_type or "").lower()
        if actual not in allowed:
            return Applicability(
                rule,
                False,
                reasons,
                blocking_reason=(
                    f"Package type {context.package_type!r} is outside this rule's "
                    f"scope ({', '.join(sorted(allowed))})."
                ),
            )
        reasons.append(f"package type {actual!r} is in scope")

    if rule.quantity_kind is not None:
        if context.quantity_kind != rule.quantity_kind:
            return Applicability(
                rule,
                False,
                reasons,
                blocking_reason=(
                    f"This rule covers {rule.quantity_kind} declarations; this package "
                    f"declares {context.quantity_kind or 'no'} quantity."
                ),
            )
        reasons.append(f"quantity dimension is {rule.quantity_kind}")

    if rule.quantity_min_base is not None or rule.quantity_max_base is not None:
        if context.quantity_base is None:
            return Applicability(
                rule,
                False,
                reasons,
                blocking_reason=(
                    "This rule applies only within a net quantity range, and the net "
                    "quantity for this package is not established."
                ),
            )
        lower = rule.quantity_min_base
        upper = rule.quantity_max_base
        if lower is not None and context.quantity_base < lower:
            return Applicability(
                rule,
                False,
                reasons,
                blocking_reason=(
                    f"Net quantity {context.quantity_base} is below this rule's lower "
                    f"bound of {lower} (inclusive)."
                ),
            )
        # Upper bound is exclusive so adjacent bands cannot both match a boundary.
        if upper is not None and context.quantity_base >= upper:
            return Applicability(
                rule,
                False,
                reasons,
                blocking_reason=(
                    f"Net quantity {context.quantity_base} is at or above this rule's "
                    f"upper bound of {upper} (exclusive)."
                ),
            )
        reasons.append(
            f"net quantity {context.quantity_base} falls in "
            f"[{lower if lower is not None else '-inf'}, "
            f"{upper if upper is not None else 'inf'})"
        )

    if rule.applies_to_imported is not None and rule.applies_to_imported != context.is_imported:
        return Applicability(
            rule,
            False,
            reasons,
            blocking_reason=(
                f"This rule applies where imported is {rule.applies_to_imported}; this "
                f"package is recorded as imported={context.is_imported}."
            ),
        )
    if rule.applies_to_ecommerce is not None and rule.applies_to_ecommerce != context.is_ecommerce:
        return Applicability(
            rule,
            False,
            reasons,
            blocking_reason=(
                f"This rule applies where the e-commerce context is "
                f"{rule.applies_to_ecommerce}; this inspection is "
                f"{context.is_ecommerce}."
            ),
        )
    if (
        rule.applies_to_multipiece is not None
        and rule.applies_to_multipiece != context.is_multipiece
    ):
        return Applicability(
            rule,
            False,
            reasons,
            blocking_reason=(
                f"This rule applies where multipiece is {rule.applies_to_multipiece}; "
                f"this package is {context.is_multipiece}."
            ),
        )

    for exception in rule.exceptions or []:
        key = str(exception.get("key") if isinstance(exception, dict) else exception)
        if key in context.claimed_exceptions:
            description = exception.get("description", key) if isinstance(exception, dict) else key
            return Applicability(
                rule,
                False,
                reasons,
                blocking_reason=(
                    f"An exception recorded on this inspection removes the requirement: "
                    f"{description}"
                ),
            )

    return Applicability(rule, True, reasons)


def select(
    candidates: list[RuleVersion], context: SelectionContext
) -> tuple[list[Applicability], list[Applicability]]:
    """Split candidates into applicable and non-applicable, resolving duplicates.

    Returns ``(selected, rejected)``. Where several versions of the same rule code
    apply, only the latest by effective date is selected and the others are moved to
    ``rejected`` with the reason recorded.
    """
    evaluated = [evaluate(rule, context) for rule in candidates]
    applicable = [item for item in evaluated if item.applies]
    rejected = [item for item in evaluated if not item.applies]

    by_code: dict[str, list[Applicability]] = {}
    for item in applicable:
        by_code.setdefault(item.rule_version.code, []).append(item)

    selected: list[Applicability] = []
    for code, group in by_code.items():
        if len(group) == 1:
            selected.append(group[0])
            continue
        ordered = sorted(
            group,
            key=lambda item: (item.rule_version.effective_from, item.rule_version.version),
            reverse=True,
        )
        winner = ordered[0]
        winner.reasons.append(
            f"selected over {len(ordered) - 1} other in-force version(s) of {code} "
            "because it has the latest effective date"
        )
        selected.append(winner)
        for loser in ordered[1:]:
            loser.applies = False
            loser.blocking_reason = (
                f"Superseded for this inspection by {code} version "
                f"{winner.rule_version.version}, effective "
                f"{winner.rule_version.effective_from.isoformat()}."
            )
            rejected.append(loser)

    selected.sort(key=lambda item: item.rule_version.code)
    return selected, rejected


def is_active_status(status: str) -> bool:
    return status in {RuleStatus.APPROVED.value, RuleStatus.ACTIVE.value}
