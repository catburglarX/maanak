"""Extraction pipeline.

Input: the word and line structures produced by ``app.services.ocr``.
Output: ``Match`` objects, each carrying the verbatim text, the parsed value, the
image region and a confidence.

Order of work:

1. Build a character-offset index for each line so a regex span can be traced back
   to the exact words, and therefore to an image region.
2. For each declaration specification, search every line for a printed label.
3. Read the value from the remainder of that line, or from the next line when the
   label sits alone.
4. Where no label was found, fall back to shape recognition for the few
   declarations that have an unmistakable form.
5. Score each match and keep the best candidates per declaration type.

Confidence is the product of two factors, both recorded on the match: the OCR
confidence for the words the value came from, and a locating factor reflecting how
the declaration was identified. It is a transparent number, not a model output, and
it is never presented as a compliance score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ...domain.enums import DeclarationType
from ..ocr import Line, OcrOutcome, Word
from . import text as text_utils
from .fields import SPECS, DeclarationSpec, Match, compiled_label

#: Locating factors. A label on the same line is the strongest signal.
CONFIDENCE_LABEL_SAME_LINE = 1.00
CONFIDENCE_LABEL_NEXT_LINE = 0.85
CONFIDENCE_SHAPE_ONLY = 0.60
#: Applied when the label matched but the value could not be parsed.
CONFIDENCE_UNPARSED = 0.30

#: Maximum candidates kept per declaration type. Packaging can legitimately print
#: the same declaration twice (front and back), and both are worth reviewing.
MAX_CANDIDATES_PER_TYPE = 3


@dataclass
class LineIndex:
    """A line with the character offsets of each of its words."""

    line: Line
    words: list[Word]
    #: (start, end) character offset of each word within ``line.text``.
    offsets: list[tuple[int, int]]

    def words_for_span(self, start: int, end: int) -> list[Word]:
        """Words overlapping a character span."""
        selected = []
        for word, (word_start, word_end) in zip(self.words, self.offsets, strict=False):
            if word_start < end and word_end > start:
                selected.append(word)
        return selected

    def region_for_span(self, start: int, end: int) -> list[int] | None:
        matched = self.words_for_span(start, end)
        if not matched:
            return None
        return [
            min(word.box[0] for word in matched),
            min(word.box[1] for word in matched),
            max(word.box[2] for word in matched),
            max(word.box[3] for word in matched),
        ]

    def confidence_for_span(self, start: int, end: int) -> float:
        matched = self.words_for_span(start, end)
        if not matched:
            return 0.0
        return sum(word.confidence for word in matched) / len(matched)

    def height_for_span(self, start: int, end: int) -> float | None:
        matched = self.words_for_span(start, end)
        if not matched:
            return None
        return sum(word.height_px for word in matched) / len(matched)


def build_line_index(outcome: OcrOutcome) -> list[LineIndex]:
    """Index every line with per-word character offsets.

    ``Line.text`` is built by joining words with a single space, so offsets are
    reconstructed by walking the same join.
    """
    indexes: list[LineIndex] = []
    for line in outcome.lines:
        words = [outcome.words[position] for position in line.word_indexes]
        offsets: list[tuple[int, int]] = []
        cursor = 0
        for position, word in enumerate(words):
            if position:
                cursor += 1  # the joining space
            offsets.append((cursor, cursor + len(word.text)))
            cursor += len(word.text)
        indexes.append(LineIndex(line=line, words=words, offsets=offsets))
    return indexes


def _find_label(line_text: str, spec: DeclarationSpec) -> tuple[str, int, int] | None:
    """Longest matching label in a line, with its span.

    Longest wins so "unit sale price" is not mistaken for "price".
    """
    best: tuple[str, int, int] | None = None
    for label in spec.labels:
        match = compiled_label(label).search(line_text)
        if match is None:
            continue
        if best is None or len(label) > len(best[0]):
            best = (label, match.start(), match.end())
    return best


def _has_negative_label(line_text: str, spec: DeclarationSpec) -> bool:
    return any(
        compiled_label(label).search(line_text) is not None for label in spec.negative_labels
    )


def _numeric_from(parsed: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    """Pull a comparable number and unit out of a parsed value."""
    kind = parsed.get("kind")
    if kind == "money":
        return Decimal(str(parsed["amount"])), parsed.get("currency")
    if kind == "quantity":
        return Decimal(str(parsed["base_amount"])), parsed.get("base_unit")
    return None, None


def extract_matches(outcome: OcrOutcome) -> list[Match]:
    """Locate every declaration in one OCR result."""
    if not outcome.lines:
        return []

    indexes = build_line_index(outcome)
    collected: dict[DeclarationType, list[Match]] = {}

    for spec in SPECS:
        for position, index in enumerate(indexes):
            raw_line = index.line.text
            searchable = text_utils.collapse_ocr_gaps(raw_line)

            label_hit = _find_label(searchable, spec)
            if label_hit is None:
                continue
            if _has_negative_label(searchable, spec):
                continue

            label, label_start, label_end = label_hit
            remainder = raw_line[label_end:] if label_end <= len(raw_line) else ""
            remainder = remainder.lstrip(" :-–=.\t")

            match = _try_value(
                spec=spec,
                index=index,
                value_text=remainder,
                offset=label_end,
                label=label,
                locating_factor=CONFIDENCE_LABEL_SAME_LINE,
            )

            # A label alone on its line: the value is usually printed below it.
            if match is None and spec.allow_next_line and position + 1 < len(indexes):
                following = indexes[position + 1]
                match = _try_value(
                    spec=spec,
                    index=following,
                    value_text=following.line.text,
                    offset=0,
                    label=label,
                    locating_factor=CONFIDENCE_LABEL_NEXT_LINE,
                )

            if match is None:
                # The label is printed but no value could be read from it. This is
                # a real observation and must be recorded, because "label present,
                # value unreadable" is different from "label absent".
                region_confidence = index.confidence_for_span(label_start, label_end)
                match = Match(
                    declaration_type=spec.declaration_type,
                    matched_text=text_utils.truncate_context(raw_line, limit=200),
                    normalised_value={},
                    label_found=label,
                    line_index=index.line.index,
                    span=(label_start, label_end),
                    parse_confidence=round(region_confidence * CONFIDENCE_UNPARSED, 4),
                    notes=["The label was found but no value could be read next to it."],
                )

            collected.setdefault(spec.declaration_type, []).append(match)

        # Shape-only fallback for declarations with an unmistakable form.
        if spec.shape_pattern is not None and not collected.get(spec.declaration_type):
            for index in indexes:
                shape = spec.shape_pattern.search(index.line.text)
                if shape is None:
                    continue
                parsed = spec.parser(shape.group(0))
                if parsed is None:
                    continue
                numeric, unit = _numeric_from(parsed)
                ocr_confidence = index.confidence_for_span(shape.start(), shape.end())
                collected.setdefault(spec.declaration_type, []).append(
                    Match(
                        declaration_type=spec.declaration_type,
                        matched_text=shape.group(0),
                        normalised_value=parsed,
                        numeric_value=numeric,
                        unit=unit,
                        label_found=None,
                        line_index=index.line.index,
                        span=(shape.start(), shape.end()),
                        parse_confidence=round(ocr_confidence * CONFIDENCE_SHAPE_ONLY, 4),
                        notes=[
                            "Recognised by its format. No printed label was found "
                            "next to it, so confirm which declaration this is."
                        ],
                    )
                )

    results: list[Match] = []
    for matches in collected.values():
        ranked = sorted(
            matches, key=lambda item: (item.parsed, item.parse_confidence), reverse=True
        )
        deduplicated: list[Match] = []
        seen: set[str] = set()
        for candidate in ranked:
            fingerprint = _fingerprint(candidate)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            deduplicated.append(candidate)
            if len(deduplicated) >= MAX_CANDIDATES_PER_TYPE:
                break
        results.extend(deduplicated)

    return results


def _try_value(
    *,
    spec: DeclarationSpec,
    index: LineIndex,
    value_text: str,
    offset: int,
    label: str,
    locating_factor: float,
) -> Match | None:
    """Attempt to parse a value from a text fragment."""
    if not value_text or not value_text.strip():
        return None

    parsed = spec.parser(value_text)
    if parsed is None:
        return None

    # Locate the parsed source text inside the line so the region is tight.
    source_text = str(parsed.get("source_text") or parsed.get("value") or value_text)
    span_start, span_end = _locate_span(index.line.text, source_text, offset)

    numeric, unit = _numeric_from(parsed)
    ocr_confidence = index.confidence_for_span(span_start, span_end)

    notes: list[str] = []
    if parsed.get("kind") == "money" and parsed.get("plausible") is False:
        notes.append(
            "The amount is outside the range normally printed on a retail package. "
            "Check it against the photograph."
        )
    if parsed.get("kind") == "date" and parsed.get("ambiguous"):
        alternatives = ", ".join(parsed.get("alternatives", []))
        notes.append(
            f"The printed date could be read more than one way ({alternatives}). "
            "Confirm which is correct."
        )
    if parsed.get("kind") == "quantity" and parsed.get("pieces"):
        notes.append(
            "Read as a multi-piece declaration. The total quantity is used for the "
            "net quantity test."
        )

    return Match(
        declaration_type=spec.declaration_type,
        matched_text=source_text[:400],
        normalised_value=parsed,
        numeric_value=numeric,
        unit=unit,
        label_found=label,
        line_index=index.line.index,
        span=(span_start, span_end),
        parse_confidence=round(max(0.05, ocr_confidence) * locating_factor, 4),
        notes=notes,
    )


def _locate_span(line_text: str, needle: str, fallback_offset: int) -> tuple[int, int]:
    """Character span of ``needle`` within ``line_text``, searching from an offset."""
    if needle:
        position = line_text.find(needle, max(0, fallback_offset - 1))
        if position < 0:
            position = line_text.find(needle)
        if position >= 0:
            return position, position + len(needle)
    return fallback_offset, len(line_text)


def _fingerprint(match: Match) -> str:
    value = match.normalised_value.get("value") or match.normalised_value.get("amount")
    if value is None:
        value = match.matched_text.strip().lower()
    return f"{match.declaration_type}:{value}"


def context_for(outcome: OcrOutcome, line_index: int, *, window: int = 1) -> str:
    """Surrounding lines, so the review screen can show the value in context."""
    lines = outcome.lines
    if not lines:
        return ""
    lower = max(0, line_index - window)
    upper = min(len(lines), line_index + window + 1)
    return "\n".join(line.text for line in lines[lower:upper])


_MULTI_SPACE = re.compile(r"\s{2,}")


def summarise(matches: list[Match]) -> dict[str, Any]:
    """Counts used by the worker log and the inspection summary panel."""
    located = [item for item in matches if item.parsed]
    return {
        "declarations_located": len(located),
        "labels_without_values": len(matches) - len(located),
        "types_found": sorted({item.declaration_type.value for item in located}),
        "mean_confidence": (
            round(sum(item.parse_confidence for item in located) / len(located), 4)
            if located
            else None
        ),
    }
