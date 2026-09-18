"""Unit tests for the image quality signals.

These exist because of one photograph. A 679x679 catalogue image of a nutrition panel,
shot against seamless white, was refused with three blocking failures: glare at 54.91 per
cent of the frame against a 3 per cent threshold, bright clipping at 70.8 against 25, and
resolution below the floor. Only the resolution finding was true. The other two were the
white backdrop being counted as a blown highlight, once as glare and again as clipping.

The fix has to hold in both directions, so both directions are asserted here: a backdrop
stops being reported, and a real highlight on the package keeps being reported.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.services.imaging import (
    THRESHOLDS,
    detect_backdrop,
    measure_exposure,
    measure_glare,
    measure_sharpness,
)


def label_patch(width: int, height: int) -> np.ndarray:
    """A grey panel carrying dark text-like marks, so it is neither flat nor clipped."""
    panel = np.full((height, width, 3), 236, dtype=np.uint8)
    for row in range(18, height - 12, 26):
        cv2.rectangle(panel, (14, row), (width - 18, row + 11), (24, 24, 28), -1)
    return panel


def on_white(panel: np.ndarray, frame: int = 679) -> np.ndarray:
    """The panel centred on seamless white, the way a marketplace renders a listing."""
    canvas = np.full((frame, frame, 3), 255, dtype=np.uint8)
    top = (frame - panel.shape[0]) // 2
    left = (frame - panel.shape[1]) // 2
    canvas[top : top + panel.shape[0], left : left + panel.shape[1]] = panel
    return canvas


def with_highlight(panel: np.ndarray) -> np.ndarray:
    """A specular highlight burnt into the middle of the panel."""
    marked = panel.copy()
    height, width = marked.shape[:2]
    cv2.ellipse(
        marked,
        (width // 2, height // 2),
        (width // 4, height // 4),
        0,
        0,
        360,
        (255, 255, 255),
        -1,
    )
    return marked


def grey_of(image: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


class TestBackdropDetection:
    def test_a_studio_backdrop_is_found(self) -> None:
        grey = grey_of(on_white(label_patch(420, 272)))
        _, fraction = detect_backdrop(grey)
        # The panel occupies a small part of a catalogue frame, so most of it is white.
        assert fraction > 0.5, f"backdrop was only {fraction:.1%} of the frame"

    def test_a_full_frame_panel_has_no_backdrop(self) -> None:
        """A photograph that fills the frame must not have part of it discounted."""
        grey = grey_of(label_patch(1200, 780))
        _, fraction = detect_backdrop(grey)
        assert fraction == 0.0

    def test_an_interior_highlight_is_not_a_backdrop(self) -> None:
        """A blown patch in the middle of the panel reaches no edge, so it is glare."""
        grey = grey_of(with_highlight(label_patch(1200, 780)))
        _, fraction = detect_backdrop(grey)
        assert fraction == 0.0


class TestGlareExcludesTheBackdrop:
    def test_a_catalogue_photograph_is_not_reported_as_glare(self) -> None:
        grey = grey_of(on_white(label_patch(420, 272)))
        without, _ = measure_glare(grey)
        backdrop, _ = detect_backdrop(grey)
        with_mask, _ = measure_glare(grey, backdrop)
        assert (
            without >= THRESHOLDS["glare_area_blocking"]
        ), "the test image no longer reproduces the original false positive"
        assert (
            with_mask < THRESHOLDS["glare_area_advisory"]
        ), f"backdrop still counted as glare: {with_mask:.2%}"

    def test_a_real_highlight_still_blocks(self) -> None:
        grey = grey_of(with_highlight(label_patch(1200, 780)))
        backdrop, _ = detect_backdrop(grey)
        area, largest = measure_glare(grey, backdrop)
        assert (
            area >= THRESHOLDS["glare_area_blocking"]
        ), f"a burnt-out patch on the panel was not reported: {area:.2%}"
        assert largest > 0


class TestExposureExcludesTheBackdrop:
    def test_clipping_is_measured_on_the_package(self) -> None:
        grey = grey_of(on_white(label_patch(420, 272)))
        backdrop, _ = detect_backdrop(grey)
        before = measure_exposure(grey)
        after = measure_exposure(grey, backdrop)
        assert before[3] >= THRESHOLDS["bright_clipped_blocking"]
        assert (
            after[3] < THRESHOLDS["bright_clipped_blocking"]
        ), f"backdrop still counted as clipping: {after[3]:.1%}"

    def test_mean_luminance_describes_the_package(self) -> None:
        """A backdrop drags the mean toward white and hides the real exposure."""
        grey = grey_of(on_white(label_patch(420, 272)))
        backdrop, _ = detect_backdrop(grey)
        assert measure_exposure(grey, backdrop)[0] < measure_exposure(grey)[0]

    def test_a_dark_photograph_is_still_dark(self) -> None:
        panel = (label_patch(1200, 780) * 0.12).astype(np.uint8)
        grey = grey_of(panel)
        backdrop, _ = detect_backdrop(grey)
        mean = measure_exposure(grey, backdrop)[0]
        assert mean < THRESHOLDS["brightness_floor"], f"mean was {mean:.1f}"


class TestSharpnessUnaffected:
    @pytest.mark.parametrize("blur", [0, 9, 21])
    def test_blurring_lowers_the_measurement(self, blur: int) -> None:
        panel = label_patch(1200, 780)
        if blur:
            panel = cv2.GaussianBlur(panel, (blur, blur), 0)
        value = measure_sharpness(grey_of(panel))
        assert value >= 0
        if blur >= 21:
            assert value < THRESHOLDS["sharpness_blocking"]
