"""Human labels for the domain enumerations.

Every enum value can be turned into a label mechanically by replacing underscores
with spaces and capitalising the first letter. That is right most of the time and
wrong in the places a reader notices: it produced "Mrp close up" for
``mrp_close_up``, "Fssai licence" for ``fssai_licence`` and "Non compliant" for
``non_compliant``.

So the mechanical rule stays as the default and this module holds the exceptions:
acronyms, hyphenated compounds and the two or three phrases that read better
reordered. Anything absent from :data:`OVERRIDES` keeps the mechanical label, which
means adding an enum member never requires editing this file unless its label is
one of those exceptions.

The labels are served from ``/api/v1/reference/vocabulary`` and are what the browser
renders, so this is the only place a value's wording is decided.
"""

from __future__ import annotations

from enum import StrEnum

#: Values whose mechanical label is wrong. Keyed by the raw enum value, which is
#: unique across the domain for every member listed here.
OVERRIDES: dict[str, str] = {
    # Acronyms the mechanical rule lower-cases.
    "mrp": "MRP",
    "mrp_close_up": "MRP close-up",
    "mrp_overcharge": "MRP overcharge",
    "fssai_licence": "FSSAI licence",
    "ocr_input": "OCR input",
    # Compounds that take a hyphen.
    "net_quantity_close_up": "Net quantity close-up",
    "date_marking_close_up": "Date marking close-up",
    "non_compliant": "Non-compliant",
    "follow_up_inspection": "Follow-up inspection",
    "show_cause": "Show-cause notice",
    "ecommerce_listing": "E-commerce listing",
    "veg_nonveg_mark": "Veg or non-veg mark",
    # Phrases that read better reordered or spelled out.
    "side_panel_left": "Left side panel",
    "side_panel_right": "Right side panel",
    "admin": "Administrator",
    "rule_admin": "Rule administrator",
    "expired_or_date_issue": "Expired or wrong date",
}


def mechanical(value: str) -> str:
    """Underscores to spaces, first letter capitalised, rest left alone.

    ``str.capitalize`` lower-cases the remainder, which would turn a legitimate
    proper noun into lower case. This keeps whatever the value already had.
    """
    words = value.replace("_", " ")
    return words[:1].upper() + words[1:] if words else words


def label_for(value: str) -> str:
    """The label a reader should see for a raw enum value."""
    return OVERRIDES.get(value, mechanical(value))


def options(enum_class: type[StrEnum]) -> list[dict[str, str]]:
    """``[{"value": ..., "label": ...}]`` for every member, in declaration order."""
    return [{"value": member.value, "label": label_for(member.value)} for member in enum_class]


def options_for(values: tuple[str, ...]) -> list[dict[str, str]]:
    """The same shape for an explicit subset of values, such as the decided states."""
    return [{"value": value, "label": label_for(value)} for value in values]
