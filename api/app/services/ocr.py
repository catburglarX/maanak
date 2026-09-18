"""OCR and barcode reading.

Tesseract is the engine. It runs in the worker process, never in a request
handler, because a full-resolution package photograph takes seconds rather than
milliseconds.

Language handling: Indian packages routinely carry English and Hindi on the same
panel, often in the same line. Rather than guessing one language, the default
profile runs ``eng+hin`` together and a script-detection pass records what was
actually present, so the record shows which script each declaration came from.

Nothing here claims an accuracy figure. ``scripts/ocr_benchmark.py`` measures
character and word error rates against a labelled set, and the measured numbers
live in ``docs/OCR_BENCHMARK.md``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import numpy as np
import pytesseract
from PIL import Image

from ..config import get_settings
from ..errors import ProcessingError
from ..observability import OCR_DURATION, get_logger

logger = get_logger(__name__)

ENGINE_NAME = "tesseract"
REQUIRED_LANGUAGES = ("eng", "hin")

#: Devanagari code point range, used for script attribution.
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
LATIN = re.compile(r"[A-Za-z]")


@dataclass(frozen=True)
class OcrProfile:
    """A named Tesseract configuration.

    ``psm`` choices that matter here:
      3  fully automatic page segmentation - good for a whole panel
      6  assume a single uniform block of text - good for a cropped declaration
      7  single text line - good for a close-up of one line
     11  sparse text, find as much as possible - good for cluttered packaging
    """

    name: str
    languages: str
    psm: int
    oem: int = 3
    extra: str = ""

    @property
    def config(self) -> str:
        parts = [f"--oem {self.oem}", f"--psm {self.psm}"]
        if self.extra:
            parts.append(self.extra)
        return " ".join(parts)


PROFILES: dict[str, OcrProfile] = {
    # Default: mixed-script whole panel.
    "default": OcrProfile("default", "eng+hin", psm=3),
    # Cluttered packaging where text is scattered around graphics.
    "sparse": OcrProfile("sparse", "eng+hin", psm=11),
    # A cropped declaration block.
    "block": OcrProfile("block", "eng+hin", psm=6),
    # A single close-up line, for example an MRP strip.
    "line": OcrProfile("line", "eng+hin", psm=7),
    # English only: faster and slightly more accurate when no Hindi is present.
    "english": OcrProfile("english", "eng", psm=3),
    "hindi": OcrProfile("hindi", "hin", psm=3),
    # Digits and currency punctuation only, for re-reading a numeric field.
    "numeric": OcrProfile(
        "numeric",
        "eng",
        psm=7,
        extra="-c tessedit_char_whitelist=0123456789.,/-₹RsINRrs ",
    ),
}


@dataclass
class Word:
    text: str
    confidence: float
    box: list[int]
    line_index: int
    block_index: int
    height_px: float
    script: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": round(self.confidence, 4),
            "box": self.box,
            "line": self.line_index,
            "block": self.block_index,
            "height_px": round(self.height_px, 2),
            "script": self.script,
        }


@dataclass
class Line:
    index: int
    text: str
    box: list[int]
    word_indexes: list[int]
    mean_confidence: float
    script: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "text": self.text,
            "box": self.box,
            "words": self.word_indexes,
            "mean_confidence": round(self.mean_confidence, 4),
            "script": self.script,
        }


@dataclass
class OcrOutcome:
    profile: str
    languages: str
    engine: str
    engine_version: str
    raw_text: str
    words: list[Word]
    lines: list[Line]
    mean_confidence: float | None
    duration_ms: int
    orientation_applied_degrees: int = 0
    detected_script: str | None = None
    failure_reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(self.words)

    @property
    def succeeded(self) -> bool:
        return self.failure_reason is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "languages": self.languages,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "raw_text": self.raw_text,
            "words": [item.as_dict() for item in self.words],
            "lines": [item.as_dict() for item in self.lines],
            "mean_confidence": self.mean_confidence,
            "word_count": self.word_count,
            "duration_ms": self.duration_ms,
            "orientation_applied_degrees": self.orientation_applied_degrees,
            "detected_script": self.detected_script,
            "failure_reason": self.failure_reason,
            "warnings": self.warnings,
        }


def classify_script(text: str) -> str:
    """Which script a string is written in."""
    has_devanagari = bool(DEVANAGARI.search(text))
    has_latin = bool(LATIN.search(text))
    if has_devanagari and has_latin:
        return "mixed"
    if has_devanagari:
        return "devanagari"
    if has_latin:
        return "latin"
    return "other"


@lru_cache(maxsize=1)
def engine_version() -> str:
    try:
        return str(pytesseract.get_tesseract_version())
    except Exception:  # noqa: BLE001 - reported through engine_status
        return "unknown"


@lru_cache(maxsize=1)
def available_languages() -> tuple[str, ...]:
    try:
        return tuple(sorted(pytesseract.get_languages(config="")))
    except Exception:  # noqa: BLE001 - reported through engine_status
        return ()


def engine_status() -> dict[str, Any]:
    """Report engine health for the readiness endpoint."""
    settings = get_settings()
    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd

    languages = available_languages()
    version = engine_version()
    missing = [code for code in REQUIRED_LANGUAGES if code not in languages]
    return {
        "available": version != "unknown" and bool(languages),
        "engine": ENGINE_NAME,
        "version": version,
        "languages": list(languages),
        "missing_languages": missing,
        "orientation_detection": "osd" in languages,
    }


def detect_orientation(image: Image.Image) -> int:
    """Suggest a clockwise rotation that would make the text upright.

    Tesseract's orientation model is treated as a *suggestion only*. It is unreliable
    on package panels, where large graphics and mixed scripts routinely produce a
    confident but wrong answer, and a wrong 180-degree rotation turns a readable
    panel into gibberish. :func:`run_ocr` therefore verifies the suggestion by
    reading both orientations and keeping whichever actually reads better.
    """
    if "osd" not in available_languages():
        return 0
    try:
        report = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT)
    except Exception as exc:  # noqa: BLE001 - detection is optional
        logger.debug("orientation_detection_unavailable", error=type(exc).__name__)
        return 0

    rotation = int(report.get("rotate", 0) or 0)
    if rotation % 90 != 0:
        return 0
    return rotation % 360


def _score(outcome: OcrOutcome) -> float:
    """How well a read went, for comparing orientations.

    Word count and mean confidence together: a wrong orientation typically produces
    a similar number of tokens at much lower confidence, or far fewer tokens.
    """
    if not outcome.words:
        return 0.0
    confidence = outcome.mean_confidence or 0.0
    # Words that contain at least one alphanumeric character; upside-down text tends
    # to decode as punctuation and box-drawing noise.
    meaningful = sum(1 for word in outcome.words if any(ch.isalnum() for ch in word.text))
    return meaningful * confidence


def read_with_best_profile(
    image: Image.Image,
    *,
    profile_names: tuple[str, ...] = ("default", "english"),
    correct_orientation: bool = True,
    min_word_confidence: float | None = None,
) -> OcrOutcome:
    """Read an image under several profiles and keep the best result.

    Why this exists: running the Devanagari model over an English-only panel makes
    Latin recognition measurably worse, because the combined model will happily
    classify Latin glyphs as Devanagari or as digits. Running the English-only model
    over a Hindi panel loses the Hindi entirely. Neither configuration is right for
    every package, and Indian packaging is a mixture, so the engine reads both ways
    and keeps whichever produced the better result.

    The profile that won, and the score of each attempt, are recorded in the outcome's
    warnings so the choice is visible rather than hidden.
    """
    attempts: list[tuple[str, OcrOutcome, float]] = []
    for name in profile_names:
        if name not in PROFILES:
            continue
        outcome = run_ocr(
            image,
            profile_name=name,
            # Orientation is resolved once, on the first attempt, and reused.
            correct_orientation=correct_orientation and not attempts,
            min_word_confidence=min_word_confidence,
        )
        if outcome.succeeded:
            attempts.append((name, outcome, _score(outcome)))

    if not attempts:
        return run_ocr(
            image,
            profile_name=profile_names[0],
            correct_orientation=correct_orientation,
            min_word_confidence=min_word_confidence,
        )

    attempts.sort(key=lambda item: item[2], reverse=True)
    winner_name, winner, winner_score = attempts[0]
    winner.warnings = [
        *winner.warnings,
        "Language configuration chosen by comparing reads: "
        + ", ".join(f"{name} scored {score:.1f}" for name, _outcome, score in attempts)
        + f". Used {winner_name}.",
    ]
    return winner


def run_ocr(
    image: Image.Image,
    *,
    profile_name: str = "default",
    correct_orientation: bool = True,
    min_word_confidence: float | None = None,
) -> OcrOutcome:
    """Read text from an image.

    A failure is returned as an ``OcrOutcome`` with ``failure_reason`` set rather
    than raised, so one unreadable face does not abandon an inspection. The caller
    decides what an unreadable image means.
    """
    settings = get_settings()
    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd

    profile = PROFILES.get(profile_name)
    if profile is None:
        raise ProcessingError(f"Unknown OCR profile {profile_name!r}.", code="unknown_ocr_profile")

    threshold = (
        min_word_confidence if min_word_confidence is not None else settings.ocr_min_word_confidence
    )
    warnings: list[str] = []
    started = time.perf_counter()

    missing = [code for code in profile.languages.split("+") if code not in available_languages()]
    if missing:
        return OcrOutcome(
            profile=profile.name,
            languages=profile.languages,
            engine=ENGINE_NAME,
            engine_version=engine_version(),
            raw_text="",
            words=[],
            lines=[],
            mean_confidence=None,
            duration_ms=int((time.perf_counter() - started) * 1000),
            failure_reason=f"OCR language data is missing: {', '.join(missing)}.",
        )

    def read(candidate: Image.Image) -> OcrOutcome | None:
        """Read one image, or None when the engine refused it."""
        try:
            data = pytesseract.image_to_data(
                candidate,
                lang=profile.languages,
                config=profile.config,
                output_type=pytesseract.Output.DICT,
            )
        except pytesseract.TesseractError as exc:
            logger.warning("ocr_engine_error", profile=profile.name, error=str(exc)[:200])
            return None
        except OSError as exc:
            logger.error("ocr_engine_unavailable", error=type(exc).__name__)
            raise ProcessingError(
                "The OCR engine is unavailable.", code="ocr_engine_unavailable"
            ) from exc

        words, lines = _collect(data, threshold)
        return OcrOutcome(
            profile=profile.name,
            languages=profile.languages,
            engine=ENGINE_NAME,
            engine_version=engine_version(),
            raw_text="\n".join(line.text for line in lines),
            words=words,
            lines=lines,
            mean_confidence=(
                float(np.mean([word.confidence for word in words])) if words else None
            ),
            duration_ms=0,
        )

    upright = read(image)
    chosen = upright
    rotation = 0

    if correct_orientation:
        suggested = detect_orientation(image)
        if suggested:
            # Pillow rotates counter-clockwise; Tesseract reports clockwise.
            rotated_image = image.rotate(-suggested, expand=True, fillcolor=(255, 255, 255))
            rotated = read(rotated_image)
            if rotated is not None and (
                upright is None or _score(rotated) > _score(upright) * 1.15
            ):
                # Only accept the rotation when it reads clearly better, so a
                # marginal difference never flips a correctly oriented panel.
                chosen = rotated
                rotation = suggested
                warnings.append(
                    f"Image rotated {suggested} degrees because the text read more "
                    "reliably that way."
                )
            elif rotated is not None:
                warnings.append(
                    f"Orientation detection suggested {suggested} degrees, but the "
                    "image read better as supplied, so it was left unrotated."
                )

    duration_ms = int((time.perf_counter() - started) * 1000)
    OCR_DURATION.observe(duration_ms / 1000.0)

    if chosen is None:
        return OcrOutcome(
            profile=profile.name,
            languages=profile.languages,
            engine=ENGINE_NAME,
            engine_version=engine_version(),
            raw_text="",
            words=[],
            lines=[],
            mean_confidence=None,
            duration_ms=duration_ms,
            orientation_applied_degrees=rotation,
            failure_reason="The OCR engine could not process this image.",
        )

    if not chosen.words:
        warnings.append("No text was recognised in this image.")

    chosen.duration_ms = duration_ms
    chosen.orientation_applied_degrees = rotation
    chosen.detected_script = classify_script(chosen.raw_text) if chosen.raw_text else None
    chosen.warnings = warnings
    return chosen


def _collect(data: dict[str, Any], threshold: float) -> tuple[list[Word], list[Line]]:
    """Turn Tesseract's parallel arrays into words grouped into lines."""
    words: list[Word] = []
    grouping: dict[tuple[int, int, int, int], list[int]] = {}

    texts = data.get("text", [])
    for index in range(len(texts)):
        text = str(texts[index]).strip()
        if not text:
            continue
        try:
            raw_confidence = float(data["conf"][index])
        except (KeyError, TypeError, ValueError):
            continue
        # Tesseract uses -1 for non-text regions.
        confidence = max(0.0, raw_confidence) / 100.0
        if confidence < threshold:
            continue

        try:
            left = int(data["left"][index])
            top = int(data["top"][index])
            width = int(data["width"][index])
            height = int(data["height"][index])
            block = int(data["block_num"][index])
            paragraph = int(data["par_num"][index])
            line = int(data["line_num"][index])
        except (KeyError, TypeError, ValueError):
            continue

        word_index = len(words)
        words.append(
            Word(
                text=text,
                confidence=confidence,
                box=[left, top, left + width, top + height],
                line_index=line,
                block_index=block,
                height_px=float(height),
                script=classify_script(text),
            )
        )
        grouping.setdefault((block, paragraph, line, 0), []).append(word_index)

    lines: list[Line] = []
    for line_index, (_, member_indexes) in enumerate(sorted(grouping.items())):
        members = [words[index] for index in member_indexes]
        text = " ".join(word.text for word in members)
        lines.append(
            Line(
                index=line_index,
                text=text,
                box=[
                    min(word.box[0] for word in members),
                    min(word.box[1] for word in members),
                    max(word.box[2] for word in members),
                    max(word.box[3] for word in members),
                ],
                word_indexes=member_indexes,
                mean_confidence=float(np.mean([word.confidence for word in members])),
                script=classify_script(text),
            )
        )
        for word_position in member_indexes:
            words[word_position].line_index = line_index

    return words, lines


# --------------------------------------------------------------------------
# Barcodes
# --------------------------------------------------------------------------
@dataclass
class BarcodeRead:
    text: str
    symbology: str
    box: list[int] | None
    #: Whether the value passes the GS1 modulo-10 check digit, when applicable.
    check_digit_valid: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "symbology": self.symbology,
            "box": self.box,
            "check_digit_valid": self.check_digit_valid,
        }


def read_barcodes(image: Image.Image) -> list[BarcodeRead]:
    """Read every barcode and QR code in an image.

    A successful read proves an identifier is printed on the package. It is not
    evidence that the package is genuine, and nothing downstream treats it that
    way: the officer confirms an identifier before it is trusted.
    """
    try:
        import zxingcpp
    except ImportError:  # pragma: no cover - dependency is pinned
        logger.warning("barcode_library_unavailable")
        return []

    try:
        results = zxingcpp.read_barcodes(np.asarray(image.convert("RGB")))
    except Exception as exc:  # noqa: BLE001 - a failed read is not a failed upload
        logger.warning("barcode_read_failed", error=type(exc).__name__)
        return []

    from .gtin import validate_check_digit

    reads: list[BarcodeRead] = []
    for result in results:
        text = str(result.text or "").strip()
        if not text:
            continue
        symbology = str(getattr(result, "format", "unknown"))
        box: list[int] | None = None
        position = getattr(result, "position", None)
        if position is not None:
            try:
                xs = [
                    position.top_left.x,
                    position.top_right.x,
                    position.bottom_left.x,
                    position.bottom_right.x,
                ]
                ys = [
                    position.top_left.y,
                    position.top_right.y,
                    position.bottom_left.y,
                    position.bottom_right.y,
                ]
                box = [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]
            except AttributeError:
                box = None

        digits_only = text.isdigit() and len(text) in {8, 12, 13, 14}
        reads.append(
            BarcodeRead(
                text=text,
                symbology=symbology,
                box=box,
                check_digit_valid=validate_check_digit(text) if digits_only else None,
            )
        )
    return reads
