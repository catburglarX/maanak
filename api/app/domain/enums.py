"""Domain enumerations.

These values are the single source of truth. Database CHECK constraints,
Pydantic schemas, the state machines and the frontend all derive from them, so a
new state cannot be introduced in one layer only.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """Workspace roles. ``public`` is implicit (no account) and not stored."""

    INSPECTOR = "inspector"
    REVIEWER = "reviewer"
    CONTROLLER = "controller"
    RULE_ADMIN = "rule_admin"
    ADMIN = "admin"


ASSIGNABLE_ROLES = tuple(role.value for role in Role)


class InspectionState(StrEnum):
    DRAFT = "draft"
    EVIDENCE_PENDING = "evidence_pending"
    PROCESSING = "processing"
    OFFICER_REVIEW = "officer_review"
    REVIEWER_REVIEW = "reviewer_review"
    ADDITIONAL_EVIDENCE_REQUIRED = "additional_evidence_required"
    COMPLIANT = "compliant"
    VIOLATION_FOUND = "violation_found"
    UNABLE_TO_DETERMINE = "unable_to_determine"
    REPORT_ISSUED = "report_issued"
    CASE_OPENED = "case_opened"
    CLOSED = "closed"
    ARCHIVED = "archived"


#: States that represent a recorded legal outcome for the inspection.
DECIDED_STATES = frozenset(
    {
        InspectionState.COMPLIANT,
        InspectionState.VIOLATION_FOUND,
        InspectionState.UNABLE_TO_DETERMINE,
    }
)

#: States after which evidence and findings become read-only.
FROZEN_STATES = frozenset(
    {
        InspectionState.REPORT_ISSUED,
        InspectionState.CASE_OPENED,
        InspectionState.CLOSED,
        InspectionState.ARCHIVED,
    }
)


class PackageFace(StrEnum):
    """Package faces an officer is expected to capture."""

    PRINCIPAL_DISPLAY_PANEL = "principal_display_panel"
    DECLARATION_PANEL = "declaration_panel"
    BACK_PANEL = "back_panel"
    SIDE_PANEL_LEFT = "side_panel_left"
    SIDE_PANEL_RIGHT = "side_panel_right"
    TOP_PANEL = "top_panel"
    BOTTOM_PANEL = "bottom_panel"
    BARCODE_AREA = "barcode_area"
    MRP_CLOSE_UP = "mrp_close_up"
    NET_QUANTITY_CLOSE_UP = "net_quantity_close_up"
    DATE_MARKING_CLOSE_UP = "date_marking_close_up"
    INGREDIENTS_PANEL = "ingredients_panel"
    NUTRITION_PANEL = "nutrition_panel"
    IMPORTER_LABEL = "importer_label"
    OTHER = "other"


class FaceCaptureState(StrEnum):
    REQUIRED = "required"
    CAPTURED = "captured"
    ABSENT = "absent"
    NOT_APPLICABLE = "not_applicable"
    UNABLE_TO_CAPTURE = "unable_to_capture"
    ADDITIONAL_IMAGE_REQUIRED = "additional_image_required"


class EvidenceKind(StrEnum):
    """Original evidence is never modified; derivatives are regenerable."""

    ORIGINAL = "original"
    THUMBNAIL = "thumbnail"
    OCR_INPUT = "ocr_input"
    ANNOTATED = "annotated"
    REPORT_IMAGE = "report_image"


class AnalysisState(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RetentionState(StrEnum):
    ACTIVE = "active"
    SCHEDULED_FOR_DELETION = "scheduled_for_deletion"
    DELETED = "deleted"


class DeclarationType(StrEnum):
    """Declarations Maanak can extract and test."""

    MRP = "mrp"
    NET_QUANTITY = "net_quantity"
    UNIT_SALE_PRICE = "unit_sale_price"
    RETAIL_SALE_PRICE_LABEL = "retail_sale_price_label"
    MANUFACTURER = "manufacturer"
    PACKER = "packer"
    IMPORTER = "importer"
    MARKETER = "marketer"
    RESPONSIBLE_PARTY_ADDRESS = "responsible_party_address"
    CONSUMER_CARE_EMAIL = "consumer_care_email"
    CONSUMER_CARE_PHONE = "consumer_care_phone"
    CONSUMER_CARE_NAME = "consumer_care_name"
    COUNTRY_OF_ORIGIN = "country_of_origin"
    COMMON_GENERIC_NAME = "common_generic_name"
    DATE_OF_MANUFACTURE = "date_of_manufacture"
    DATE_OF_PACKING = "date_of_packing"
    DATE_OF_IMPORT = "date_of_import"
    BEST_BEFORE = "best_before"
    BATCH_NUMBER = "batch_number"
    LOT_NUMBER = "lot_number"
    INGREDIENTS = "ingredients"
    NUTRITION = "nutrition"
    ALLERGEN_STATEMENT = "allergen_statement"
    VEG_NONVEG_MARK = "veg_nonveg_mark"
    FSSAI_LICENCE = "fssai_licence"


class MachineState(StrEnum):
    """What the machine observed. Never a legal conclusion."""

    LOCATED = "located"
    NOT_DETECTED = "not_detected"
    UNREADABLE = "unreadable"
    AMBIGUOUS = "ambiguous"
    INCOMPLETE_EVIDENCE = "incomplete_evidence"
    PROCESSING_FAILED = "processing_failed"


class ReviewState(StrEnum):
    """What an authorised officer decided about a candidate."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CORRECTED = "corrected"
    NOT_APPLICABLE = "not_applicable"


class LegalOutcome(StrEnum):
    """Result of applying an approved rule version to reviewed evidence."""

    COMPLIANT = "compliant"
    NON_COMPLIANT = "non_compliant"
    UNABLE_TO_DETERMINE = "unable_to_determine"
    NOT_APPLICABLE = "not_applicable"
    ADDITIONAL_EVIDENCE_REQUIRED = "additional_evidence_required"


class RuleStatus(StrEnum):
    DRAFT = "draft"
    UNDER_REVIEW = "under_review"
    CHANGES_REQUIRED = "changes_required"
    APPROVED = "approved"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


#: Only these statuses may influence an inspection outcome.
EFFECTIVE_RULE_STATUSES = frozenset({RuleStatus.APPROVED, RuleStatus.ACTIVE})


class ComplaintState(StrEnum):
    RECEIVED = "received"
    TRIAGED = "triaged"
    DUPLICATE = "duplicate"
    ASSIGNED = "assigned"
    INSPECTION_CREATED = "inspection_created"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    CLOSED = "closed"


class ComplaintCategory(StrEnum):
    MISSING_DECLARATION = "missing_declaration"
    MRP_OVERCHARGE = "mrp_overcharge"
    INCORRECT_QUANTITY = "incorrect_quantity"
    UNREADABLE_LABEL = "unreadable_label"
    MISLEADING_CLAIM = "misleading_claim"
    EXPIRED_OR_DATE_ISSUE = "expired_or_date_issue"
    ALLERGEN_INFORMATION = "allergen_information"
    UNSAFE_PRODUCT = "unsafe_product"
    OTHER = "other"


#: Categories that receive a higher triage priority because of consumer safety.
SAFETY_PRIORITY_CATEGORIES = frozenset(
    {ComplaintCategory.UNSAFE_PRODUCT, ComplaintCategory.ALLERGEN_INFORMATION}
)


class CaseState(StrEnum):
    DRAFT = "draft"
    NOTICE_ISSUED = "notice_issued"
    AWAITING_RESPONSE = "awaiting_response"
    RESPONSE_RECEIVED = "response_received"
    HEARING_SCHEDULED = "hearing_scheduled"
    UNDER_CONSIDERATION = "under_consideration"
    FOLLOW_UP_INSPECTION = "follow_up_inspection"
    RESOLVED = "resolved"
    WITHDRAWN = "withdrawn"
    CLOSED = "closed"


class NoticeType(StrEnum):
    SHOW_CAUSE = "show_cause"
    COMPLIANCE_ADVISORY = "compliance_advisory"
    DEMAND_FOR_INFORMATION = "demand_for_information"
    HEARING_NOTICE = "hearing_notice"


class DeliveryMethod(StrEnum):
    HAND_DELIVERY = "hand_delivery"
    REGISTERED_POST = "registered_post"
    SPEED_POST = "speed_post"
    EMAIL = "email"
    COURIER = "courier"
    PUBLIC_NOTICE = "public_notice"


class ReportState(StrEnum):
    ISSUED = "issued"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


class InspectionSource(StrEnum):
    FIELD_INSPECTION = "field_inspection"
    RETAIL_STORE = "retail_store"
    WAREHOUSE = "warehouse"
    COMPLAINT = "complaint"
    ECOMMERCE_LISTING = "ecommerce_listing"
    IMPORT_CONSIGNMENT = "import_consignment"
    MARKET_SURVEILLANCE = "market_surveillance"


class PackageType(StrEnum):
    POUCH = "pouch"
    CARTON = "carton"
    BOTTLE = "bottle"
    CAN = "can"
    JAR = "jar"
    SACHET = "sachet"
    TUBE = "tube"
    WRAPPER = "wrapper"
    BOX = "box"
    SACK = "sack"
    OTHER = "other"


class QuantityKind(StrEnum):
    """Dimension of the net quantity declaration."""

    WEIGHT = "weight"
    VOLUME = "volume"
    LENGTH = "length"
    AREA = "area"
    COUNT = "count"


def values(enum_cls: type[StrEnum]) -> tuple[str, ...]:
    """Tuple of raw values, used to build database CHECK constraints."""
    return tuple(member.value for member in enum_cls)
