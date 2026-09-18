"""Schemas shared across the API."""

from __future__ import annotations

from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

ItemT = TypeVar("ItemT")

#: Page sizes are capped so a single request cannot pull an entire register.
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


class Schema(BaseModel):
    """Base for every request and response model.

    ``extra="forbid"`` on request bodies is what prevents mass assignment: an
    unexpected field is a 422 rather than being silently ignored or, worse,
    reaching a model attribute.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReadSchema(BaseModel):
    """Base for response models built from ORM objects."""

    model_config = ConfigDict(from_attributes=True)


class ErrorBody(BaseModel):
    code: str = Field(examples=["validation_failed"])
    message: str = Field(examples=["The submitted values are not acceptable."])
    details: dict[str, Any] | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """The single error shape returned by every endpoint."""

    error: ErrorBody


class PageMeta(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool
    has_previous: bool


class Page(BaseModel, Generic[ItemT]):
    items: list[ItemT]
    meta: PageMeta

    @classmethod
    def build(cls, items: list[ItemT], *, page: int, page_size: int, total: int) -> Page[ItemT]:
        total_pages = max(1, -(-total // page_size)) if page_size else 1
        return cls(
            items=items,
            meta=PageMeta(
                page=page,
                page_size=page_size,
                total=total,
                total_pages=total_pages,
                has_next=page < total_pages,
                has_previous=page > 1,
            ),
        )


PageNumber = Annotated[int, Field(ge=1, le=10_000)]
PageSize = Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)]

#: Optimistic-lock field. Clients echo the version they loaded; a mismatch is 409.
ExpectedVersion = Annotated[
    int,
    Field(
        ge=1,
        description=(
            "Version of the record as loaded by the client. If the record has "
            "changed since, the request is rejected with 409 stale_record."
        ),
    ),
]


class Acknowledgement(Schema):
    """Minimal response for actions with no useful body."""

    status: str = "ok"
    message: str | None = None


class TransitionRequest(Schema):
    """Request a state change on a record with a state machine."""

    target_state: str = Field(min_length=2, max_length=40)
    reason: str | None = Field(default=None, max_length=2000)
    expected_version: ExpectedVersion


class AvailableTransition(BaseModel):
    target_state: str
    label: str
    reason_required: bool
    blocked_by: list[str] = Field(default_factory=list)


class TimelineEntry(BaseModel):
    at: Any
    kind: str
    summary: str
    actor: str | None = None
    actor_role: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
