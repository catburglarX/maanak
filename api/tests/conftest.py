"""Shared test configuration.

Two kinds of test live here:

* **unit** tests, which import pure functions and run in milliseconds with no
  database, no network and no container;
* **integration** tests, which drive the ``scripts/verify_*.py`` suites against a
  running stack. Those are marked so they can be selected or skipped.

Run the fast set with::

    pytest -m "not integration and not browser"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# The application package lives one level up from tests/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Unit tests must not depend on a .env file being present. A syntactically valid
# secret is supplied so app.config imports cleanly.
os.environ.setdefault("JWT_SECRET", "unit-test-secret-value-at-least-32-characters-long")
os.environ.setdefault("S3_SECRET_KEY", "unit-test-object-storage-secret")
os.environ.setdefault("MAANAK_ENV", "test")
# Keep Argon2 cheap so password tests do not dominate the run.
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_MEMORY_COST_KIB", "8192")


@pytest.fixture(scope="session")
def api_base_url() -> str:
    """Base URL used by the integration suites."""
    return os.environ.get("API_URL", "http://api:8000")


@pytest.fixture(scope="session")
def web_base_url() -> str:
    return os.environ.get("BASE_URL", "http://web:8080")
