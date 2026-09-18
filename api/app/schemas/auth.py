"""Authentication schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import EmailStr, Field, field_validator

from ..domain.enums import Role
from ..security.permissions import normalise_jurisdiction
from .common import ReadSchema, Schema

# Jurisdiction codes are hierarchical and dash-separated: IN-HR-GURUGRAM.
JurisdictionCode = Annotated[
    str,
    Field(
        min_length=2,
        max_length=64,
        pattern=r"^[A-Za-z]{2}(-[A-Za-z0-9]{1,20}){0,3}$",
        examples=["IN-HR-GURUGRAM"],
    ),
]
Password = Annotated[str, Field(min_length=12, max_length=128)]


class _JurisdictionNormalising(Schema):
    @field_validator("jurisdiction_code", check_fields=False)
    @classmethod
    def _normalise(cls, value: str) -> str:
        return normalise_jurisdiction(value)


class BootstrapRequest(_JurisdictionNormalising):
    """One-time creation of the first administrator."""

    email: EmailStr
    name: str = Field(min_length=2, max_length=120)
    password: Password
    jurisdiction_code: JurisdictionCode
    jurisdiction_name: str = Field(min_length=2, max_length=160)
    designation: str | None = Field(default=None, max_length=120)


class LoginRequest(Schema):
    email: EmailStr
    # Not length-validated: a policy error here would tell an attacker that the
    # submitted string was the wrong shape rather than simply wrong.
    password: str = Field(min_length=1, max_length=256)


class UserCreateRequest(_JurisdictionNormalising):
    email: EmailStr
    name: str = Field(min_length=2, max_length=120)
    password: Password
    role: Role
    jurisdiction_code: JurisdictionCode
    jurisdiction_name: str = Field(min_length=2, max_length=160)
    designation: str | None = Field(default=None, max_length=120)
    must_change_password: bool = True


class UserUpdateRequest(_JurisdictionNormalising):
    """Every field optional; only what is supplied changes."""

    name: str | None = Field(default=None, min_length=2, max_length=120)
    role: Role | None = None
    jurisdiction_code: JurisdictionCode | None = None
    jurisdiction_name: str | None = Field(default=None, min_length=2, max_length=160)
    designation: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None
    reason: str | None = Field(default=None, max_length=500)


class PasswordChangeRequest(Schema):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: Password


class PasswordResetRequest(Schema):
    email: EmailStr


class PasswordResetCompleteRequest(Schema):
    token: str = Field(min_length=20, max_length=200)
    new_password: Password


class AdminPasswordResetRequest(Schema):
    """An administrator issues a reset token for an account."""

    reason: str = Field(min_length=5, max_length=500)


class UserProfile(ReadSchema):
    id: str
    email: str
    name: str
    role: str
    designation: str | None
    jurisdiction_code: str
    jurisdiction_name: str
    scope: str
    must_change_password: bool
    permissions: list[str]


class UserSummary(ReadSchema):
    id: str
    email: str
    name: str
    role: str
    designation: str | None
    jurisdiction_code: str
    jurisdiction_name: str
    is_active: bool
    last_login_at: datetime | None
    locked: bool
    created_at: datetime


class SessionSummary(ReadSchema):
    id: str
    issued_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip_address: str | None
    user_agent: str | None
    is_current: bool


class LoginResponse(ReadSchema):
    user: UserProfile
    access_expires_at: datetime
    #: Echoed for clients that cannot read cookies (for example a test harness).
    csrf_token: str


class PasswordPolicyResponse(ReadSchema):
    min_length: int
    requirements: list[str]
