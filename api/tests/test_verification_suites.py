"""Run the live-stack verification suites under pytest.

Each ``scripts/verify_*.py`` suite asserts against a running deployment: real
PostgreSQL, real Redis, real object storage, real Tesseract, real HTTP. They are
executed here as subprocesses so that one ``pytest`` invocation covers both the fast
unit tests and the integration suites, and so a failure prints the suite's own
per-check output rather than an opaque assertion error.

Marked ``integration`` because they need the stack:

    pytest -m integration            # these only
    pytest -m "not integration"      # fast unit tests only
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

#: (script, expected minimum number of checks). The minimum guards against a suite
#: silently degrading to a handful of assertions.
SUITES: tuple[tuple[str, int], ...] = (
    ("verify_schema.py", 14),
    ("verify_security.py", 51),
    ("verify_extraction.py", 62),
    ("verify_rules.py", 71),
    ("verify_auth_flow.py", 53),
    ("verify_pipeline.py", 56),
    ("verify_workflow.py", 117),
    ("verify_matters.py", 86),
)

#: Suites that need only the database, not the HTTP API.
DATABASE_ONLY = {"verify_schema.py"}
#: Suites that need no external service at all.
PURE = {"verify_security.py", "verify_extraction.py", "verify_rules.py"}


def _run(script: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.setdefault("API_URL", "http://api:8000")
    environment.setdefault("PYTHONPATH", str(SCRIPTS.parent))
    return subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [sys.executable, str(SCRIPTS / script)],
        capture_output=True,
        text=True,
        timeout=1800,
        env=environment,
        cwd=str(SCRIPTS.parent),
        check=False,
    )


def _reset_data() -> None:
    """Clear application data so each suite starts from a known state.

    The API suites each bootstrap their own first administrator, which is a one-time
    operation, so they cannot run one after another against the same data. Resetting
    between them is what makes the whole set runnable in a single pytest invocation.
    """
    result = subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [sys.executable, str(SCRIPTS / "reset_data.py")],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(SCRIPTS.parent),
        env=dict(os.environ) | {"PYTHONPATH": str(SCRIPTS.parent)},
        check=False,
    )
    if result.returncode != 0:
        pytest.fail("could not reset data before the suite:\n" + result.stdout + result.stderr)


def _assert_suite(script: str, minimum_checks: int, *, reset_first: bool = False) -> None:
    if reset_first:
        _reset_data()

    result = _run(script)
    output = result.stdout + result.stderr
    print(output)

    failed_lines = [line for line in output.splitlines() if line.strip().startswith("FAIL")]
    passed = "checks passed" in output

    assert result.returncode == 0, (
        f"{script} exited {result.returncode}. Failing checks:\n" + "\n".join(failed_lines[:20])
    )
    assert passed, f"{script} did not report a passing summary"

    # Confirm the suite actually ran the expected volume of assertions.
    total = 0
    for line in output.splitlines():
        if "checks passed" in line:
            for token in line.split():
                if token.isdigit():
                    total = int(token)
                    break
    assert total >= minimum_checks, (
        f"{script} reported only {total} checks, expected at least {minimum_checks}. "
        "A suite must not silently shrink."
    )


@pytest.mark.parametrize(
    ("script", "minimum"),
    [pytest.param(script, minimum, id=script) for script, minimum in SUITES if script in PURE],
)
def test_offline_suites(script: str, minimum: int) -> None:
    """Suites that need no running service."""
    _assert_suite(script, minimum)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("script", "minimum"),
    [
        pytest.param(script, minimum, id=script)
        for script, minimum in SUITES
        if script in DATABASE_ONLY
    ],
)
def test_database_suites(script: str, minimum: int) -> None:
    """Suites that need a migrated PostgreSQL database."""
    _assert_suite(script, minimum, reset_first=True)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("script", "minimum"),
    [
        pytest.param(script, minimum, id=script)
        for script, minimum in SUITES
        if script not in PURE and script not in DATABASE_ONLY
    ],
)
def test_api_suites(script: str, minimum: int) -> None:
    """Suites that drive the running API over HTTP.

    Each suite bootstraps its own first administrator, which is a one-time operation,
    so the data is reset before each one. They are ordered by the parametrisation
    above; run them with ``-p no:randomly`` if a random-order plugin is ever added.
    """
    _assert_suite(script, minimum, reset_first=True)


@pytest.mark.browser
def test_browser_suite() -> None:
    """Browser and accessibility suite.

    Skipped unless Playwright is importable, because it needs the dedicated test
    image (see api/Dockerfile.browser) rather than the application image.
    """
    pytest.importorskip("playwright", reason="run inside the maanak-browser image")
    _assert_suite("verify_browser.py", 59, reset_first=True)
