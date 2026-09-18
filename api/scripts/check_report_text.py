"""Download the generated report documents and check them for banned characters.

The PDF and the DOCX are rendered from a frozen snapshot, and that snapshot is already
checked in the database. This checks the rendered output itself, which is what a reader
actually receives.

    docker compose run --rm --no-deps -v "$PWD/api:/app" \
      -e API_URL=http://api:8000 --entrypoint python api scripts/check_report_text.py
"""

from __future__ import annotations

import os
import re
import sys
import zipfile
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.session_client import RefreshingClient

API_URL = os.environ.get("API_URL", "http://api:8000")
EMAIL = os.environ.get("REVIEWER_EMAIL", "reviewer@example.org")
PASSWORD = os.environ.get("DEMO_PASSWORD", "Ganga-Yamuna-2026")

EM_DASH = "\u2014"
#: Characters and strings that must never reach a reader.
BANNED = {
    "em dash": EM_DASH,
    "parenthesised plural": "(s)",
    "stringified object": "[object Object]",
    "raw enum": "_found",
}


def pdf_text(data: bytes) -> str:
    """Pull the text out of a PDF without a parser dependency.

    ReportLab writes text as literal strings inside Tj and TJ operators in content
    streams, which are Flate-compressed. Inflating every stream and taking the
    parenthesised literals is enough to search for a character.
    """
    import zlib

    out: list[str] = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.DOTALL):
        chunk = match.group(1)
        try:
            chunk = zlib.decompress(chunk)
        except zlib.error:
            continue
        for literal in re.findall(rb"\(((?:[^()\\]|\\.)*)\)", chunk):
            out.append(literal.decode("latin-1", errors="replace"))
    return " ".join(out)


def docx_text(data: bytes) -> str:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        parts = [
            archive.read(name).decode("utf-8", errors="replace")
            for name in archive.namelist()
            if name.endswith(".xml")
        ]
    return " ".join(re.sub(r"<[^>]+>", " ", part) for part in parts)


def main() -> int:
    failures = 0
    with RefreshingClient(base_url=API_URL, timeout=60) as client:
        response = client.post("/api/v1/auth/sign-in", json={"email": EMAIL, "password": PASSWORD})
        if response.status_code != 200:
            print(f"  FAIL  could not sign in as {EMAIL}: {response.text[:200]}")
            return 1

        listing = client.get("/api/v1/reports", params={"page_size": 5})
        items = listing.json().get("items", [])
        if not items:
            print("  FAIL  no reports exist. Run scripts/seed_example.py first.")
            return 1

        for report in items:
            reference = report["reference"]
            for fmt, extract in (("pdf", pdf_text), ("docx", docx_text)):
                got = client.get(f"/api/v1/reports/{report['id']}/document.{fmt}")
                if got.status_code != 200:
                    print(f"  FAIL  {reference} {fmt}: HTTP {got.status_code}")
                    failures += 1
                    continue
                text = extract(got.content)
                problems = [label for label, needle in BANNED.items() if needle in text]
                if problems:
                    print(f"  FAIL  {reference} {fmt}: contains {problems}")
                    failures += 1
                else:
                    print(
                        f"  ok    {reference} {fmt}: "
                        f"{len(got.content)} bytes, {len(text.split())} words, none of "
                        f"{sorted(BANNED)}"
                    )

    print()
    print(f"report document problems: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
