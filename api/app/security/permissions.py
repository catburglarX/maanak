"""Permissions and jurisdiction scope.

Two independent gates, both enforced server-side:

Role permission
    What kind of action the account may perform at all.

Jurisdiction scope
    Which records the account may perform it on.

Both must pass. A reviewer holding ``inspection.decide`` still cannot decide an
inspection belonging to another jurisdiction, and the scope is applied inside the
SQL query rather than after loading, so a guessed UUID returns 404.

Jurisdiction codes are hierarchical, dash-separated, most general first::

    IN                  national
    IN-HR               state
    IN-HR-GURUGRAM      district

An account at ``IN-HR`` covers ``IN-HR`` and everything beneath it. It does not
cover ``IN`` or ``IN-PB``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import ColumnElement, or_
from sqlalchemy.orm import InstrumentedAttribute

from ..domain.enums import Role
from ..errors import PermissionDeniedError

JURISDICTION_SEPARATOR = "-"
#: Code that grants access to every jurisdiction. Reserved for administrators.
NATIONAL_CODE = "IN"


class Permission(StrEnum):
    """Every guarded action in the system."""

    # Accounts
    USER_CREATE = "user.create"
    USER_READ = "user.read"
    USER_UPDATE = "user.update"
    USER_DEACTIVATE = "user.deactivate"
    USER_RESET_PASSWORD = "user.reset_password"  # noqa: S105 - a permission name
    SESSION_REVOKE_ANY = "session.revoke_any"

    # Products
    PRODUCT_CREATE = "product.create"
    PRODUCT_READ = "product.read"
    PRODUCT_UPDATE = "product.update"
    PRODUCT_MERGE = "product.merge"

    # Inspections
    INSPECTION_CREATE = "inspection.create"
    INSPECTION_READ = "inspection.read"
    INSPECTION_UPDATE = "inspection.update"
    INSPECTION_ASSIGN = "inspection.assign"
    INSPECTION_TRANSITION = "inspection.transition"
    INSPECTION_DECIDE = "inspection.decide"
    INSPECTION_ARCHIVE = "inspection.archive"

    # Evidence
    EVIDENCE_UPLOAD = "evidence.upload"
    EVIDENCE_READ = "evidence.read"
    EVIDENCE_DOWNLOAD_ORIGINAL = "evidence.download_original"
    EVIDENCE_REANALYSE = "evidence.reanalyse"

    # Candidates and findings
    CANDIDATE_REVIEW = "candidate.review"
    FINDING_READ = "finding.read"
    FINDING_OVERRIDE = "finding.override"
    CHECKS_RUN = "checks.run"

    # Rules
    RULE_READ = "rule.read"
    RULE_CREATE = "rule.create"
    RULE_UPDATE = "rule.update"
    RULE_SUBMIT = "rule.submit"
    RULE_APPROVE = "rule.approve"
    RULE_ACTIVATE = "rule.activate"
    RULE_WITHDRAW = "rule.withdraw"
    RULE_SIMULATE = "rule.simulate"

    # Complaints
    COMPLAINT_READ = "complaint.read"
    COMPLAINT_TRIAGE = "complaint.triage"
    COMPLAINT_ASSIGN = "complaint.assign"
    COMPLAINT_RESOLVE = "complaint.resolve"
    COMPLAINT_CONVERT = "complaint.convert"

    # Cases and notices
    CASE_READ = "case.read"
    CASE_CREATE = "case.create"
    CASE_UPDATE = "case.update"
    CASE_TRANSITION = "case.transition"
    NOTICE_ISSUE = "notice.issue"
    NOTICE_WITHDRAW = "notice.withdraw"
    CASE_CLOSE = "case.close"

    # Reports
    REPORT_READ = "report.read"
    REPORT_ISSUE = "report.issue"
    REPORT_WITHDRAW = "report.withdraw"
    REPORT_DOWNLOAD = "report.download"

    # Oversight
    AUDIT_READ = "audit.read"
    AUDIT_VERIFY = "audit.verify"
    EXPORT_CREATE = "export.create"
    DASHBOARD_READ = "dashboard.read"
    TOOLS_USE = "tools.use"
    LISTING_FETCH = "listing.fetch"


_INSPECTOR: frozenset[Permission] = frozenset(
    {
        Permission.PRODUCT_CREATE,
        Permission.PRODUCT_READ,
        Permission.PRODUCT_UPDATE,
        Permission.INSPECTION_CREATE,
        Permission.INSPECTION_READ,
        Permission.INSPECTION_UPDATE,
        Permission.INSPECTION_TRANSITION,
        Permission.EVIDENCE_UPLOAD,
        Permission.EVIDENCE_READ,
        Permission.EVIDENCE_DOWNLOAD_ORIGINAL,
        Permission.EVIDENCE_REANALYSE,
        Permission.CANDIDATE_REVIEW,
        Permission.FINDING_READ,
        Permission.CHECKS_RUN,
        Permission.RULE_READ,
        Permission.COMPLAINT_READ,
        Permission.COMPLAINT_TRIAGE,
        Permission.COMPLAINT_CONVERT,
        Permission.CASE_READ,
        Permission.REPORT_READ,
        Permission.REPORT_DOWNLOAD,
        Permission.DASHBOARD_READ,
        Permission.TOOLS_USE,
        Permission.LISTING_FETCH,
        Permission.EXPORT_CREATE,
    }
)

# A reviewer records the legal decision and may issue reports.
_REVIEWER: frozenset[Permission] = _INSPECTOR | {
    Permission.INSPECTION_ASSIGN,
    Permission.INSPECTION_DECIDE,
    Permission.FINDING_OVERRIDE,
    Permission.COMPLAINT_ASSIGN,
    Permission.COMPLAINT_RESOLVE,
    Permission.CASE_CREATE,
    Permission.CASE_UPDATE,
    Permission.CASE_TRANSITION,
    Permission.NOTICE_ISSUE,
    Permission.REPORT_ISSUE,
}

# A controller supervises a wider jurisdiction and can close matters.
_CONTROLLER: frozenset[Permission] = _REVIEWER | {
    Permission.PRODUCT_MERGE,
    Permission.INSPECTION_ARCHIVE,
    Permission.NOTICE_WITHDRAW,
    Permission.CASE_CLOSE,
    Permission.REPORT_WITHDRAW,
    Permission.AUDIT_READ,
    Permission.AUDIT_VERIFY,
    Permission.USER_READ,
}

# A rule administrator governs legal content and does not handle inspections.
_RULE_ADMIN: frozenset[Permission] = frozenset(
    {
        Permission.RULE_READ,
        Permission.RULE_CREATE,
        Permission.RULE_UPDATE,
        Permission.RULE_SUBMIT,
        Permission.RULE_APPROVE,
        Permission.RULE_ACTIVATE,
        Permission.RULE_WITHDRAW,
        Permission.RULE_SIMULATE,
        Permission.INSPECTION_READ,
        Permission.FINDING_READ,
        Permission.DASHBOARD_READ,
        Permission.AUDIT_READ,
        Permission.AUDIT_VERIFY,
        Permission.TOOLS_USE,
    }
)

_ADMIN: frozenset[Permission] = frozenset(Permission)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.INSPECTOR: _INSPECTOR,
    Role.REVIEWER: _REVIEWER,
    Role.CONTROLLER: _CONTROLLER,
    Role.RULE_ADMIN: _RULE_ADMIN,
    Role.ADMIN: _ADMIN,
}

#: Roles whose jurisdiction covers descendants as well as their own code.
#: An inspector sees only their exact jurisdiction.
HIERARCHY_ROLES: frozenset[Role] = frozenset({Role.CONTROLLER, Role.ADMIN})

#: Roles that are not jurisdiction-bound at all.
UNSCOPED_ROLES: frozenset[Role] = frozenset({Role.ADMIN, Role.RULE_ADMIN})


def permissions_for(role: Role | str) -> frozenset[Permission]:
    try:
        return ROLE_PERMISSIONS[Role(role)]
    except (ValueError, KeyError):
        return frozenset()


def has_permission(role: Role | str, permission: Permission) -> bool:
    return permission in permissions_for(role)


def require_permission(role: Role | str, permission: Permission) -> None:
    if not has_permission(role, permission):
        raise PermissionDeniedError(
            f"Your role does not permit {permission.value}.",
            details={"required_permission": permission.value, "role": str(role)},
        )


def permission_matrix() -> dict[str, list[str]]:
    """Role -> sorted permission list. Served by the API and used in the docs."""
    return {
        role.value: sorted(permission.value for permission in permissions)
        for role, permissions in ROLE_PERMISSIONS.items()
    }


def normalise_jurisdiction(code: str) -> str:
    """Upper-case, trimmed, single-separator form."""
    parts = [
        part.strip().upper() for part in code.strip().split(JURISDICTION_SEPARATOR) if part.strip()
    ]
    return JURISDICTION_SEPARATOR.join(parts)


def is_descendant(candidate: str, ancestor: str) -> bool:
    """True when ``candidate`` is ``ancestor`` or sits beneath it."""
    candidate = normalise_jurisdiction(candidate)
    ancestor = normalise_jurisdiction(ancestor)
    if not candidate or not ancestor:
        return False
    if candidate == ancestor:
        return True
    return candidate.startswith(ancestor + JURISDICTION_SEPARATOR)


@dataclass(frozen=True)
class JurisdictionScope:
    """The set of jurisdictions an account may act in."""

    code: str
    role: Role
    unrestricted: bool = False
    include_descendants: bool = False

    @classmethod
    def for_account(cls, role: Role | str, jurisdiction_code: str) -> JurisdictionScope:
        role_enum = Role(role)
        return cls(
            code=normalise_jurisdiction(jurisdiction_code),
            role=role_enum,
            unrestricted=role_enum in UNSCOPED_ROLES,
            include_descendants=role_enum in HIERARCHY_ROLES,
        )

    def covers(self, jurisdiction_code: str | None) -> bool:
        if self.unrestricted:
            return True
        if not jurisdiction_code:
            # Unassigned records (for example a new public complaint) are visible
            # to anyone who can triage; assignment sets the code.
            return True
        target = normalise_jurisdiction(jurisdiction_code)
        if self.include_descendants:
            return is_descendant(target, self.code)
        return target == self.code

    def require(self, jurisdiction_code: str | None) -> None:
        """Raise 404-shaped denial when the record is out of scope.

        ``NotFoundError`` rather than ``PermissionDeniedError`` on purpose: a
        403 would confirm that the record exists.
        """
        if not self.covers(jurisdiction_code):
            from ..errors import NotFoundError

            raise NotFoundError

    def filter(
        self,
        column: InstrumentedAttribute[str] | InstrumentedAttribute[str | None],
        *,
        allow_null: bool = False,
    ) -> ColumnElement[bool] | None:
        """SQL predicate restricting a query to this scope.

        Returns ``None`` when no restriction applies, so callers can skip adding
        a WHERE clause entirely.
        """
        if self.unrestricted:
            return None
        if self.include_descendants:
            predicate: ColumnElement[bool] = or_(
                column == self.code,
                column.startswith(self.code + JURISDICTION_SEPARATOR),
            )
        else:
            predicate = column == self.code
        if allow_null:
            predicate = or_(predicate, column.is_(None))
        return predicate

    def describe(self) -> str:
        if self.unrestricted:
            return "all jurisdictions"
        if self.include_descendants:
            return f"{self.code} and below"
        return self.code
