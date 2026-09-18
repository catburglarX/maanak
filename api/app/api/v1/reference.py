"""Reference data.

The browser application reads its vocabulary from here rather than hard-coding
state names, roles or declaration types. That way a change to
``app/domain/enums.py`` cannot leave the interface offering a state the backend
will reject.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ...deps import Principal, get_principal
from ...domain import enums, labels, states
from ...security.permissions import permission_matrix

router = APIRouter(prefix="/reference", tags=["reference"])


@router.get("/vocabulary", summary="Enumerations used across the API")
async def vocabulary() -> dict[str, Any]:
    """Public: the sign-in page and the public complaint form both need this."""
    return {
        "roles": labels.options(enums.Role),
        "inspection_states": labels.options(enums.InspectionState),
        "inspection_sources": labels.options(enums.InspectionSource),
        "package_faces": labels.options(enums.PackageFace),
        "face_capture_states": labels.options(enums.FaceCaptureState),
        "package_types": labels.options(enums.PackageType),
        "quantity_kinds": labels.options(enums.QuantityKind),
        "declaration_types": labels.options(enums.DeclarationType),
        "machine_states": labels.options(enums.MachineState),
        "review_states": labels.options(enums.ReviewState),
        "legal_outcomes": labels.options(enums.LegalOutcome),
        # The three decisions an officer may record on an inspection. This is a
        # narrower set than legal_outcomes, which is the vocabulary of a single rule
        # test: a finding can come back "not applicable", an inspection cannot be
        # decided that way. The list is derived from DECIDED_STATES, the same
        # constant the service checks the submitted decision against, so the choices
        # offered and the choices accepted cannot drift apart.
        "decision_outcomes": labels.options_for(
            tuple(state.value for state in enums.InspectionState if state in enums.DECIDED_STATES)
        ),
        "rule_statuses": labels.options(enums.RuleStatus),
        "complaint_states": labels.options(enums.ComplaintState),
        "complaint_categories": labels.options(enums.ComplaintCategory),
        "case_states": labels.options(enums.CaseState),
        "notice_types": labels.options(enums.NoticeType),
        "delivery_methods": labels.options(enums.DeliveryMethod),
        "report_states": labels.options(enums.ReportState),
    }


@router.get("/permissions", summary="Role to permission matrix")
async def permissions(_principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    return {
        "matrix": permission_matrix(),
        "note": (
            "Holding a permission is necessary but not sufficient. Records are also "
            "filtered by jurisdiction inside every query."
        ),
    }


@router.get("/state-machines", summary="Allowed state transitions")
async def state_machines(
    _principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    return {
        name: {
            "initial": machine.initial,
            "states": sorted(machine.states()),
            "transitions": [
                {
                    "from": transition.source,
                    "to": transition.target,
                    "label": transition.label,
                    "roles": sorted(role.value for role in transition.roles),
                    "reason_required": transition.reason_required,
                    "guards": list(transition.guards),
                }
                for transition in machine.transitions
            ],
        }
        for name, machine in states.MACHINES.items()
    }
