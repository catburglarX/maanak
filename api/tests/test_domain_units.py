"""Unit tests for identifiers, permissions, state machines and hashing."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.enums import CaseState, ComplaintState, InspectionState, Role
from app.domain.states import CASE_MACHINE, COMPLAINT_MACHINE, INSPECTION_MACHINE
from app.errors import NotFoundError, PermissionDeniedError
from app.security import passwords
from app.security.permissions import (
    JurisdictionScope,
    Permission,
    has_permission,
    normalise_jurisdiction,
    permission_matrix,
    require_permission,
)
from app.services import gtin
from app.services.canonical import canonical_json, canonical_sha256, json_safe


class TestGtin:
    # Check digits computed with the GS1 modulo-10 algorithm and confirmed against
    # the implementation, so these are real valid numbers rather than plausible ones.
    @pytest.mark.parametrize(
        "value",
        ["8901030865275", "4006381333931", "036000291452", "96385074", "00012345600012"],
    )
    def test_accepts_valid_check_digits(self, value):
        result = gtin.check(value)
        assert result.acceptable, result.problem
        assert result.check_digit_valid

    @pytest.mark.parametrize("value", ["8901030865278", "4006381333932", "96385075"])
    def test_rejects_wrong_check_digit_and_says_what_it_should_be(self, value):
        result = gtin.check(value)
        assert not result.acceptable
        assert result.problem and "last digit should be" in result.problem

    @pytest.mark.parametrize("value", ["12345", "1234567890123456", "abc"])
    def test_rejects_wrong_length(self, value):
        result = gtin.check(value)
        assert not result.acceptable

    def test_check_digit_matches_the_published_algorithm(self):
        # Worked through by hand: body 890103086527, weights 3,1 alternating from the
        # right, sum 115, so the check digit is (10 - 115 mod 10) mod 10 = 5.
        assert gtin.compute_check_digit("890103086527") == 5

    def test_separators_are_ignored(self):
        assert gtin.check("890-1030-865275").acceptable

    def test_india_prefix_is_observed_not_concluded(self):
        result = gtin.check("8901030865275")
        assert result.india_prefix is True
        # The scheme is recorded; nothing infers origin from it.
        assert result.scheme == "gtin"

    def test_pads_to_gtin14_for_comparison(self):
        assert gtin.to_gtin14("96385074") == "00000096385074"


class TestPermissions:
    def test_inspector_cannot_decide_or_approve(self):
        assert not has_permission(Role.INSPECTOR, Permission.INSPECTION_DECIDE)
        assert not has_permission(Role.INSPECTOR, Permission.RULE_APPROVE)
        assert not has_permission(Role.INSPECTOR, Permission.AUDIT_READ)

    def test_reviewer_decides_but_does_not_read_the_audit_trail(self):
        assert has_permission(Role.REVIEWER, Permission.INSPECTION_DECIDE)
        assert not has_permission(Role.REVIEWER, Permission.AUDIT_READ)

    def test_rule_administrator_cannot_touch_evidence(self):
        assert not has_permission(Role.RULE_ADMIN, Permission.EVIDENCE_UPLOAD)
        assert has_permission(Role.RULE_ADMIN, Permission.RULE_APPROVE)

    def test_administrator_holds_everything(self):
        for permission in Permission:
            assert has_permission(Role.ADMIN, permission), permission

    def test_require_permission_raises(self):
        with pytest.raises(PermissionDeniedError):
            require_permission(Role.INSPECTOR, Permission.RULE_APPROVE)

    def test_matrix_covers_every_role(self):
        assert set(permission_matrix()) == {role.value for role in Role}


class TestJurisdictionScope:
    def test_inspector_is_confined_to_its_own_code(self):
        scope = JurisdictionScope.for_account(Role.INSPECTOR, "IN-HR-GURUGRAM")
        assert scope.covers("IN-HR-GURUGRAM")
        assert not scope.covers("IN-HR-FARIDABAD")
        assert not scope.covers("IN-HR")
        assert not scope.covers("IN-PB-LUDHIANA")

    def test_controller_covers_its_subtree_only(self):
        scope = JurisdictionScope.for_account(Role.CONTROLLER, "IN-HR")
        assert scope.covers("IN-HR")
        assert scope.covers("IN-HR-GURUGRAM")
        assert not scope.covers("IN")
        assert not scope.covers("IN-PB-LUDHIANA")

    def test_prefix_confusion_is_rejected(self):
        scope = JurisdictionScope.for_account(Role.CONTROLLER, "IN-HR")
        assert not scope.covers("IN-HRX-SOMEWHERE")

    def test_administrator_is_unrestricted(self):
        scope = JurisdictionScope.for_account(Role.ADMIN, "IN")
        assert scope.unrestricted
        assert scope.covers("IN-PB-LUDHIANA")

    def test_out_of_scope_raises_not_found_not_forbidden(self):
        scope = JurisdictionScope.for_account(Role.INSPECTOR, "IN-HR-GURUGRAM")
        # 404 rather than 403, so a guessed identifier cannot confirm a record exists.
        with pytest.raises(NotFoundError):
            scope.require("IN-PB-LUDHIANA")

    def test_codes_are_normalised(self):
        assert normalise_jurisdiction("  in-hr-gurugram ") == "IN-HR-GURUGRAM"


class TestStateMachines:
    def test_inspection_cannot_jump_from_draft_to_decided(self):
        assert INSPECTION_MACHINE.find(InspectionState.DRAFT, InspectionState.COMPLIANT) is None
        assert INSPECTION_MACHINE.find(InspectionState.DRAFT, InspectionState.REPORT_ISSUED) is None

    def test_decision_edges_require_a_reason_and_review_guards(self):
        for target in (
            InspectionState.COMPLIANT,
            InspectionState.VIOLATION_FOUND,
            InspectionState.UNABLE_TO_DETERMINE,
        ):
            edge = INSPECTION_MACHINE.find(InspectionState.REVIEWER_REVIEW, target)
            assert edge is not None
            assert edge.reason_required
            assert "all_candidates_reviewed" in edge.guards

    def test_inspector_cannot_take_a_decision_edge(self):
        edge = INSPECTION_MACHINE.find(
            InspectionState.REVIEWER_REVIEW, InspectionState.VIOLATION_FOUND
        )
        assert edge is not None
        assert Role.INSPECTOR not in edge.roles

    def test_report_issue_requires_a_recorded_decision(self):
        edge = INSPECTION_MACHINE.find(
            InspectionState.VIOLATION_FOUND, InspectionState.REPORT_ISSUED
        )
        assert edge is not None
        assert "decision_recorded" in edge.guards

    def test_field_officer_can_open_an_inspection_from_a_triaged_complaint(self):
        edge = COMPLAINT_MACHINE.find(ComplaintState.TRIAGED, ComplaintState.INSPECTION_CREATED)
        assert edge is not None
        assert Role.INSPECTOR in edge.roles
        assert "inspection_linked" in edge.guards

    def test_notice_issue_requires_a_prepared_notice(self):
        edge = CASE_MACHINE.find(CaseState.DRAFT, CaseState.NOTICE_ISSUED)
        assert edge is not None
        assert "notice_ready" in edge.guards

    def test_only_controllers_close_or_withdraw_a_case(self):
        for source, target in (
            (CaseState.UNDER_CONSIDERATION, CaseState.RESOLVED),
            (CaseState.NOTICE_ISSUED, CaseState.WITHDRAWN),
        ):
            edge = CASE_MACHINE.find(source, target)
            assert edge is not None
            assert Role.REVIEWER not in edge.roles
            assert Role.CONTROLLER in edge.roles

    def test_every_machine_state_is_reachable_from_its_initial_state(self):
        for machine in (INSPECTION_MACHINE, COMPLAINT_MACHINE, CASE_MACHINE):
            reached = {machine.initial}
            frontier = [machine.initial]
            while frontier:
                current = frontier.pop()
                for target in machine.targets_from(current):
                    if target not in reached:
                        reached.add(target)
                        frontier.append(target)
            unreachable = machine.states() - reached
            assert not unreachable, f"{machine.name}: unreachable states {unreachable}"


class TestCanonicalHashing:
    def test_key_order_does_not_change_the_hash(self):
        first = {"a": 1, "b": {"c": 2, "d": 3}}
        second = {"b": {"d": 3, "c": 2}, "a": 1}
        assert canonical_sha256(first) == canonical_sha256(second)

    def test_decimal_keeps_trailing_zeros(self):
        # 45.00 and 45.0 are different printed prices and must hash differently.
        assert canonical_sha256({"v": Decimal("45.00")}) != canonical_sha256({"v": Decimal("45.0")})

    def test_decimal_never_goes_through_float(self):
        text = canonical_json({"v": Decimal("0.1")})
        assert "0.1" in text
        assert "0.1000000000000000055" not in text

    def test_hindi_text_is_not_escaped(self):
        text = canonical_json({"v": "शुद्ध मात्रा"})
        assert "शुद्ध" in text

    def test_datetime_is_normalised_to_utc(self):
        moment = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        assert "2026-03-01T12:00:00.000000+00:00" in canonical_json({"at": moment})

    def test_json_safe_round_trips_through_the_same_convention(self):
        payload = {"amount": Decimal("45.00"), "id": uuid.uuid4()}
        stored = json_safe(payload)
        # Re-hashing the stored form must reproduce the original hash, which is what
        # makes a report snapshot verifiable after a database round trip.
        assert canonical_sha256(stored) == canonical_sha256(payload)


class TestPasswordPolicy:
    def test_accepts_a_strong_passphrase(self):
        assert passwords.check_policy("Correct-Horse-Battery-7").acceptable

    @pytest.mark.parametrize(
        "candidate", ["short1!", "password", "aaaaaaaaaaaaaaaa", "111111111111"]
    )
    def test_rejects_weak_candidates(self, candidate):
        assert not passwords.check_policy(candidate).acceptable

    def test_rejects_a_password_containing_the_account_email(self):
        result = passwords.check_policy("ravi.kumar-2026", email="ravi.kumar@example.org")
        assert not result.acceptable

    def test_hash_is_argon2id_and_salted(self):
        first = passwords.hash_password("Correct-Horse-Battery-7")
        second = passwords.hash_password("Correct-Horse-Battery-7")
        assert first.startswith("$argon2id$")
        assert first != second

    def test_verify_rejects_a_malformed_stored_hash(self):
        assert passwords.verify_password("anything", "not-a-hash") == (False, False)
