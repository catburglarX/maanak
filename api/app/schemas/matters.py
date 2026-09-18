"""Schemas for complaints, cases, notices and the audit trail."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from pydantic import EmailStr, Field, field_validator

from ..domain.enums import (
    CaseState,
    ComplaintCategory,
    ComplaintState,
    DeliveryMethod,
    NoticeType,
    PackageFace,
)
from .common import ExpectedVersion, ReadSchema, Schema

Reason = Annotated[str, Field(min_length=10, max_length=2000)]


# --------------------------------------------------------------------------
# Complaints
# --------------------------------------------------------------------------
class ComplaintSubmission(Schema):
    """The public complaint form.

    Contact details are optional: a consumer may report a problem without
    identifying themselves. Supplying one enables the status page and, with consent,
    contact by an officer.
    """

    product_name: str = Field(min_length=2, max_length=240)
    brand: str | None = Field(default=None, max_length=160)
    barcode_value: str | None = Field(default=None, max_length=32)
    category: ComplaintCategory
    description: str = Field(min_length=20, max_length=5000)
    purchase_date: date | None = None
    seller_name: str | None = Field(default=None, max_length=240)
    marketplace_name: str | None = Field(default=None, max_length=160)
    listing_url: str | None = Field(default=None, max_length=2000)
    stated_mrp: str | None = Field(default=None, max_length=40)
    stated_price_paid: str | None = Field(default=None, max_length=40)
    location_text: str | None = Field(default=None, max_length=240)
    contact_name: str | None = Field(default=None, max_length=160)
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=40)
    consent_to_contact: bool = False
    privacy_notice_acknowledged: bool = Field(
        description="Must be true. Records that the privacy notice was shown."
    )

    @field_validator("privacy_notice_acknowledged")
    @classmethod
    def _must_acknowledge(cls, value: bool) -> bool:
        if not value:
            raise ValueError("the privacy notice must be acknowledged before submitting")
        return value

    @field_validator("purchase_date")
    @classmethod
    def _not_future(cls, value: date | None) -> date | None:
        if value is not None and value > date.today():
            raise ValueError("the purchase date cannot be in the future")
        return value


class ComplaintSubmissionResponse(ReadSchema):
    reference: str
    state: str
    submitted_at: datetime
    priority: int
    priority_reason: str | None
    attachments_stored: int
    status_lookup_available: bool
    message: str


class ComplaintStatusQuery(Schema):
    reference: str = Field(min_length=6, max_length=40)
    contact: str = Field(
        min_length=5,
        max_length=320,
        description="The email address or phone number supplied when the complaint was made.",
    )


class ComplaintTriageRequest(Schema):
    jurisdiction_code: str | None = Field(default=None, max_length=64)
    jurisdiction_name: str | None = Field(default=None, max_length=160)
    priority: int | None = Field(default=None, ge=0, le=100)
    triage_note: str | None = Field(default=None, max_length=2000)
    duplicate_of_id: str | None = None
    matched_product_id: str | None = None
    expected_version: ExpectedVersion


class ComplaintAssignRequest(Schema):
    officer_id: str
    expected_version: ExpectedVersion


class ComplaintTransitionRequest(Schema):
    target_state: ComplaintState
    reason: str | None = Field(default=None, max_length=2000)
    expected_version: ExpectedVersion


class ComplaintToInspectionRequest(Schema):
    inspection_date: date
    jurisdiction_code: str | None = Field(default=None, max_length=64)
    premises_name: str | None = Field(default=None, max_length=240)
    premises_address: str | None = Field(default=None, max_length=1000)
    #: Create a product from the complaint details, or attach an existing one.
    product_id: str | None = None
    commodity_category: str = Field(default="unclassified", max_length=120)
    #: Copy the complainant's photographs into the inspection as evidence.
    promote_attachments: bool = True
    attachment_face: PackageFace = PackageFace.OTHER
    expected_version: ExpectedVersion


# --------------------------------------------------------------------------
# Cases and notices
# --------------------------------------------------------------------------
class CaseOpenRequest(Schema):
    inspection_id: str
    respondent_name: str = Field(min_length=2, max_length=240)
    respondent_role: str | None = Field(
        default=None, pattern="^(manufacturer|packer|importer|marketer|brand_owner)$"
    )
    respondent_address: str | None = Field(default=None, max_length=1000)
    respondent_email: EmailStr | None = None
    respondent_phone: str | None = Field(default=None, max_length=40)
    subject: str = Field(min_length=10, max_length=300)


class NoticePrepareRequest(Schema):
    notice_type: NoticeType = NoticeType.SHOW_CAUSE
    response_days: int = Field(default=15, ge=1, le=180)
    signature_block: str = Field(
        min_length=5,
        max_length=500,
        description="Name and designation of the issuing officer, as it should appear.",
    )


class NoticeIssueRequest(Schema):
    response_due_on: date
    expected_version: ExpectedVersion


class NoticeDeliveryRequest(Schema):
    method: DeliveryMethod
    delivered_at: datetime
    proof_reference: str | None = Field(default=None, max_length=160)
    note: str | None = Field(default=None, max_length=2000)
    expected_version: ExpectedVersion


class NoticeResponseRequest(Schema):
    summary: str = Field(min_length=10, max_length=5000)
    received_at: datetime
    expected_version: ExpectedVersion


class CaseTransitionRequest(Schema):
    target_state: CaseState
    reason: str | None = Field(default=None, max_length=2000)
    expected_version: ExpectedVersion


class CaseOutcomeRequest(Schema):
    outcome: str = Field(min_length=3, max_length=60)
    outcome_note: Reason
    expected_version: ExpectedVersion


class CaseEventRequest(Schema):
    event_type: str = Field(min_length=3, max_length=40)
    summary: str = Field(min_length=5, max_length=2000)
    scheduled_for: datetime | None = None


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------
class AuditEventOut(ReadSchema):
    id: str
    sequence: int
    action: str
    entity_type: str
    entity_id: str | None
    entity_version: int | None
    actor_id: str | None
    actor_role: str | None
    actor_email: str | None
    actor_is_public: bool
    jurisdiction_code: str | None
    old_values: dict[str, Any]
    new_values: dict[str, Any]
    reason: str | None
    request_id: str | None
    recorded_at: datetime
    event_hash: str
    previous_hash: str | None


class ChainVerificationOut(ReadSchema):
    chain_key: str
    events_checked: int
    intact: bool
    first_broken_sequence: int | None
    problem: str | None
    head_sequence: int
    head_hash: str | None
    note: str
