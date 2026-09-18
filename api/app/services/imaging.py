"""Image validation, quality analysis and derivative generation.

Design decision: there is no single opaque "quality score". Each measurement is
reported separately with the number, the threshold it was compared against, and a
sentence telling the officer what to do. A combined verdict exists, but it is a
rule over the individual signals rather than a weighted sum, so it can be
explained and argued with.

Thresholds are defined in ``THRESHOLDS`` with the reasoning for each value. They were
chosen from measurement on sample images rather than guessed, they are recorded
alongside every measurement so a reviewer can see what a value was compared against,
and every one of them is overridable per deployment. ``scripts/verify_imaging.py``
exercises them against generated images covering each failure mode.
"""

from __future__ import annotations

import io
import math
from dataclasses import asdict, dataclass, field
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError
from PIL.ExifTags import TAGS

from ..config import get_settings
from ..errors import PayloadTooLargeError, ProcessingError, UnsupportedMediaTypeError

# Pillow's own guard against decompression bombs. Set explicitly rather than left
# at the default so the limit is visible and testable.
Image.MAX_IMAGE_PIXELS = 64_000_000

ACCEPTED_MIME_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

#: Magic bytes checked against the declared type. A client-supplied Content-Type is
#: never trusted.
MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)

ANALYSIS_VERSION = "quality-1.0"


class Severity:
    OK = "ok"
    ADVISORY = "advisory"
    BLOCKING = "blocking"


class Verdict:
    ACCEPTABLE = "acceptable"
    REVIEW_RECOMMENDED = "review_recommended"
    RECAPTURE_RECOMMENDED = "recapture_recommended"


THRESHOLDS: dict[str, Any] = {
    # Variance of the Laplacian, scaled to a 1000px-wide image. Below 40 printed
    # text on a package is not reliably separable from the background in our sample
    # set; 40-110 is readable but degrades OCR.
    "sharpness_blocking": 40.0,
    "sharpness_advisory": 110.0,
    # Glare is measured as *clipped* pixels (>= 254), not merely bright ones.
    # This distinction matters: a white atta or salt pouch photographed correctly
    # sits around 235-250 and carries detail, while a specular highlight clips to
    # 255 and carries none. An earlier revision used >= 250 and flagged every white
    # package as glare, which would have made the check useless on a large share of
    # Indian packaging.
    "glare_area_blocking": 0.030,
    "glare_area_advisory": 0.010,
    # Exposure is judged by information loss rather than average brightness, for the
    # same reason: the mean of a white pouch is high even when correctly exposed.
    # Blocking conditions are clipping-based; the mean only raises an advisory.
    "dark_clipped_blocking": 0.35,
    "bright_clipped_blocking": 0.25,
    "brightness_floor": 45.0,
    "brightness_advisory_min": 70.0,
    "brightness_advisory_max": 232.0,
    # Standard deviation of luminance. Under 18 the image is flat and OCR
    # thresholding becomes unstable.
    "contrast_blocking": 18.0,
    "contrast_advisory": 30.0,
    # Total pixels. 480_000 is roughly 800x600: below this, 1mm print at typical
    # framing falls under the ~10px cap-height that Tesseract needs.
    "pixels_blocking": 480_000,
    "pixels_advisory": 1_200_000,
    # Estimated median glyph height in pixels. Tesseract's documented comfort
    # zone starts around 20px cap height; 10px is the practical floor.
    "glyph_height_blocking": 9.0,
    "glyph_height_advisory": 16.0,
    # Absolute text skew in degrees. Beyond 12 degrees line grouping degrades even
    # after deskewing.
    "skew_blocking": 12.0,
    "skew_advisory": 4.0,
    # Fraction of the border occupied by high-contrast content, suggesting the
    # panel continues outside the frame.
    "edge_content_blocking": 0.30,
    "edge_content_advisory": 0.15,
}

#: Pixel value at which a sensor is considered clipped. 254 rather than 255 because
#: JPEG quantisation moves a genuinely blown pixel by a level or two.
CLIPPING_LEVEL = 254


@dataclass
class Signal:
    """One measurement and what it means for the officer."""

    name: str
    severity: str
    #: The measured number, or None when the measurement could not be taken.
    value: float | None
    threshold: float | None
    #: Plain sentence. Written as an instruction when action is needed.
    message: str
    unit: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecodedImage:
    """A validated image plus the facts needed for the custody record."""

    pillow: Image.Image
    grey: np.ndarray
    width: int
    height: int
    detected_mime: str
    extension: str
    exif: dict[str, Any] = field(default_factory=dict)
    orientation_corrected: bool = False

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass
class QualityReport:
    verdict: str
    signals: list[Signal]
    measurements: dict[str, Any]
    analysis_version: str = ANALYSIS_VERSION

    @property
    def blocking(self) -> list[Signal]:
        return [item for item in self.signals if item.severity == Severity.BLOCKING]

    @property
    def advisories(self) -> list[Signal]:
        return [item for item in self.signals if item.severity == Severity.ADVISORY]

    @property
    def requires_recapture(self) -> bool:
        return self.verdict == Verdict.RECAPTURE_RECOMMENDED

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "analysis_version": self.analysis_version,
            "signals": [item.as_dict() for item in self.signals],
            "measurements": self.measurements,
            "actions": [
                item.message
                for item in self.signals
                if item.severity in {Severity.BLOCKING, Severity.ADVISORY}
            ],
        }


# --------------------------------------------------------------------------
# Decoding and validation
# --------------------------------------------------------------------------
def sniff_mime_type(data: bytes) -> str | None:
    for signature, mime in MAGIC_SIGNATURES:
        if data.startswith(signature):
            return mime
    # WebP: "RIFF" .... "WEBP"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def decode_image(data: bytes, *, declared_mime: str | None = None) -> DecodedImage:
    """Validate and decode an upload.

    Order matters: size first (cheapest), then magic bytes, then a structural
    verify, then a full decode. A file that lies about its type never reaches the
    decoder for the type it claimed.
    """
    settings = get_settings()

    if len(data) > settings.max_upload_bytes:
        raise PayloadTooLargeError(
            f"The image is larger than the {settings.max_upload_mb} MB limit.",
            details={"limit_bytes": settings.max_upload_bytes, "size_bytes": len(data)},
        )
    if len(data) < 1024:
        raise UnsupportedMediaTypeError(
            "That file is too small to be a photograph.", code="file_too_small"
        )

    detected = sniff_mime_type(data)
    if detected is None:
        raise UnsupportedMediaTypeError(
            "Upload a JPEG, PNG or WebP photograph.", code="unrecognised_image"
        )
    if declared_mime and declared_mime.split(";")[0].strip() != detected:
        raise UnsupportedMediaTypeError(
            "The file contents do not match the declared file type.",
            code="mime_mismatch",
            details={"declared": declared_mime, "detected": detected},
        )

    # Structural check on a throwaway handle: verify() leaves the object unusable.
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UnsupportedMediaTypeError(
            "That image file is damaged or incomplete.", code="corrupt_image"
        ) from exc

    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > settings.ocr_max_pixels:
                raise PayloadTooLargeError(
                    "The image resolution is larger than this system accepts.",
                    code="resolution_too_large",
                    details={
                        "pixels": source.width * source.height,
                        "limit_pixels": settings.ocr_max_pixels,
                    },
                )
            exif = _read_exif(source)
            original_orientation = exif.get("Orientation")
            # Apply the EXIF rotation so analysis and OCR see the upright image.
            # The stored original keeps its bytes and its EXIF untouched.
            # exif_transpose returns None when the tag cannot be read.
            upright = ImageOps.exif_transpose(source) or source
            rgb = upright.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise UnsupportedMediaTypeError(
            "That image could not be read.", code="undecodable_image"
        ) from exc

    array = np.asarray(rgb)
    grey = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)

    return DecodedImage(
        pillow=rgb,
        grey=grey,
        width=rgb.width,
        height=rgb.height,
        detected_mime=detected,
        extension=ACCEPTED_MIME_TYPES[detected],
        exif=exif,
        orientation_corrected=bool(original_orientation and original_orientation != 1),
    )


def _read_exif(image: Image.Image) -> dict[str, Any]:
    """Extract a small, safe subset of EXIF.

    Only fields with an operational use are kept. GPS is deliberately excluded:
    location is recorded from an explicit officer action, not silently from a
    photograph's metadata.
    """
    wanted = {
        "Make",
        "Model",
        "DateTimeOriginal",
        "Orientation",
        "ExifImageWidth",
        "ExifImageHeight",
        "FNumber",
        "ExposureTime",
        "ISOSpeedRatings",
        "Flash",
        "FocalLength",
        "Software",
    }
    result: dict[str, Any] = {}
    try:
        raw = image.getexif()
    except Exception:  # noqa: BLE001 - malformed EXIF must not fail an upload
        return result
    if not raw:
        return result
    for tag_id, value in raw.items():
        name = TAGS.get(tag_id, str(tag_id))
        if name not in wanted:
            continue
        if isinstance(value, bytes):
            continue
        try:
            result[name] = float(value) if isinstance(value, int | float) else str(value)[:120]
        except (TypeError, ValueError):
            continue
    return result


# --------------------------------------------------------------------------
# Measurements
# --------------------------------------------------------------------------
def _scaled_grey(grey: np.ndarray, target_width: int = 1000) -> np.ndarray:
    """Resize to a fixed width so measurements are resolution-independent.

    Without this, the same photograph at 2x resolution produces a different
    Laplacian variance and the thresholds stop meaning anything.
    """
    height, width = grey.shape[:2]
    if width == target_width:
        return grey
    scale = target_width / float(width)
    return cv2.resize(
        grey,
        (target_width, max(1, int(round(height * scale)))),
        interpolation=cv2.INTER_AREA,
    )


def measure_sharpness(grey: np.ndarray) -> float:
    normalised = _scaled_grey(grey)
    return float(cv2.Laplacian(normalised, cv2.CV_64F).var())


def measure_glare(grey: np.ndarray) -> tuple[float, float]:
    """Return ``(glare_area_fraction, largest_patch_fraction)``.

    Glare is a *clipped* region, not a bright one. Two conditions must both hold:

    * the pixels are at or above :data:`CLIPPING_LEVEL`, so tone information is gone;
    * they form a connected region of meaningful size.

    Counting merely bright pixels would flag every white pouch, which is why the
    threshold sits at clipping level and why the region is also required to be
    locally flat.
    """
    normalised = _scaled_grey(grey)
    total = float(normalised.size)
    mask = (normalised >= CLIPPING_LEVEL).astype(np.uint8)
    if not mask.any():
        return 0.0, 0.0

    # Close small gaps so a broken highlight counts as one region. cv2 widens the
    # dtype, so the result takes a new name rather than reassigning the uint8 mask.
    kernel = np.ones((3, 3), np.uint8)
    closed: Any = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    # Require local flatness: printed white paper retains fibre and print texture,
    # a blown highlight does not.
    as_float = normalised.astype(np.float32)
    blurred: Any = cv2.blur(as_float, (9, 9))
    deviation: Any = (as_float - blurred) ** 2
    local_variance: Any = cv2.blur(deviation.astype(np.float32), (9, 9))
    flat_mask: Any = (closed.astype(bool) & (local_variance < 4.0)).astype(np.uint8)
    if not flat_mask.any():
        return 0.0, 0.0

    count, _, stats, _ = cv2.connectedComponentsWithStats(flat_mask, connectivity=8)
    minimum_area = total * 0.0015
    areas = [
        float(stats[index, cv2.CC_STAT_AREA])
        for index in range(1, count)
        if stats[index, cv2.CC_STAT_AREA] >= minimum_area
    ]
    if not areas:
        return 0.0, 0.0
    return sum(areas) / total, max(areas) / total


def measure_exposure(grey: np.ndarray) -> tuple[float, float, float, float]:
    """Return ``(mean, std, clipped_dark_fraction, clipped_bright_fraction)``."""
    values = grey.astype(np.float32)
    total = float(values.size)
    return (
        float(values.mean()),
        float(values.std()),
        float((values <= 2).sum() / total),
        float((values >= CLIPPING_LEVEL).sum() / total),
    )


def estimate_glyph_height(grey: np.ndarray) -> tuple[float | None, int]:
    """Median height of text-like connected components, in original pixels.

    Uses adaptive thresholding then filters components by aspect ratio, area and
    fill so that borders, images and specks are excluded. Returns
    ``(median_height, component_count)``; the height is None when too few
    components look like text to draw a conclusion.
    """
    height, width = grey.shape[:2]
    working = grey
    scale = 1.0
    # Cap the working size for speed; scale the result back afterwards.
    if width > 1600:
        scale = 1600.0 / width
        working = cv2.resize(
            grey, (1600, max(1, int(round(height * scale)))), interpolation=cv2.INTER_AREA
        )

    binary = cv2.adaptiveThreshold(
        cv2.GaussianBlur(working, (3, 3), 0),
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        31,
        10,
    )
    count, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)

    heights: list[float] = []
    working_height, working_width = binary.shape[:2]
    for index in range(1, count):
        component_width = float(stats[index, cv2.CC_STAT_WIDTH])
        component_height = float(stats[index, cv2.CC_STAT_HEIGHT])
        area = float(stats[index, cv2.CC_STAT_AREA])
        if component_height < 5 or component_height > working_height * 0.25:
            continue
        if component_width < 2 or component_width > working_width * 0.25:
            continue
        aspect = component_width / component_height
        if not 0.08 <= aspect <= 4.0:
            continue
        fill = area / (component_width * component_height)
        if not 0.12 <= fill <= 0.95:
            continue
        heights.append(component_height)

    if len(heights) < 12:
        return None, len(heights)
    median = float(np.median(heights))
    return median / scale, len(heights)


def estimate_skew(grey: np.ndarray) -> float | None:
    """Dominant text baseline angle in degrees, or None when undetectable.

    Horizontal projection of dilated text rows via ``minAreaRect`` is stable on
    package labels, where full-page Hough transforms tend to lock onto package
    edges instead of text.
    """
    normalised = _scaled_grey(grey, 1200)
    binary = cv2.adaptiveThreshold(
        cv2.GaussianBlur(normalised, (3, 3), 0),
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        31,
        10,
    )
    # Join characters into line-shaped blobs.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 3))
    lines = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    angles: list[float] = []
    weights: list[float] = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < 400:
            continue
        (_, _), (rect_width, rect_height), angle = cv2.minAreaRect(contour)
        if min(rect_width, rect_height) < 4:
            continue
        if rect_width < rect_height:
            angle += 90.0
        # Normalise to [-45, 45]: a label line is never meaningfully "80 degrees".
        while angle > 45:
            angle -= 90
        while angle < -45:
            angle += 90
        if abs(angle) > 45:
            continue
        angles.append(angle)
        weights.append(area)

    if len(angles) < 3:
        return None
    return float(np.average(angles, weights=weights))


def measure_edge_content(grey: np.ndarray) -> float:
    """Fraction of the border band carrying strong edges.

    High values mean printed content runs off the frame, so the panel is cut off.
    """
    normalised = _scaled_grey(grey)
    edges = cv2.Canny(normalised, 60, 180)
    height, width = edges.shape[:2]
    band = max(2, int(round(min(height, width) * 0.02)))
    border = np.concatenate(
        [
            edges[:band, :].ravel(),
            edges[-band:, :].ravel(),
            edges[:, :band].ravel(),
            edges[:, -band:].ravel(),
        ]
    )
    if border.size == 0:
        return 0.0
    return float((border > 0).sum() / border.size)


def looks_like_screenshot(decoded: DecodedImage) -> tuple[bool, str]:
    """Heuristic: is this a screen capture rather than a photograph?

    Reported as an advisory only. A screenshot of a listing is legitimate evidence
    for an e-commerce inspection but is not a photograph of a physical package, and
    the record should say which it is.
    """
    reasons: list[str] = []
    if not decoded.exif:
        reasons.append("no camera metadata")
    elif not decoded.exif.get("Make") and not decoded.exif.get("Model"):
        reasons.append("no camera make or model")

    software = str(decoded.exif.get("Software", "")).lower()
    if any(token in software for token in ("screenshot", "snipping", "shot")):
        reasons.append("capture software recorded in metadata")

    common_screen_sizes = {
        (1080, 1920),
        (1920, 1080),
        (1170, 2532),
        (1284, 2778),
        (828, 1792),
        (1440, 2560),
        (750, 1334),
        (1080, 2340),
        (1080, 2400),
        (1179, 2556),
        (1290, 2796),
        (2560, 1440),
        (3840, 2160),
        (1366, 768),
        (1536, 2048),
    }
    if (decoded.width, decoded.height) in common_screen_sizes:
        reasons.append("dimensions match a common screen resolution exactly")

    # A photograph almost always carries sensor noise; a screenshot is clean.
    if decoded.pixels > 200_000:
        smooth = cv2.GaussianBlur(decoded.grey, (3, 3), 0)
        noise = float(np.abs(decoded.grey.astype(np.float32) - smooth.astype(np.float32)).mean())
        if noise < 0.6:
            reasons.append("no sensor noise detected")

    # Two independent indications, so a stripped-EXIF photograph is not flagged.
    return (len(reasons) >= 2, "; ".join(reasons))


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------
def analyse_quality(decoded: DecodedImage) -> QualityReport:
    """Run every measurement and produce a verdict with actionable reasons."""
    thresholds = THRESHOLDS
    signals: list[Signal] = []

    sharpness = measure_sharpness(decoded.grey)
    if sharpness < thresholds["sharpness_blocking"]:
        signals.append(
            Signal(
                "sharpness",
                Severity.BLOCKING,
                round(sharpness, 1),
                thresholds["sharpness_blocking"],
                "The image is too blurred to read. Hold the camera steady, tap to "
                "focus on the printed panel, and take another photograph.",
            )
        )
    elif sharpness < thresholds["sharpness_advisory"]:
        signals.append(
            Signal(
                "sharpness",
                Severity.ADVISORY,
                round(sharpness, 1),
                thresholds["sharpness_advisory"],
                "The image is soft. A sharper photograph will read more reliably.",
            )
        )
    else:
        signals.append(
            Signal("sharpness", Severity.OK, round(sharpness, 1), None, "The image is sharp.")
        )

    glare_area, largest_patch = measure_glare(decoded.grey)
    if glare_area >= thresholds["glare_area_blocking"]:
        signals.append(
            Signal(
                "glare",
                Severity.BLOCKING,
                round(glare_area * 100, 2),
                thresholds["glare_area_blocking"] * 100,
                "Strong glare is covering part of the panel. Move the light source or "
                "tilt the package slightly and photograph it again.",
                unit="% of frame",
            )
        )
    elif glare_area >= thresholds["glare_area_advisory"]:
        signals.append(
            Signal(
                "glare",
                Severity.ADVISORY,
                round(glare_area * 100, 2),
                thresholds["glare_area_advisory"] * 100,
                "There is some glare on the package. Check that no declaration sits "
                "under a bright patch.",
                unit="% of frame",
            )
        )
    else:
        signals.append(
            Signal(
                "glare",
                Severity.OK,
                round(glare_area * 100, 2),
                None,
                "No significant glare.",
                unit="% of frame",
            )
        )

    mean, deviation, dark_clip, bright_clip = measure_exposure(decoded.grey)
    if dark_clip >= thresholds["dark_clipped_blocking"] or mean < thresholds["brightness_floor"]:
        signals.append(
            Signal(
                "brightness",
                Severity.BLOCKING,
                round(mean, 1),
                thresholds["brightness_floor"],
                "The photograph is too dark and detail is lost in shadow. Add light or "
                "move to a brighter place and photograph the panel again.",
            )
        )
    elif bright_clip >= thresholds["bright_clipped_blocking"]:
        signals.append(
            Signal(
                "brightness",
                Severity.BLOCKING,
                round(bright_clip * 100, 1),
                thresholds["bright_clipped_blocking"] * 100,
                "Much of the frame is burnt out, so printed detail is lost. Reduce "
                "direct light and photograph the panel again.",
                unit="% clipped",
            )
        )
    elif not (
        thresholds["brightness_advisory_min"] <= mean <= thresholds["brightness_advisory_max"]
    ):
        signals.append(
            Signal(
                "brightness",
                Severity.ADVISORY,
                round(mean, 1),
                None,
                "Lighting is uneven or the panel is very light or very dark overall. "
                "More even light will read more reliably.",
            )
        )
    else:
        signals.append(
            Signal("brightness", Severity.OK, round(mean, 1), None, "Lighting is suitable.")
        )

    if deviation < thresholds["contrast_blocking"]:
        signals.append(
            Signal(
                "contrast",
                Severity.BLOCKING,
                round(deviation, 1),
                thresholds["contrast_blocking"],
                "There is too little contrast between the print and the packaging. "
                "Photograph the panel straight on with even light.",
            )
        )
    elif deviation < thresholds["contrast_advisory"]:
        signals.append(
            Signal(
                "contrast",
                Severity.ADVISORY,
                round(deviation, 1),
                thresholds["contrast_advisory"],
                "Contrast is low, which can hide thin print.",
            )
        )
    else:
        signals.append(
            Signal("contrast", Severity.OK, round(deviation, 1), None, "Contrast is adequate.")
        )

    if decoded.pixels < thresholds["pixels_blocking"]:
        signals.append(
            Signal(
                "resolution",
                Severity.BLOCKING,
                float(decoded.pixels),
                float(thresholds["pixels_blocking"]),
                f"The image is only {decoded.width}x{decoded.height}. Photograph the "
                "panel closer, or at a higher camera setting.",
                unit="pixels",
            )
        )
    elif decoded.pixels < thresholds["pixels_advisory"]:
        signals.append(
            Signal(
                "resolution",
                Severity.ADVISORY,
                float(decoded.pixels),
                float(thresholds["pixels_advisory"]),
                "Resolution is modest. Move closer to the declaration panel for small print.",
                unit="pixels",
            )
        )
    else:
        signals.append(
            Signal(
                "resolution",
                Severity.OK,
                float(decoded.pixels),
                None,
                f"Resolution is {decoded.width}x{decoded.height}.",
                unit="pixels",
            )
        )

    glyph_height, component_count = estimate_glyph_height(decoded.grey)
    if glyph_height is None:
        signals.append(
            Signal(
                "text_size",
                Severity.ADVISORY,
                None,
                None,
                "No printed text was located in this image. Check that the "
                "declaration panel is inside the frame.",
            )
        )
    elif glyph_height < thresholds["glyph_height_blocking"]:
        signals.append(
            Signal(
                "text_size",
                Severity.BLOCKING,
                round(glyph_height, 1),
                thresholds["glyph_height_blocking"],
                "The text is too small to read reliably. Take a closer photograph of "
                "the declaration panel on its own.",
                unit="px",
            )
        )
    elif glyph_height < thresholds["glyph_height_advisory"]:
        signals.append(
            Signal(
                "text_size",
                Severity.ADVISORY,
                round(glyph_height, 1),
                thresholds["glyph_height_advisory"],
                "The text is small. A closer photograph will read more reliably.",
                unit="px",
            )
        )
    else:
        signals.append(
            Signal(
                "text_size",
                Severity.OK,
                round(glyph_height, 1),
                None,
                "Text is large enough to read.",
                unit="px",
            )
        )

    skew = estimate_skew(decoded.grey)
    if skew is None:
        signals.append(
            Signal("skew", Severity.OK, None, None, "Text alignment could not be measured.")
        )
    elif abs(skew) >= thresholds["skew_blocking"]:
        signals.append(
            Signal(
                "skew",
                Severity.BLOCKING,
                round(skew, 1),
                thresholds["skew_blocking"],
                f"The text runs at about {abs(skew):.0f} degrees. Hold the camera "
                "parallel to the panel and photograph it again.",
                unit="degrees",
            )
        )
    elif abs(skew) >= thresholds["skew_advisory"]:
        signals.append(
            Signal(
                "skew",
                Severity.ADVISORY,
                round(skew, 1),
                thresholds["skew_advisory"],
                "The image is slightly tilted. It will be straightened before reading.",
                unit="degrees",
            )
        )
    else:
        signals.append(
            Signal(
                "skew",
                Severity.OK,
                round(skew, 1),
                None,
                "The panel is square to the camera.",
                unit="degrees",
            )
        )

    edge_content = measure_edge_content(decoded.grey)
    if edge_content >= thresholds["edge_content_blocking"]:
        signals.append(
            Signal(
                "framing",
                Severity.BLOCKING,
                round(edge_content * 100, 1),
                thresholds["edge_content_blocking"] * 100,
                "Printed content runs off the edge of the frame, so the panel is cut "
                "off. Step back and include the whole panel.",
                unit="% of border",
            )
        )
    elif edge_content >= thresholds["edge_content_advisory"]:
        signals.append(
            Signal(
                "framing",
                Severity.ADVISORY,
                round(edge_content * 100, 1),
                thresholds["edge_content_advisory"] * 100,
                "Content reaches the edge of the frame. Check that nothing is cut off.",
                unit="% of border",
            )
        )
    else:
        signals.append(
            Signal(
                "framing",
                Severity.OK,
                round(edge_content * 100, 1),
                None,
                "The panel is fully inside the frame.",
                unit="% of border",
            )
        )

    if bright_clip > 0.12:
        signals.append(
            Signal(
                "highlight_clipping",
                Severity.ADVISORY,
                round(bright_clip * 100, 1),
                12.0,
                "Bright areas are washed out. Print inside them may be unreadable.",
                unit="% of frame",
            )
        )
    if dark_clip > 0.12:
        signals.append(
            Signal(
                "shadow_clipping",
                Severity.ADVISORY,
                round(dark_clip * 100, 1),
                12.0,
                "Dark areas are crushed to black. Print inside them may be unreadable.",
                unit="% of frame",
            )
        )

    is_screenshot, screenshot_reason = looks_like_screenshot(decoded)
    if is_screenshot:
        signals.append(
            Signal(
                "capture_source",
                Severity.ADVISORY,
                None,
                None,
                "This looks like a screen capture rather than a photograph of the "
                "package. Record it as listing evidence, and photograph the physical "
                f"package separately. ({screenshot_reason})",
            )
        )

    verdict = Verdict.ACCEPTABLE
    if any(item.severity == Severity.BLOCKING for item in signals):
        verdict = Verdict.RECAPTURE_RECOMMENDED
    elif sum(1 for item in signals if item.severity == Severity.ADVISORY) >= 2:
        verdict = Verdict.REVIEW_RECOMMENDED

    measurements = {
        "sharpness_laplacian_variance": round(sharpness, 2),
        "glare_area_fraction": round(glare_area, 5),
        "glare_largest_patch_fraction": round(largest_patch, 5),
        "brightness_mean": round(mean, 2),
        "contrast_std": round(deviation, 2),
        "dark_clipped_fraction": round(dark_clip, 5),
        "bright_clipped_fraction": round(bright_clip, 5),
        "width": decoded.width,
        "height": decoded.height,
        "pixels": decoded.pixels,
        "estimated_glyph_height_px": round(glyph_height, 2) if glyph_height else None,
        "text_component_count": component_count,
        "skew_degrees": round(skew, 2) if skew is not None else None,
        "edge_content_fraction": round(edge_content, 4),
        "looks_like_screenshot": is_screenshot,
        "orientation_corrected": decoded.orientation_corrected,
    }
    return QualityReport(verdict=verdict, signals=signals, measurements=measurements)


# --------------------------------------------------------------------------
# Derivatives
# --------------------------------------------------------------------------
def make_thumbnail(decoded: DecodedImage, *, max_edge: int = 480) -> tuple[bytes, int, int]:
    """Small JPEG for lists. EXIF is dropped."""
    image = decoded.pillow.copy()
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=82, optimize=True)
    return buffer.getvalue(), image.width, image.height


def make_ocr_input(
    decoded: DecodedImage, *, target_min_edge: int = 1600, deskew: bool = True
) -> tuple[bytes, int, int, dict[str, Any]]:
    """Prepare an image for OCR.

    Upscaling small images helps Tesseract; deskewing is applied only when a
    meaningful angle was measured. The transform is returned so the derivative can
    be reproduced and so coordinates can be mapped back to the original.
    """
    image = decoded.pillow.copy()
    transform: dict[str, Any] = {"operations": []}

    angle = estimate_skew(decoded.grey) if deskew else None
    if angle is not None and 0.5 <= abs(angle) <= 20.0:
        image = image.rotate(
            angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(255, 255, 255)
        )
        transform["operations"].append({"rotate_degrees": round(angle, 3)})

    scale = 1.0
    shortest = min(image.width, image.height)
    if shortest < target_min_edge:
        scale = min(3.0, target_min_edge / float(shortest))
        image = image.resize(
            (int(round(image.width * scale)), int(round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
        transform["operations"].append({"scale": round(scale, 4)})

    # Mild local contrast improvement. CLAHE is used rather than a global stretch
    # because package labels frequently mix a bright panel with a dark background.
    grey = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(grey)
    transform["operations"].append({"clahe": {"clip_limit": 2.0, "tile_grid": [8, 8]}})

    output = Image.fromarray(enhanced)
    buffer = io.BytesIO()
    output.save(buffer, format="PNG", optimize=True)
    transform["scale_applied"] = round(scale, 4)
    transform["rotation_applied"] = round(angle, 3) if angle is not None else 0.0
    return buffer.getvalue(), output.width, output.height, transform


def map_region_to_original(
    box: list[float], *, transform: dict[str, Any], original_size: tuple[int, int]
) -> list[int]:
    """Map an OCR-input box back to original-image pixel coordinates.

    Rotation is inverted about the rotated image centre, then the scale is undone.
    Approximate at the pixel level, which is all a highlight overlay needs.
    """
    scale = float(transform.get("scale_applied") or 1.0)
    rotation = float(transform.get("rotation_applied") or 0.0)
    x1, y1, x2, y2 = (float(value) for value in box)

    if scale and scale != 1.0:
        x1, y1, x2, y2 = x1 / scale, y1 / scale, x2 / scale, y2 / scale

    if rotation:
        width, height = original_size
        radians = math.radians(-rotation)
        cosine, sine = math.cos(radians), math.sin(radians)
        centre_x, centre_y = width / 2.0, height / 2.0
        corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        rotated = []
        for point_x, point_y in corners:
            offset_x, offset_y = point_x - centre_x, point_y - centre_y
            rotated.append(
                (
                    centre_x + offset_x * cosine - offset_y * sine,
                    centre_y + offset_x * sine + offset_y * cosine,
                )
            )
        xs = [point[0] for point in rotated]
        ys = [point[1] for point in rotated]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)

    width, height = original_size
    return [
        max(0, min(width, int(round(x1)))),
        max(0, min(height, int(round(y1)))),
        max(0, min(width, int(round(x2)))),
        max(0, min(height, int(round(y2)))),
    ]


def annotate_regions(
    decoded: DecodedImage, regions: list[dict[str, Any]]
) -> tuple[bytes, int, int]:
    """Draw labelled boxes for the report and the review screen."""
    from PIL import ImageDraw

    image = decoded.pillow.copy()
    draw = ImageDraw.Draw(image)
    stroke = max(2, int(round(min(image.width, image.height) / 400)))

    for region in regions:
        box = region.get("box") or []
        if len(box) != 4:
            continue
        colour = region.get("colour", "#B3282D")
        draw.rectangle([box[0], box[1], box[2], box[3]], outline=colour, width=stroke)
        label = str(region.get("label", ""))
        if label:
            draw.text((box[0] + stroke + 2, max(0, box[1] - 14)), label, fill=colour)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    return buffer.getvalue(), image.width, image.height


def crop_region(decoded: DecodedImage, box: list[int], *, padding: int = 8) -> bytes:
    """Crop one declaration region, used in the report as printed evidence."""
    width, height = decoded.width, decoded.height
    x1 = max(0, int(box[0]) - padding)
    y1 = max(0, int(box[1]) - padding)
    x2 = min(width, int(box[2]) + padding)
    y2 = min(height, int(box[3]) + padding)
    if x2 <= x1 or y2 <= y1:
        raise ProcessingError("The requested region is outside the image.", code="invalid_region")
    cropped = decoded.pillow.crop((x1, y1, x2, y2))
    buffer = io.BytesIO()
    cropped.save(buffer, format="JPEG", quality=90, optimize=True)
    return buffer.getvalue()
