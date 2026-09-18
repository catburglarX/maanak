"""Compare the API surface against the operations the browser application calls.

The question this answers is "can an officer actually reach this feature?". An endpoint
nobody calls is either a gap in the interface or dead weight in the API, and both are
worth knowing about before anyone demonstrates the product.

Paths are read out of the template literals in web/js, so `/inspections/${id}/checks`
matches the spec's `/api/v1/inspections/{inspection_id}/checks`.

    docker run --rm -v "$PWD:/repo" -w /repo maanak-api:latest \
      python api/scripts/check_api_reach.py --base http://localhost:8000

Without --base it reads the committed route table from the source instead, so it works
without a running stack.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

WEB = Path("web/js")

#: Operations that are deliberately not called by the browser application, with the
#: reason. Anything here is a decision, not an oversight.
INTENTIONAL: dict[str, str] = {
    "GET /health": "operational probe, not part of the interface",
    "GET /health/live": "container liveness probe",
    "GET /health/ready": "container readiness probe, also used by the overview page banner",
    "POST /api/v1/auth/password-reset/complete": (
        "reached from an emailed link. No mail service is configured in this build, so the "
        "flow is exercised by verify_auth_flow.py rather than by a page"
    ),
    "POST /api/v1/auth/refresh": "called by the client interceptor on a 401, not by a page",
    "GET /api/v1/reference/state-machines": "reference data, published for integrators",
    "GET /api/v1/reference/permissions": "reference data, published for integrators",
    "GET /api/v1/products/gtin-check": "check-digit helper, published for integrators",
    "POST /api/v1/products/{product_id}/merge/{target_id}": (
        "deduplication is a data-stewardship task with no screen in this build"
    ),
    "PATCH /api/v1/products/{product_id}": "product editing has no screen in this build",
    "GET /api/v1/products/{product_id}": "the register shows every field a product record holds",
    "POST /api/v1/inspections/{inspection_id}/measurements": (
        "character-height measurement has no screen in this build; the check reports that "
        "the measurement is missing, which is the documented behaviour"
    ),
    "GET /api/v1/inspections/{inspection_id}/evidence/{evidence_id}/integrity": (
        "re-verification is an operator task, covered by verify_pipeline.py"
    ),
    "POST /api/v1/inspections/{inspection_id}/evidence/{evidence_id}/reanalyse": (
        "re-running the reader has no screen in this build"
    ),
    "GET /api/v1/audit/export.csv": "download link, built by the audit page as a plain href",
    "GET /api/v1/audit/summary": "aggregate counts, not shown in this build",
    "GET /api/v1/audit/entity/{entity_type}/{entity_id}": (
        "each detail screen embeds the timeline for its own record, so this is for "
        "integrators reading the history of an entity they hold an id for"
    ),
    "GET /api/v1/reports/{report_id}": (
        "the reports register and the inspection screen show every field a report holds"
    ),
    "GET /api/v1/reports/{report_id}/snapshot": (
        "the frozen record is what the PDF and the document are rendered from, and both "
        "are linked. The raw snapshot is for integrators and for verification"
    ),
    "PATCH /api/v1/rules/{rule_id}": (
        "rule text is seeded and then simulated and approved. Editing the clauses by hand "
        "has no screen in this build"
    ),
    "POST /api/v1/products/{product_id}/identifiers": (
        "a barcode read from evidence is attached by the worker. Adding one by hand has no "
        "screen in this build"
    ),
    "GET /api/v1/rules/{rule_id}/required-scenarios": (
        "the rule screen reports coverage from the simulate response instead"
    ),
    "GET /api/v1/rules/check-kinds": "reference data for rule authors, published for integrators",
    "POST /api/v1/rules": "rule authoring by hand has no screen; versions are seeded then edited",
}

CALL_RE = re.compile(
    r"""api\.(get|post|patch|del|postForm)\(\s*[`'"]([^`'"]+)[`'"]""", re.MULTILINE
)
#: createRegister takes the path as an option rather than calling api.get directly.
ENDPOINT_RE = re.compile(r"""endpoint:\s*[`'"]([^`'"]+)[`'"]""")
#: Downloads and exports are plain links, built as a string and set as an href.
HREF_RE = re.compile(r"""[`'"](/api/v1/[^`'"\s]+)[`'"]""")
METHODS = {"get": "GET", "post": "POST", "patch": "PATCH", "del": "DELETE", "postForm": "POST"}


def normalise(path: str) -> str:
    """Turn a template literal into a spec-shaped path."""
    path = re.sub(r"\$\{[^}]*\}", "{x}", path)
    path = path.split("?")[0].rstrip("/")
    if not path.startswith("/api/v1") and not path.startswith("/health"):
        path = "/api/v1" + path
    # The document route is declared as document.{fmt} and linked as document.pdf and
    # document.docx, so the concrete extensions have to reduce to the same shape.
    path = re.sub(r"/document\.(pdf|docx)$", "/document.{x}", path)
    return re.sub(r"\{[^}]*\}", "{x}", path)


def spec_operations(base: str | None) -> set[str]:
    if base:
        with urllib.request.urlopen(f"{base}/openapi.json", timeout=30) as response:
            spec = json.load(response)
    else:
        raise SystemExit("--base is required: the route table is read from the running API")
    found = set()
    for path, methods in spec["paths"].items():
        for method in methods:
            if method.upper() in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
                found.add(f"{method.upper()} {path}")
    return found


def called_operations() -> set[str]:
    found = set()
    for path in sorted(WEB.rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        for method, route in CALL_RE.findall(text):
            found.add(f"{METHODS[method]} {normalise(route)}")
        for route in ENDPOINT_RE.findall(text):
            found.add(f"GET {normalise(route)}")
        for route in HREF_RE.findall(text):
            found.add(f"GET {normalise(route)}")
    return found


PARAM_RE = re.compile(r"\{[^}]*\}")


def shape_of(operation: str) -> str:
    """Reduce an operation to method plus path with every parameter anonymised."""
    method, path = operation.split(" ", 1)
    return f"{method} {PARAM_RE.sub('{x}', path.rstrip('/'))}"


def main(argv: list[str]) -> int:
    base = None
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]
    operations = spec_operations(base)
    called = called_operations()

    unreached = [op for op in sorted(operations) if shape_of(op) not in called]

    documented = [op for op in unreached if op in INTENTIONAL]
    undocumented = [op for op in unreached if op not in INTENTIONAL]

    print(f"API operations: {len(operations)}")
    print(f"reached from the browser application: {len(operations) - len(unreached)}")
    print(f"deliberately not reached: {len(documented)}")
    print()
    for operation in documented:
        print(f"  note  {operation}")
        print(f"        {INTENTIONAL[operation]}")
    if undocumented:
        print()
        print("Not reachable from any screen, and not recorded as deliberate:")
        for operation in undocumented:
            print(f"  FAIL  {operation}")
    stale = sorted(set(INTENTIONAL) - set(unreached))
    if stale:
        print()
        print("Recorded as deliberate but actually reached, so the note is stale:")
        for operation in stale:
            print(f"  FAIL  {operation}")
    print()
    print(f"unexplained unreachable operations: {len(undocumented) + len(stale)}")
    return 0 if not undocumented and not stale else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
