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
from ...domain import enums, states
from ...security.permissions import permission_matrix

router = APIRouter(prefix="/reference", tags=["reference"])


def _enum_options(enum_class: type[enums.StrEnum]) -> list[dict[str, str]]:
    return [
        {"value": member.value, "label": member.value.replace("_", " ").capitalize()}
        for member in enum_class
    ]


@router.get("/vocabulary", summary="Enumerations used across the API")
async def vocabulary() -> dict[str, Any]:
    """Public: the sign-in page and the public complaint form both need this."""
    return {
        "roles": _enum_options(enums.Role),
        "inspection_states": _enum_options(enums.InspectionState),
        "inspection_sources": _enum_options(enums.InspectionSource),
        "package_faces": _enum_options(enums.PackageFace),
        "face_capture_states": _enum_options(enums.FaceCaptureState),
        "package_types": _enum_options(enums.PackageType),
        "quantity_kinds": _enum_options(enums.QuantityKind),
        "declaration_types": _enum_options(enums.DeclarationType),
        "machine_states": _enum_options(enums.MachineState),
        "review_states": _enum_options(enums.ReviewState),
        "legal_outcomes": _enum_options(enums.LegalOutcome),
        "rule_statuses": _enum_options(enums.RuleStatus),
        "complaint_states": _enum_options(enums.ComplaintState),
        "complaint_categories": _enum_options(enums.ComplaintCategory),
        "case_states": _enum_options(enums.CaseState),
        "notice_types": _enum_options(enums.NoticeType),
        "delivery_methods": _enum_options(enums.DeliveryMethod),
        "report_states": _enum_options(enums.ReportState),
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
