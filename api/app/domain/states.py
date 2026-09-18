"""State machines for inspections, complaints and cases.

Transitions are declared once and enforced in the service layer. A transition is
allowed only when (a) the pair of states is declared here and (b) the actor holds
one of the declared roles. Nothing in the frontend can widen this.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import CaseState, ComplaintState, InspectionState, Role

ALL_ROLES = frozenset(Role)
REVIEW_ROLES = frozenset({Role.REVIEWER, Role.CONTROLLER, Role.ADMIN})
FIELD_ROLES = frozenset({Role.INSPECTOR, Role.REVIEWER, Role.CONTROLLER, Role.ADMIN})
CONTROL_ROLES = frozenset({Role.CONTROLLER, Role.ADMIN})


@dataclass(frozen=True)
class Transition:
    """One allowed edge in a state machine."""

    source: str
    target: str
    roles: frozenset[Role]
    #: Human-readable label used in the UI and the audit trail.
    label: str
    #: When true the caller must supply a reason; recorded in the audit event.
    reason_required: bool = False
    #: Free-form guard names checked by the service before the transition.
    guards: tuple[str, ...] = field(default_factory=tuple)


class StateMachine:
    """Small explicit state machine with role and guard metadata."""

    def __init__(self, name: str, initial: str, transitions: list[Transition]) -> None:
        self.name = name
        self.initial = initial
        self._transitions: dict[tuple[str, str], Transition] = {
            (item.source, item.target): item for item in transitions
        }

    @property
    def transitions(self) -> tuple[Transition, ...]:
        return tuple(self._transitions.values())

    def find(self, source: str, target: str) -> Transition | None:
        return self._transitions.get((source, target))

    def targets_from(self, source: str) -> tuple[str, ...]:
        return tuple(
            target for (existing_source, target) in self._transitions if existing_source == source
        )

    def allowed_for(self, source: str, role: Role) -> tuple[Transition, ...]:
        return tuple(
            transition
            for (existing_source, _), transition in self._transitions.items()
            if existing_source == source and role in transition.roles
        )

    def states(self) -> frozenset[str]:
        found: set[str] = {self.initial}
        for source, target in self._transitions:
            found.add(source)
            found.add(target)
        return frozenset(found)


# --------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------
INSPECTION_MACHINE = StateMachine(
    name="inspection",
    initial=InspectionState.DRAFT,
    transitions=[
        Transition(
            InspectionState.DRAFT,
            InspectionState.EVIDENCE_PENDING,
            FIELD_ROLES,
            "Begin evidence capture",
            guards=("product_identified",),
        ),
        Transition(
            InspectionState.EVIDENCE_PENDING,
            InspectionState.PROCESSING,
            FIELD_ROLES,
            "Evidence submitted for analysis",
            guards=("has_evidence",),
        ),
        Transition(
            InspectionState.PROCESSING,
            InspectionState.OFFICER_REVIEW,
            FIELD_ROLES,
            "Analysis complete",
        ),
        Transition(
            InspectionState.PROCESSING,
            InspectionState.EVIDENCE_PENDING,
            FIELD_ROLES,
            "Analysis failed, capture again",
            reason_required=True,
        ),
        Transition(
            InspectionState.OFFICER_REVIEW,
            InspectionState.EVIDENCE_PENDING,
            FIELD_ROLES,
            "Capture more evidence",
            reason_required=True,
        ),
        Transition(
            InspectionState.OFFICER_REVIEW,
            InspectionState.ADDITIONAL_EVIDENCE_REQUIRED,
            FIELD_ROLES,
            "Request additional evidence",
            reason_required=True,
        ),
        Transition(
            InspectionState.ADDITIONAL_EVIDENCE_REQUIRED,
            InspectionState.EVIDENCE_PENDING,
            FIELD_ROLES,
            "Resume evidence capture",
        ),
        Transition(
            InspectionState.OFFICER_REVIEW,
            InspectionState.REVIEWER_REVIEW,
            FIELD_ROLES,
            "Send for reviewer decision",
            guards=("all_candidates_reviewed",),
        ),
        Transition(
            InspectionState.REVIEWER_REVIEW,
            InspectionState.OFFICER_REVIEW,
            REVIEW_ROLES,
            "Return to inspecting officer",
            reason_required=True,
        ),
        Transition(
            InspectionState.REVIEWER_REVIEW,
            InspectionState.ADDITIONAL_EVIDENCE_REQUIRED,
            REVIEW_ROLES,
            "Request additional evidence",
            reason_required=True,
        ),
        Transition(
            InspectionState.REVIEWER_REVIEW,
            InspectionState.COMPLIANT,
            REVIEW_ROLES,
            "Record: compliant",
            reason_required=True,
            guards=("all_candidates_reviewed", "all_checks_executed"),
        ),
        Transition(
            InspectionState.REVIEWER_REVIEW,
            InspectionState.VIOLATION_FOUND,
            REVIEW_ROLES,
            "Record: violation found",
            reason_required=True,
            guards=("all_candidates_reviewed", "all_checks_executed"),
        ),
        Transition(
            InspectionState.REVIEWER_REVIEW,
            InspectionState.UNABLE_TO_DETERMINE,
            REVIEW_ROLES,
            "Record: unable to determine",
            reason_required=True,
            guards=("all_candidates_reviewed",),
        ),
        Transition(
            InspectionState.COMPLIANT,
            InspectionState.REPORT_ISSUED,
            REVIEW_ROLES,
            "Issue report",
            guards=("decision_recorded",),
        ),
        Transition(
            InspectionState.VIOLATION_FOUND,
            InspectionState.REPORT_ISSUED,
            REVIEW_ROLES,
            "Issue report",
            guards=("decision_recorded",),
        ),
        Transition(
            InspectionState.UNABLE_TO_DETERMINE,
            InspectionState.REPORT_ISSUED,
            REVIEW_ROLES,
            "Issue report",
            guards=("decision_recorded",),
        ),
        Transition(
            InspectionState.REPORT_ISSUED,
            InspectionState.CASE_OPENED,
            REVIEW_ROLES,
            "Open case",
            guards=("report_issued",),
        ),
        Transition(
            InspectionState.REPORT_ISSUED,
            InspectionState.CLOSED,
            REVIEW_ROLES,
            "Close inspection",
            reason_required=True,
        ),
        Transition(
            InspectionState.CASE_OPENED,
            InspectionState.CLOSED,
            REVIEW_ROLES,
            "Close inspection",
            reason_required=True,
        ),
        Transition(
            InspectionState.CLOSED,
            InspectionState.ARCHIVED,
            CONTROL_ROLES,
            "Archive",
        ),
        # A reviewer may reopen a decided inspection before a report exists.
        Transition(
            InspectionState.COMPLIANT,
            InspectionState.REVIEWER_REVIEW,
            REVIEW_ROLES,
            "Reopen decision",
            reason_required=True,
        ),
        Transition(
            InspectionState.VIOLATION_FOUND,
            InspectionState.REVIEWER_REVIEW,
            REVIEW_ROLES,
            "Reopen decision",
            reason_required=True,
        ),
        Transition(
            InspectionState.UNABLE_TO_DETERMINE,
            InspectionState.REVIEWER_REVIEW,
            REVIEW_ROLES,
            "Reopen decision",
            reason_required=True,
        ),
    ],
)


# --------------------------------------------------------------------------
# Complaint
# --------------------------------------------------------------------------
COMPLAINT_MACHINE = StateMachine(
    name="complaint",
    initial=ComplaintState.RECEIVED,
    transitions=[
        Transition(
            ComplaintState.RECEIVED,
            ComplaintState.TRIAGED,
            FIELD_ROLES,
            "Triage",
        ),
        Transition(
            ComplaintState.RECEIVED,
            ComplaintState.DUPLICATE,
            FIELD_ROLES,
            "Mark duplicate",
            reason_required=True,
        ),
        Transition(
            ComplaintState.RECEIVED,
            ComplaintState.REJECTED,
            REVIEW_ROLES,
            "Reject",
            reason_required=True,
        ),
        Transition(
            ComplaintState.TRIAGED,
            ComplaintState.ASSIGNED,
            REVIEW_ROLES,
            "Assign to officer",
            guards=("assignee_present",),
        ),
        # An inspector who opens an inspection from a complaint has taken it on. That
        # is a normal field action and does not need a reviewer to assign it first;
        # assigning work to *another* officer still does.
        Transition(
            ComplaintState.TRIAGED,
            ComplaintState.INSPECTION_CREATED,
            FIELD_ROLES,
            "Open inspection",
            guards=("inspection_linked",),
        ),
        Transition(
            ComplaintState.TRIAGED,
            ComplaintState.DUPLICATE,
            FIELD_ROLES,
            "Mark duplicate",
            reason_required=True,
        ),
        Transition(
            ComplaintState.TRIAGED,
            ComplaintState.REJECTED,
            REVIEW_ROLES,
            "Reject",
            reason_required=True,
        ),
        Transition(
            ComplaintState.ASSIGNED,
            ComplaintState.INSPECTION_CREATED,
            FIELD_ROLES,
            "Create inspection",
            guards=("inspection_linked",),
        ),
        Transition(
            ComplaintState.ASSIGNED,
            ComplaintState.ESCALATED,
            FIELD_ROLES,
            "Escalate",
            reason_required=True,
        ),
        Transition(
            ComplaintState.ASSIGNED,
            ComplaintState.RESOLVED,
            REVIEW_ROLES,
            "Resolve",
            reason_required=True,
        ),
        Transition(
            ComplaintState.INSPECTION_CREATED,
            ComplaintState.RESOLVED,
            REVIEW_ROLES,
            "Resolve",
            reason_required=True,
        ),
        Transition(
            ComplaintState.INSPECTION_CREATED,
            ComplaintState.ESCALATED,
            FIELD_ROLES,
            "Escalate",
            reason_required=True,
        ),
        Transition(
            ComplaintState.ESCALATED,
            ComplaintState.RESOLVED,
            REVIEW_ROLES,
            "Resolve",
            reason_required=True,
        ),
        Transition(
            ComplaintState.RESOLVED,
            ComplaintState.CLOSED,
            REVIEW_ROLES,
            "Close",
        ),
        Transition(
            ComplaintState.REJECTED,
            ComplaintState.CLOSED,
            REVIEW_ROLES,
            "Close",
        ),
        Transition(
            ComplaintState.DUPLICATE,
            ComplaintState.CLOSED,
            REVIEW_ROLES,
            "Close",
        ),
    ],
)


# --------------------------------------------------------------------------
# Case
# --------------------------------------------------------------------------
CASE_MACHINE = StateMachine(
    name="case",
    initial=CaseState.DRAFT,
    transitions=[
        Transition(
            CaseState.DRAFT,
            CaseState.NOTICE_ISSUED,
            REVIEW_ROLES,
            "Issue notice",
            guards=("notice_ready",),
        ),
        Transition(
            CaseState.DRAFT,
            CaseState.WITHDRAWN,
            CONTROL_ROLES,
            "Withdraw",
            reason_required=True,
        ),
        Transition(
            CaseState.NOTICE_ISSUED,
            CaseState.AWAITING_RESPONSE,
            REVIEW_ROLES,
            "Record delivery",
            guards=("delivery_recorded",),
        ),
        Transition(
            CaseState.AWAITING_RESPONSE,
            CaseState.RESPONSE_RECEIVED,
            REVIEW_ROLES,
            "Record response",
        ),
        Transition(
            CaseState.AWAITING_RESPONSE,
            CaseState.UNDER_CONSIDERATION,
            REVIEW_ROLES,
            "No response, proceed",
            reason_required=True,
        ),
        Transition(
            CaseState.RESPONSE_RECEIVED,
            CaseState.HEARING_SCHEDULED,
            REVIEW_ROLES,
            "Schedule hearing",
        ),
        Transition(
            CaseState.RESPONSE_RECEIVED,
            CaseState.UNDER_CONSIDERATION,
            REVIEW_ROLES,
            "Consider response",
        ),
        Transition(
            CaseState.HEARING_SCHEDULED,
            CaseState.UNDER_CONSIDERATION,
            REVIEW_ROLES,
            "Record hearing outcome",
        ),
        Transition(
            CaseState.UNDER_CONSIDERATION,
            CaseState.FOLLOW_UP_INSPECTION,
            REVIEW_ROLES,
            "Order follow-up inspection",
        ),
        Transition(
            CaseState.FOLLOW_UP_INSPECTION,
            CaseState.UNDER_CONSIDERATION,
            REVIEW_ROLES,
            "Record follow-up result",
        ),
        Transition(
            CaseState.UNDER_CONSIDERATION,
            CaseState.RESOLVED,
            CONTROL_ROLES,
            "Record outcome",
            reason_required=True,
        ),
        Transition(
            CaseState.NOTICE_ISSUED,
            CaseState.WITHDRAWN,
            CONTROL_ROLES,
            "Withdraw notice",
            reason_required=True,
        ),
        Transition(
            CaseState.AWAITING_RESPONSE,
            CaseState.WITHDRAWN,
            CONTROL_ROLES,
            "Withdraw notice",
            reason_required=True,
        ),
        Transition(
            CaseState.RESOLVED,
            CaseState.CLOSED,
            CONTROL_ROLES,
            "Close case",
        ),
        Transition(
            CaseState.WITHDRAWN,
            CaseState.CLOSED,
            CONTROL_ROLES,
            "Close case",
        ),
    ],
)


MACHINES: dict[str, StateMachine] = {
    machine.name: machine for machine in (INSPECTION_MACHINE, COMPLAINT_MACHINE, CASE_MACHINE)
}
