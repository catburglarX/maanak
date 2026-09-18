"""Measure the homepage against the reference specification.

Reports the values the design checklist names explicitly, so bar heights, hero
height, headline size and band alignment are read from the rendered page rather than
inferred from the stylesheet.
"""

from __future__ import annotations

import os

from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://web:8080")

BOXES = [
    ("utility bar", ".utility"),
    ("navigation bar", ".site-header .bar"),
    ("hero", ".home-hero"),
    ("hero photo", ".home-hero .hero-photo"),
    ("evidence strip", ".evidence"),
    ("six-stage row", ".path"),
]

LEFT_EDGES = [
    ("hero eyebrow", ".hero-eyebrow"),
    ("hero headline", ".home-hero h1"),
    ("hero button 1", ".hero-actions .btn"),
    ("standing line", ".standing p"),
    ("evidence heading 1", ".evidence strong"),
    ("method kicker", "#platform .kicker"),
    ("method heading", "#platform h2"),
    ("method column 1", "#platform .method-cols article"),
    ("trace heading", ".trace h2"),
    ("stage 1", ".path li"),
    ("consumer heading", "#public h2"),
    ("governance heading", "#governance-heading"),
    ("footer wordmark", ".site-footer .foot-brand img"),
    ("footer note", ".footer-note p"),
    ("nav logo", ".site-header .brand img"),
    ("utility text", ".utility .container span"),
]

FONTS = [
    ("hero headline", ".home-hero h1"),
    ("hero eyebrow", ".hero-eyebrow"),
    ("hero lede", ".hero-lede"),
    ("method heading", "#platform h2"),
    ("method number", "#platform .number"),
    ("trace heading", ".trace h2"),
    ("evidence heading", ".evidence strong"),
]


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"{BASE_URL}/index.html", wait_until="load")
        page.wait_for_selector(".site-footer", timeout=10000)

        print("=== heights (px) ===")
        for label, selector in BOXES:
            box = page.locator(selector).first.bounding_box()
            if box is None:
                print(f"  {label:<18} MISSING ({selector})")
                continue
            print(f"  {label:<18} height={box['height']:.0f}  width={box['width']:.0f}")

        print()
        print("=== left content edge (px) ===")
        for label, selector in LEFT_EDGES:
            node = page.locator(selector).first
            if node.count() == 0:
                print(f"  {label:<20} MISSING ({selector})")
                continue
            box = node.bounding_box()
            print(f"  {label:<20} x={box['x']:.0f}")

        print()
        print("=== type ===")
        for label, selector in FONTS:
            size = page.locator(selector).first.evaluate(
                "el => {"
                " const s = getComputedStyle(el);"
                " return s.fontSize + ' / ' + s.lineHeight + ' / ' + s.fontFamily.split(',')[0];"
                "}"
            )
            print(f"  {label:<18} {size}")

        print()
        print("=== hero headline lines ===")
        for index in range(3):
            span = page.locator(".home-hero h1 span").nth(index)
            box = span.bounding_box()
            print(f"  line {index + 1}: {span.inner_text()!r} y={box['y']:.0f}")

        print()
        print("=== counts ===")
        for label, selector in [
            ("evidence columns", ".evidence .container > div"),
            ("method columns", "#platform .method-cols article"),
            ("process stages", ".path li"),
            ("footer groups", ".site-footer .cols > div"),
            ("nav links", ".site-nav a"),
            ("wordmarks in header", ".site-header img"),
            ("text beside header logo", ".site-header .brand span"),
        ]:
            print(f"  {label:<24} {page.locator(selector).count()}")

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
