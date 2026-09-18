"""SQLAlchemy models.

Importing this package registers every table on ``Base.metadata``. Alembic's
``env.py`` imports it for autogenerate comparison, but released migrations never
import it: they contain explicit DDL so that historical migrations stay valid when
the models move on.
"""

from .audit import AuditEvent
from .base import Base
from .case_file import Case, CaseEvent, Notice, NoticeTemplate
from .complaint import Complaint, ComplaintAttachment
from .evidence import AnalysisJob, Evidence, EvidenceDerivative, OcrResult
from .finding import CandidateRevision, DeclarationCandidate, Finding
from .inspection import Inspection, InspectionFace, StateTransition
from .product import Product, ProductIdentifier, ProductMergeRecord, ResponsibleParty
from .report import Report, ReportDocument
from .rule import RuleReview, RuleTestRun, RuleVersion
from .user import LoginAttempt, PasswordResetToken, User, UserSession

__all__ = [
    "AnalysisJob",
    "AuditEvent",
    "Base",
    "CandidateRevision",
    "Case",
    "CaseEvent",
    "Complaint",
    "ComplaintAttachment",
    "DeclarationCandidate",
    "Evidence",
    "EvidenceDerivative",
    "Finding",
    "Inspection",
    "InspectionFace",
    "LoginAttempt",
    "Notice",
    "NoticeTemplate",
    "OcrResult",
    "PasswordResetToken",
    "Product",
    "ProductIdentifier",
    "ProductMergeRecord",
    "Report",
    "ReportDocument",
    "ResponsibleParty",
    "RuleReview",
    "RuleTestRun",
    "RuleVersion",
    "StateTransition",
    "User",
    "UserSession",
]
