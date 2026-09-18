"""Version 1 API router.

Routers are mounted in the order they appear in the navigation, so the generated
OpenAPI document reads in the same order as the application.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    audit,
    auth,
    cases,
    complaints,
    inspections,
    products,
    reference,
    reports,
    rules,
)

router = APIRouter()

router.include_router(auth.router)
router.include_router(auth.admin_router)
router.include_router(products.router)
router.include_router(inspections.router)
router.include_router(inspections.candidate_router)
router.include_router(inspections.finding_router)
router.include_router(inspections.job_router)
router.include_router(complaints.router)
router.include_router(cases.router)
router.include_router(reports.router)
router.include_router(rules.router)
router.include_router(reports.public_router)
router.include_router(complaints.public_router)
router.include_router(audit.router)
router.include_router(reference.router)
