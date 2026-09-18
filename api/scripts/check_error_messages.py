"""Find user-facing error messages that leak a schema or column name.

An API message is read by an officer, not by a developer. "Supply either an existing
product_id" names a JSON key that nobody outside the codebase has seen. This reports
every string raised in an error that contains a snake_case token, so each one can be
judged.

    docker run --rm -v "$PWD/api:/w" -w /w maanak-api:latest \
      python scripts/check_error_messages.py
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ERRORS = {
    "ValidationError",
    "ConflictError",
    "NotFoundError",
    "GuardFailedError",
    "PermissionDeniedError",
    "PayloadTooLargeError",
    "SessionExpiredError",
}
SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

#: Tokens that are legitimately part of an officer-facing sentence, because they name a
#: state, an outcome or a value the interface itself displays.
ALLOWED = {
    "additional_evidence_required",
    "unable_to_determine",
    "violation_found",
    "not_applicable",
    "non_compliant",
    "officer_review",
    "reviewer_review",
    "evidence_pending",
    "report_issued",
    "case_opened",
    "legal_hold",
    "not_scanned",
    "check_prose",
    "reset_data",
    "seed_demo",
    "seed_example",
}


def message_strings(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        func = node.exc.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name not in ERRORS:
            continue
        for arg in node.exc.args:
            for part in ast.walk(arg):
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    found.append((node.lineno, part.value))
    return found


def main() -> int:
    root = Path("app")
    offenders = 0
    checked = 0
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, text in message_strings(tree):
            checked += 1
            leaks = sorted({t for t in SNAKE.findall(text) if t not in ALLOWED})
            if leaks:
                offenders += 1
                print(f"  FAIL  {path}:{line} names {leaks}")
                print(f"        {text.strip()[:96]!r}")
    print()
    print(f"error messages checked: {checked}")
    print(f"messages naming a schema or column: {offenders}")
    return 0 if offenders == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
