"""Unit tests for the presentation layer: labels, counted nouns, and the prose gate.

These cover the wording the product shows a reader, which is as easy to get wrong as
the arithmetic and much easier to leave wrong.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest

from app.domain import labels
from app.domain.enums import (
    DECIDED_STATES,
    DeclarationType,
    InspectionState,
    LegalOutcome,
    PackageFace,
    Role,
)
from app.services.phrasing import counted, plural, verb

# tests/ sits inside api/, so one level up is the api tree and two is the repository
# root. Both matter, and they are not the same thing in every environment: the built
# image holds api/ at /app with no repository above it, while a mounted checkout has the
# repository root one level up. Anchoring on tests/ gets both right.
API_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = API_ROOT.parent
WEB_UTIL = REPO_ROOT / "web" / "js" / "util.js"

# The browser sources sit outside api/, so a run that mounts api/ alone cannot see them.
# CI checks out the whole repository and these checks are authoritative there. Locally
# they say why they did not run rather than raising FileNotFoundError from a helper.
WEB_REACHABLE = WEB_UTIL.is_file()
MOUNT_HINT = (
    f"{WEB_UTIL} is not reachable. Mount the repository root rather than api/ alone: "
    'docker compose run --rm --no-deps -v "$PWD:/repo" -w /repo/api '
    "-e PYTHONPATH=/repo/api --entrypoint sh api scripts/run_tests.sh"
)


class TestLabels:
    def test_mechanical_rule_keeps_later_capitals(self) -> None:
        # str.capitalize would lower-case the rest and turn a proper noun to junk.
        assert labels.mechanical("country_of_origin") == "Country of origin"
        assert labels.mechanical("state_HR") == "State HR"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("mrp", "MRP"),
            ("mrp_close_up", "MRP close-up"),
            ("fssai_licence", "FSSAI licence"),
            ("non_compliant", "Non-compliant"),
            ("rule_admin", "Rule administrator"),
            ("ecommerce_listing", "E-commerce listing"),
            ("side_panel_left", "Left side panel"),
            ("follow_up_inspection", "Follow-up inspection"),
        ],
    )
    def test_exceptions_are_applied(self, value: str, expected: str) -> None:
        assert labels.label_for(value) == expected

    def test_no_label_is_left_looking_mechanical(self) -> None:
        """No label may contain an underscore or a lower-case acronym."""
        for enum_class in (PackageFace, DeclarationType, LegalOutcome, Role, InspectionState):
            for option in labels.options(enum_class):
                label = option["label"]
                assert "_" not in label, f"{option['value']} label still has an underscore"
                assert not label.startswith("Mrp"), f"{option['value']} label reads as Mrp"
                assert not label.startswith("Fssai"), f"{option['value']} label reads as Fssai"

    def test_every_override_matches_a_real_enum_value(self) -> None:
        """A stale override is worse than none: it never fires and hides a typo."""
        known = set()
        for enum_class in (
            PackageFace,
            DeclarationType,
            LegalOutcome,
            Role,
            InspectionState,
        ):
            known.update(member.value for member in enum_class)
        # These are not enum members. They are values carried on records:
        # complaint categories, case states, notice types, evidence kinds and the
        # party_role column, all of which the reports label through the same table.
        allowed_extras = {
            "mrp_overcharge",
            "ocr_input",
            "date_marking_close_up",
            "net_quantity_close_up",
            "mrp_close_up",
            "follow_up_inspection",
            "show_cause",
            "ecommerce_listing",
            "expired_or_date_issue",
        }
        for value in labels.OVERRIDES:
            assert value in known or value in allowed_extras, f"override {value!r} is stale"

    def test_decision_options_are_exactly_the_decided_states(self) -> None:
        """The choices offered must be the choices the service accepts.

        The browser used to populate this select from LegalOutcome, which offered
        three values POST /inspections/{id}/decision rejects and omitted
        "violation found" entirely.
        """
        offered = {
            option["value"]
            for option in labels.options_for(
                tuple(state.value for state in InspectionState if state in DECIDED_STATES)
            )
        }
        assert offered == {state.value for state in DECIDED_STATES}
        assert "violation_found" in offered
        assert "not_applicable" not in offered


@pytest.mark.skipif(not WEB_REACHABLE, reason=MOUNT_HINT)
class TestJavaScriptLabelParity:
    """web/js/util.js keeps its own copy of the override table. Prove they agree.

    A few values are rendered without a vocabulary lookup, so the browser needs the
    table locally. Duplication is acceptable only while it is checked.
    """

    @staticmethod
    def _parse_js_overrides() -> dict[str, str]:
        source = WEB_UTIL.read_text(encoding="utf-8")
        block = re.search(r"export const LABEL_OVERRIDES = \{(.*?)\n\};", source, re.DOTALL)
        assert block, "LABEL_OVERRIDES not found in web/js/util.js"
        pairs = re.findall(r"^\s*([A-Za-z_][\w]*)\s*:\s*'([^']*)',", block.group(1), re.MULTILINE)
        return dict(pairs)

    def test_tables_are_identical(self) -> None:
        assert self._parse_js_overrides() == labels.OVERRIDES


class TestPhrasing:
    @pytest.mark.parametrize(
        ("noun", "count", "expected"),
        [
            ("test", 1, "test"),
            ("test", 2, "tests"),
            ("test", 0, "tests"),
            ("finding", 3, "findings"),
            ("category", 2, "categories"),
            ("analysis", 2, "analyses"),
            ("class", 2, "classes"),
            ("day", 2, "days"),
        ],
    )
    def test_plural_forms(self, noun: str, count: int, expected: str) -> None:
        assert plural(noun, count) == expected

    def test_counted_never_emits_the_parenthesised_s(self) -> None:
        assert counted(1, "test") == "1 test"
        assert counted(4, "test") == "4 tests"
        assert "(s)" not in counted(1, "machine reading")

    def test_verb_agreement(self) -> None:
        assert verb(1, "is", "are") == "is"
        assert verb(2, "is", "are") == "are"


class TestNoEmDashInSource:
    """The em dash is banned project-wide, so the ban is a test and not a habit.

    check_prose.py enforces this for pages and documents. This covers the source the
    prose checker does not read: Python, JavaScript and CSS.
    """

    EM_DASH = "\u2014"
    # Vendored third party, unmodified under the MPL, and never served to a user.
    SKIP: ClassVar[set[str]] = {"axe.min.js"}

    def test_source_files_are_clean(self) -> None:
        roots = [API_ROOT / "app", API_ROOT / "scripts", REPO_ROOT / "web"]
        offenders: list[str] = []
        scanned = 0
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.name in self.SKIP:
                    continue
                if path.suffix not in {".py", ".js", ".css", ".html"}:
                    continue
                scanned += 1
                if self.EM_DASH in path.read_text(encoding="utf-8"):
                    offenders.append(str(path))
        # A mis-mounted run would otherwise scan nothing and pass, which is the one
        # outcome worse than failing: a green check that proves nothing.
        assert scanned > 40, f"only {scanned} files were scanned. {MOUNT_HINT}"
        assert offenders == [], f"em dash found in: {offenders}"

    @pytest.mark.skipif(not WEB_REACHABLE, reason=MOUNT_HINT)
    def test_the_browser_sources_were_included(self) -> None:
        """Prove the scan above actually covered web/, not just the Python."""
        web_files = [
            path
            for path in (REPO_ROOT / "web").rglob("*")
            if path.is_file() and path.suffix in {".js", ".css", ".html"}
        ]
        assert len(web_files) > 30, f"found only {len(web_files)} browser sources"
