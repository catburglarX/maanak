"""Declaration extraction and value normalisation.

``units``, ``money`` and ``dates`` are pure value parsers with no OCR dependency, so
they can be unit-tested directly and reused for officer-typed input. ``fields``
holds the declaration table, and ``pipeline`` joins OCR output to that table.
"""

from .dates import DateMarking, Precision, find_all_date_markings, parse_date_marking
from .fields import SPECS, SPECS_BY_TYPE, DeclarationSpec, Match
from .money import Money, amounts_match, parse_money, quantise_paise, unit_sale_price
from .pipeline import extract_matches, summarise
from .units import Quantity, find_all_quantities, parse_quantity, to_unit

__all__ = [
    "SPECS",
    "SPECS_BY_TYPE",
    "DateMarking",
    "DeclarationSpec",
    "Match",
    "Money",
    "Precision",
    "Quantity",
    "amounts_match",
    "extract_matches",
    "find_all_date_markings",
    "find_all_quantities",
    "parse_date_marking",
    "parse_money",
    "parse_quantity",
    "quantise_paise",
    "summarise",
    "to_unit",
    "unit_sale_price",
]
