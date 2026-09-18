"""Rules: authoring, simulation and governance.

Approval here means a rule administrator in this workspace reviewed the
interpretation and its simulator run. It is not a statement of statutory endorsement;
that is tracked separately through ``legal_authority_confirmed`` and is shown on every
finding that cites the version.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...db import get_db
from ...deps import Principal, requires
from ...domain.enums import RuleStatus
from ...errors import ConflictError, NotFoundError
from ...models.rule import RuleReview, RuleVersion
from ...schemas.common import Page, PageNumber, PageSize
from ...schemas.rules import (
    CheckKindInfo,
    LegalAuthorityRequest,
    RuleCreateRequest,
    RuleDetail,
    RuleSummary,
    RuleUpdateRequest,
    SimulateRequest,
    SimulationResponse,
    StatusChangeRequest,
)
from ...security import Permission
from ...services.rules import available_checks, governance, seed
from ...services.rules.simulator import (
    Scenario,
    boundary_scenarios,
    default_scenarios,
    required_kinds_for,
    simulate,
)

router = APIRouter(prefix="/rules", tags=["rules"])

#: One-line description per implemented check, shown in the authoring form.
CHECK_SUMMARIES: dict[str, str] = {
    "declaration_present": "A named declaration must appear on the package.",
    "any_declaration_present": "At least one of a set of declarations must appear.",
    "unit_sale_price_consistency": (
        "The printed unit sale price must agree with price divided by net quantity."
    ),
    "net_quantity_unit_permitted": "The net quantity must use a permitted unit.",
    "date_marking_completeness": "A date marking must state at least the month and year.",
    "country_of_origin_required": "An imported package must declare its country of origin.",
    "consumer_care_completeness": "Consumer care details must give a usable contact route.",
    "character_height_minimum": (
        "Printed characters must reach a minimum height, measured by an officer in " "millimetres."
    ),
    "retail_price_wording": "The retail sale price must carry the prescribed wording.",
}


def _summary(rule: RuleVersion) -> RuleSummary:
    return RuleSummary(
        id=str(rule.id),
        record_version=rule.record_version,
        code=rule.code,
        version=rule.version,
        title=rule.title,
        citation=rule.citation,
        status=rule.status,
        effective_from=rule.effective_from,
        effective_to=rule.effective_to,
        test_kind=rule.test_kind,
        legal_authority_confirmed=rule.legal_authority_confirmed,
        effective_for_inspections=rule.is_effective_for_rules,
        updated_at=rule.updated_at,
    )


def _detail(
    rule: RuleVersion, *, governance_payload: dict[str, Any], reviews: list[RuleReview]
) -> RuleDetail:
    return RuleDetail(
        **_summary(rule).model_dump(),
        source_url=rule.source_url,
        source_document_title=rule.source_document_title,
        gazette_reference=rule.gazette_reference,
        source_retrieved_on=rule.source_retrieved_on,
        commodity_scope=list(rule.commodity_scope or []),
        package_scope=list(rule.package_scope or []),
        quantity_kind=rule.quantity_kind,
        quantity_min_base=rule.quantity_min_base,
        quantity_max_base=rule.quantity_max_base,
        applies_to_imported=rule.applies_to_imported,
        applies_to_ecommerce=rule.applies_to_ecommerce,
        applies_to_multipiece=rule.applies_to_multipiece,
        exceptions=list(rule.exceptions or []),
        required_inputs=list(rule.required_inputs or []),
        test_specification=rule.test_specification or {},
        plain_explanation=rule.plain_explanation,
        interpretation_note=rule.interpretation_note,
        uncertainty_note=rule.uncertainty_note,
        legal_authority_note=rule.legal_authority_note,
        author_id=str(rule.author_id) if rule.author_id else None,
        reviewer_id=str(rule.reviewer_id) if rule.reviewer_id else None,
        approver_id=str(rule.approver_id) if rule.approver_id else None,
        submitted_at=rule.submitted_at,
        reviewed_at=rule.reviewed_at,
        approved_at=rule.approved_at,
        withdrawn_at=rule.withdrawn_at,
        withdrawn_reason=rule.withdrawn_reason,
        governance=governance_payload,
        reviews=[
            {
                "decision": item.decision,
                "note": item.note,
                "actor_id": str(item.actor_id) if item.actor_id else None,
                "actor_role": item.actor_role,
                "recorded_at": item.recorded_at,
            }
            for item in reviews
        ],
        test_results=rule.test_results or {},
    )


async def _load(db: AsyncSession, rule_id: uuid.UUID) -> RuleVersion:
    rule = await db.scalar(
        select(RuleVersion)
        .options(selectinload(RuleVersion.reviews))
        .where(RuleVersion.id == rule_id)
        .execution_options(populate_existing=True)
    )
    if rule is None:
        raise NotFoundError("That rule version was not found.")
    return rule


async def _detail_response(db: AsyncSession, rule: RuleVersion) -> RuleDetail:
    run = await governance.latest_test_run(db, rule.id)
    reviews = sorted(rule.reviews, key=lambda item: item.recorded_at)
    return _detail(
        rule,
        governance_payload=governance.governance_summary(rule, run),
        reviews=reviews,
    )


@router.get("/check-kinds", response_model=list[CheckKindInfo], summary="Implemented tests")
async def list_check_kinds(
    _principal: Principal = Depends(requires(Permission.RULE_READ)),
) -> list[CheckKindInfo]:
    """The deterministic tests a rule version may name.

    A rule cannot reference a test this build does not implement; the engine would
    return "unable to determine" rather than guess.
    """
    return [
        CheckKindInfo(kind=kind, summary=CHECK_SUMMARIES.get(kind, "No description recorded."))
        for kind in available_checks()
    ]


@router.get("", response_model=Page[RuleSummary], summary="List rule versions")
async def list_rules(
    page: PageNumber = Query(1),
    page_size: PageSize = Query(25),
    search: str = Query("", max_length=160),
    rule_status: RuleStatus | None = Query(None, alias="status"),
    effective_only: bool = Query(False),
    _principal: Principal = Depends(requires(Permission.RULE_READ)),
    db: AsyncSession = Depends(get_db),
) -> Page[RuleSummary]:
    conditions: list[Any] = []
    if search:
        pattern = f"%{search.lower()}%"
        conditions.append(
            or_(
                func.lower(RuleVersion.code).like(pattern),
                func.lower(RuleVersion.title).like(pattern),
                func.lower(RuleVersion.citation).like(pattern),
            )
        )
    if rule_status is not None:
        conditions.append(RuleVersion.status == rule_status.value)
    if effective_only:
        conditions.append(
            RuleVersion.status.in_([RuleStatus.APPROVED.value, RuleStatus.ACTIVE.value])
        )

    total = int(
        await db.scalar(select(func.count()).select_from(RuleVersion).where(*conditions)) or 0
    )
    rows = list(
        await db.scalars(
            select(RuleVersion)
            .where(*conditions)
            .order_by(RuleVersion.code.asc(), RuleVersion.version.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return Page.build([_summary(row) for row in rows], page=page, page_size=page_size, total=total)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=RuleDetail,
    summary="Create a draft rule version",
)
async def create_rule(
    payload: RuleCreateRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.RULE_CREATE)),
    db: AsyncSession = Depends(get_db),
) -> RuleDetail:
    governance.assert_test_kind_supported(payload.test_kind)

    # Next version number for this code.
    highest = await db.scalar(
        select(func.max(RuleVersion.version)).where(RuleVersion.code == payload.code)
    )
    next_version = int(highest or 0) + 1

    rule = RuleVersion(
        id=uuid.uuid4(),
        code=payload.code,
        version=next_version,
        title=payload.title,
        citation=payload.citation,
        source_url=payload.source_url,
        source_document_title=payload.source_document_title,
        gazette_reference=payload.gazette_reference,
        source_retrieved_on=payload.source_retrieved_on,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        amends_rule_version_id=(
            uuid.UUID(payload.amends_rule_version_id) if payload.amends_rule_version_id else None
        ),
        supersedes_rule_version_id=(
            uuid.UUID(payload.supersedes_rule_version_id)
            if payload.supersedes_rule_version_id
            else None
        ),
        commodity_scope=payload.commodity_scope,
        package_scope=payload.package_scope,
        quantity_kind=payload.quantity_kind.value if payload.quantity_kind else None,
        quantity_min_base=payload.quantity_min_base,
        quantity_max_base=payload.quantity_max_base,
        applies_to_imported=payload.applies_to_imported,
        applies_to_ecommerce=payload.applies_to_ecommerce,
        applies_to_multipiece=payload.applies_to_multipiece,
        exceptions=payload.exceptions,
        transition_conditions={},
        required_inputs=payload.required_inputs,
        test_kind=payload.test_kind,
        test_specification=payload.test_specification,
        plain_explanation=payload.plain_explanation,
        interpretation_note=payload.interpretation_note,
        uncertainty_note=payload.uncertainty_note,
        status=RuleStatus.DRAFT,
        author_id=principal.id,
        legal_authority_confirmed=False,
        legal_authority_note=(
            "Not confirmed. Record a qualified authority's confirmation before relying "
            "on this interpretation."
        ),
        test_results={},
    )
    db.add(rule)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(
            f"Version {next_version} of {payload.code} already exists.",
            code="rule_version_exists",
        ) from exc

    from ...services import audit as audit_service

    await audit_service.record(
        db,
        principal.audit_context(request),
        action="rule.created",
        entity_type="rule_version",
        entity_id=rule.id,
        new_values={
            "code": rule.code,
            "version": rule.version,
            "citation": rule.citation,
            "test_kind": rule.test_kind,
            "effective_from": rule.effective_from.isoformat(),
        },
    )
    await db.commit()
    loaded = await _load(db, rule.id)
    return await _detail_response(db, loaded)


@router.post(
    "/seed",
    response_model=dict,
    summary="Insert the starter rule set as drafts",
)
async def seed_starter_rules(
    request: Request,
    principal: Principal = Depends(requires(Permission.RULE_CREATE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Load the shipped starter rules.

    Every seeded version is a draft with an unverified citation. Nothing is approved
    and nothing affects an inspection until a rule administrator reviews, simulates and
    approves it.
    """
    result = await seed.seed_rules(db, author_id=principal.id)

    from ...services import audit as audit_service

    await audit_service.record(
        db,
        principal.audit_context(request),
        action="rule.seed_loaded",
        entity_type="rule_version",
        new_values={"created": result["created"], "skipped": result["skipped"]},
    )
    await db.commit()
    return result


@router.get("/{rule_id}", response_model=RuleDetail, summary="Read one rule version")
async def read_rule(
    rule_id: uuid.UUID,
    _principal: Principal = Depends(requires(Permission.RULE_READ)),
    db: AsyncSession = Depends(get_db),
) -> RuleDetail:
    rule = await _load(db, rule_id)
    return await _detail_response(db, rule)


@router.patch("/{rule_id}", response_model=RuleDetail, summary="Edit a draft rule version")
async def update_rule(
    rule_id: uuid.UUID,
    payload: RuleUpdateRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.RULE_UPDATE)),
    db: AsyncSession = Depends(get_db),
) -> RuleDetail:
    rule = await _load(db, rule_id)
    governance.assert_editable(rule)

    if rule.record_version != payload.expected_version:
        from ...errors import StaleRecordError

        raise StaleRecordError(
            "This rule version changed after you loaded it. Reload and try again.",
            details={
                "your_version": payload.expected_version,
                "current_version": rule.record_version,
            },
        )

    changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    if changes.get("test_kind"):
        governance.assert_test_kind_supported(changes["test_kind"])

    before = {key: getattr(rule, key, None) for key in changes}
    for field, value in changes.items():
        if value is None:
            continue
        setattr(rule, field, value.value if hasattr(value, "value") else value)

    # Editing invalidates any previous simulator run.
    rule.test_results = {}
    await db.flush()

    from ...services import audit as audit_service

    await audit_service.record(
        db,
        principal.audit_context(request),
        action="rule.updated",
        entity_type="rule_version",
        entity_id=rule.id,
        entity_version=rule.record_version,
        old_values={key: str(value) for key, value in before.items()},
        new_values={key: str(getattr(rule, key, None)) for key in changes},
    )
    await db.commit()
    reloaded = await _load(db, rule.id)
    return await _detail_response(db, reloaded)


@router.post(
    "/{rule_id}/simulate",
    response_model=SimulationResponse,
    summary="Run the rule simulator",
)
async def simulate_rule(
    rule_id: uuid.UUID,
    payload: SimulateRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.RULE_SIMULATE)),
    db: AsyncSession = Depends(get_db),
) -> SimulationResponse:
    """Run scenarios against a rule version and record the result.

    Approval is refused until a run covers every required case and passes.
    """
    rule = await _load(db, rule_id)

    scenarios: list[Scenario] = [
        Scenario.from_dict(item.model_dump(mode="json")) for item in payload.scenarios
    ]
    if payload.use_seeded_scenarios:
        seeded = (rule.test_results or {}).get("seeded_scenarios") or seed.scenarios_for(rule.code)
        scenarios.extend(Scenario.from_dict(item) for item in seeded)

    combined = list(scenarios)
    if payload.include_generated:
        combined.extend(default_scenarios(rule))
        compliant = next((item for item in scenarios if item.kind == "compliant"), None)
        if compliant is not None:
            combined.extend(boundary_scenarios(rule, compliant))

    report = simulate(rule, combined)

    await governance.record_test_run(
        db,
        principal.audit_context(request),
        rule=rule,
        scenarios=scenarios,
        run_by_id=principal.id,
        include_generated=payload.include_generated,
    )
    await db.commit()

    return SimulationResponse(
        passed_count=report.passed_count,
        failed_count=report.failed_count,
        all_passed=report.all_passed,
        approval_ready=report.approval_ready,
        covered_kinds=report.covered_kinds,
        required_kinds=report.required_kinds,
        missing_kinds=report.missing_kinds,
        scenarios=[item.as_dict() for item in report.results],
    )


@router.post(
    "/{rule_id}/status",
    response_model=RuleDetail,
    summary="Move a rule version through governance",
)
async def change_rule_status(
    rule_id: uuid.UUID,
    payload: StatusChangeRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.RULE_SUBMIT)),
    db: AsyncSession = Depends(get_db),
) -> RuleDetail:
    """Submit, request changes, approve, activate or withdraw.

    Approving and activating need their own permissions, checked here, and approval
    additionally requires that the caller is not the author.
    """
    rule = await _load(db, rule_id)

    if payload.target_status == RuleStatus.APPROVED:
        principal.require(Permission.RULE_APPROVE)
    elif payload.target_status == RuleStatus.ACTIVE:
        principal.require(Permission.RULE_ACTIVATE)
    elif payload.target_status == RuleStatus.WITHDRAWN:
        principal.require(Permission.RULE_WITHDRAW)

    await governance.change_status(
        db,
        principal.audit_context(request),
        rule=rule,
        target_status=payload.target_status.value,
        actor_id=principal.id,
        actor_role=principal.role,
        note=payload.note,
        expected_version=payload.expected_version,
    )
    await db.commit()
    reloaded = await _load(db, rule.id)
    return await _detail_response(db, reloaded)


@router.post(
    "/{rule_id}/legal-authority",
    response_model=RuleDetail,
    summary="Record a qualified authority's confirmation",
)
async def record_legal_authority(
    rule_id: uuid.UUID,
    payload: LegalAuthorityRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.RULE_APPROVE)),
    db: AsyncSession = Depends(get_db),
) -> RuleDetail:
    """Separate from approval: this records statutory endorsement, not workspace sign-off."""
    rule = await _load(db, rule_id)
    if rule.record_version != payload.expected_version:
        from ...errors import StaleRecordError

        raise StaleRecordError(
            "This rule version changed after you loaded it. Reload and try again.",
            details={
                "your_version": payload.expected_version,
                "current_version": rule.record_version,
            },
        )

    await governance.confirm_legal_authority(
        db,
        principal.audit_context(request),
        rule=rule,
        confirmed=payload.confirmed,
        note=payload.note,
        actor_id=principal.id,
    )
    await db.commit()
    reloaded = await _load(db, rule.id)
    return await _detail_response(db, reloaded)


@router.get(
    "/{rule_id}/required-scenarios",
    summary="Which simulator cases this version must cover",
)
async def rule_required_scenarios(
    rule_id: uuid.UUID,
    _principal: Principal = Depends(requires(Permission.RULE_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rule = await _load(db, rule_id)
    run = await governance.latest_test_run(db, rule.id)
    required = required_kinds_for(rule)
    covered = list(run.covered_scenario_kinds or []) if run else []
    return {
        "required_kinds": required,
        "covered_kinds": covered,
        "missing_kinds": [kind for kind in required if kind not in covered],
        "seeded_scenarios": (rule.test_results or {}).get("seeded_scenarios")
        or seed.scenarios_for(rule.code),
    }
