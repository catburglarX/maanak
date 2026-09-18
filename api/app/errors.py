"""Structured application errors.

Every failure returned to a client is one of these. The wire format is stable:

    {"error": {"code": "...", "message": "...", "details": {...},
               "request_id": "..."}}

Internal exception text is never placed in ``message``.
"""

from __future__ import annotations

from typing import Any


class MaanakError(Exception):
    """Base class for expected, reportable failures."""

    status_code: int = 400
    code: str = "bad_request"
    message: str = "The request could not be processed."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        if code:
            self.code = code
        self.details = details or {}
        self.headers = headers or {}
        super().__init__(self.message)

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        if request_id:
            payload["request_id"] = request_id
        return {"error": payload}


class ValidationError(MaanakError):
    status_code = 422
    code = "validation_failed"
    message = "The submitted values are not acceptable."


class MalformedRequestError(MaanakError):
    status_code = 400
    code = "malformed_request"
    message = "The request structure is not valid."


class AuthenticationError(MaanakError):
    status_code = 401
    code = "not_authenticated"
    message = "Sign in to continue."


class InvalidCredentialsError(AuthenticationError):
    code = "invalid_credentials"
    # Deliberately identical for unknown accounts and wrong passwords.
    message = "Email or password is incorrect."


class SessionExpiredError(AuthenticationError):
    code = "session_expired"
    message = "Your session has expired. Sign in again."


class AccountLockedError(MaanakError):
    status_code = 429
    code = "account_locked"
    message = "Too many sign-in attempts. Try again later."


class PermissionDeniedError(MaanakError):
    status_code = 403
    code = "permission_denied"
    message = "Your role does not permit this action."


class NotFoundError(MaanakError):
    """Also returned for records outside the caller's jurisdiction.

    Returning 404 rather than 403 avoids confirming that a record exists.
    """

    status_code = 404
    code = "not_found"
    message = "The requested record was not found."


class ConflictError(MaanakError):
    status_code = 409
    code = "conflict"
    message = "The request conflicts with the current state of the record."


class StaleRecordError(ConflictError):
    code = "stale_record"
    message = "This record changed after you loaded it. Reload and try again."


class InvalidTransitionError(ConflictError):
    code = "invalid_transition"
    message = "That state change is not allowed."


class GuardFailedError(ConflictError):
    code = "precondition_not_met"
    message = "A required step has not been completed."


class PayloadTooLargeError(MaanakError):
    status_code = 413
    code = "payload_too_large"
    message = "The uploaded file is larger than the permitted limit."


class UnsupportedMediaTypeError(MaanakError):
    status_code = 415
    code = "unsupported_media_type"
    message = "That file type is not accepted."


class RateLimitedError(MaanakError):
    status_code = 429
    code = "rate_limited"
    message = "Too many requests. Slow down and try again shortly."


class ServiceUnavailableError(MaanakError):
    status_code = 503
    code = "service_unavailable"
    message = "A required service is unavailable. Try again shortly."


class StorageError(MaanakError):
    status_code = 503
    code = "storage_unavailable"
    message = "Evidence storage is unavailable. The upload was not saved."


class ProcessingError(MaanakError):
    status_code = 422
    code = "processing_failed"
    message = "The evidence could not be analysed."


class OutboundFetchError(MaanakError):
    status_code = 422
    code = "fetch_rejected"
    message = "That address could not be retrieved."
