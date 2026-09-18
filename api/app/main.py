"""Application entry point.

Middleware order matters and is applied outermost-first:

1. ``TrustedHostMiddleware``  - reject unexpected Host headers
2. ``CORSMiddleware``         - answer preflight before anything else runs
3. request context            - assign a request id, log, record metrics
4. security headers           - applied to every response including errors
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .api.health import router as health_router
from .api.v1.router import router as v1_router
from .config import get_settings
from .db import dispose_engines
from .errors import (
    ConflictError,
    MaanakError,
    MalformedRequestError,
    NotFoundError,
    StaleRecordError,
    ValidationError,
)
from .observability import (
    HTTP_LATENCY,
    HTTP_REQUESTS,
    configure_logging,
    get_logger,
    new_request_id,
    request_context,
)
from .schemas.common import ErrorResponse
from .security.ratelimit import close_client as close_redis

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

DESCRIPTION = """
Maanak records inspections of packaged commodities against the Legal Metrology
(Packaged Commodities) Rules, 2011.

Every legal finding cites an approved rule version, references the evidence it was
drawn from, and carries the identity of the officer who reviewed it. Machine
readings and officer decisions are stored separately and never merged.

**Authentication.** Browser sessions use `HttpOnly` cookies. Sign in at
`POST /api/v1/auth/sign-in`, then send the `maanak_csrf` cookie value in an
`X-CSRF-Token` header on every unsafe request. A bearer header is not accepted.

**This is not a government service.** Maanak is not affiliated with, endorsed by or
certified by any authority, and it does not issue enforcement action on its own.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(json_output=settings.environment != "development")

    if settings.is_production:
        # Refuse to start with a configuration that is unsafe in production.
        settings.assert_production_ready()

    logger.info(
        "api_starting",
        version=__version__,
        environment=settings.environment,
        docs_enabled=settings.docs_enabled,
    )

    # Object storage buckets are created on demand so a fresh deployment needs no
    # manual setup step. A storage outage is reported by readiness, not a crash.
    try:
        from .services.storage import ensure_buckets

        await ensure_buckets()
    except Exception as exc:  # noqa: BLE001 - readiness reports the detail
        logger.warning("storage_not_ready_at_startup", error=type(exc).__name__)

    yield

    await close_redis()
    await dispose_engines()
    logger.info("api_stopped")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Maanak API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        responses={
            400: {"model": ErrorResponse, "description": "Malformed request"},
            401: {"model": ErrorResponse, "description": "Not authenticated"},
            403: {"model": ErrorResponse, "description": "Permission denied"},
            404: {"model": ErrorResponse, "description": "Not found"},
            409: {"model": ErrorResponse, "description": "Conflict"},
            422: {"model": ErrorResponse, "description": "Unprocessable content"},
            429: {"model": ErrorResponse, "description": "Rate limited"},
        },
    )

    if settings.trusted_host_list != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID", "Idempotency-Key"],
        expose_headers=[REQUEST_ID_HEADER],
        max_age=600,
    )

    _register_middleware(app)
    _register_error_handlers(app)

    app.include_router(health_router)
    app.include_router(v1_router, prefix="/api/v1")

    return app


def _register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def context_and_logging(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        inbound = request.headers.get(REQUEST_ID_HEADER, "")
        # Only accept a client-supplied id if it is a plausible identifier, so it
        # cannot be used to inject content into logs.
        request_id = (
            inbound
            if inbound and len(inbound) <= 64 and inbound.replace("-", "").isalnum()
            else new_request_id()
        )
        started = time.perf_counter()

        with request_context(request_id):
            try:
                response = await call_next(request)
            except Exception:
                elapsed = time.perf_counter() - started
                route = _route_label(request)
                logger.exception(
                    "request_failed",
                    method=request.method,
                    route=route,
                    path=request.url.path,
                    duration_ms=round(elapsed * 1000, 2),
                )
                HTTP_REQUESTS.labels(request.method, route, "500").inc()
                HTTP_LATENCY.labels(request.method, route).observe(elapsed)
                response = JSONResponse(
                    status_code=500,
                    content={
                        "error": {
                            "code": "internal_error",
                            "message": "Something went wrong. The reference below "
                            "identifies this request in the server log.",
                            "request_id": request_id,
                        }
                    },
                )
                response.headers[REQUEST_ID_HEADER] = request_id
                _apply_security_headers(response, request)
                return response

            elapsed = time.perf_counter() - started
            route = _route_label(request)
            HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
            HTTP_LATENCY.labels(request.method, route).observe(elapsed)

            # Health checks would otherwise dominate the log.
            if not request.url.path.startswith(("/health", "/metrics")):
                logger.info(
                    "request",
                    method=request.method,
                    route=route,
                    path=request.url.path,
                    status=response.status_code,
                    duration_ms=round(elapsed * 1000, 2),
                )

            response.headers[REQUEST_ID_HEADER] = request_id
            _apply_security_headers(response, request)
            return response


def _route_label(request: Request) -> str:
    """Templated route path, so metric labels stay bounded."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


def _apply_security_headers(response: Response, request: Request) -> None:
    settings = get_settings()
    headers = response.headers

    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
    headers.setdefault(
        "Permissions-Policy",
        "geolocation=(self), camera=(self), microphone=(), payment=(), usb=()",
    )
    headers.setdefault("Cache-Control", "no-store")

    # The API returns JSON only, so the strictest possible policy applies. The
    # browser application is served by nginx, which sets its own policy.
    if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
        headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )

    if settings.cookie_secure:
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


def _register_error_handlers(app: FastAPI) -> None:
    from .observability import current_request_id

    @app.exception_handler(MaanakError)
    async def handle_known_error(_request: Request, exc: MaanakError) -> JSONResponse:
        response = JSONResponse(
            status_code=exc.status_code,
            content=exc.to_payload(current_request_id()),
        )
        for name, value in exc.headers.items():
            response.headers[name] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """Report field problems without echoing the submitted values.

        Echoing input into an error body is how reflected content ends up in a
        response, so only the location and the rule are returned.
        """
        fields = []
        for error in exc.errors():
            location = [str(part) for part in error.get("loc", ()) if part != "body"]
            fields.append(
                {
                    "field": ".".join(location) or "body",
                    "problem": str(error.get("msg", "invalid"))[:200],
                    "type": str(error.get("type", "")),
                }
            )
        error = ValidationError("Some values could not be accepted.", details={"fields": fields})
        return JSONResponse(
            status_code=error.status_code, content=error.to_payload(current_request_id())
        )

    @app.exception_handler(StaleDataError)
    async def handle_stale(_request: Request, _exc: StaleDataError) -> JSONResponse:
        error = StaleRecordError()
        return JSONResponse(
            status_code=error.status_code, content=error.to_payload(current_request_id())
        )

    @app.exception_handler(IntegrityError)
    async def handle_integrity(_request: Request, exc: IntegrityError) -> JSONResponse:
        """Map a database constraint violation to a useful message.

        The driver's text is never returned: it can contain table names, column
        names and the offending values.
        """
        detail = str(getattr(exc, "orig", exc))
        if "unique" in detail.lower() or "duplicate" in detail.lower():
            error: MaanakError = ConflictError(
                "That record already exists.", code="duplicate_record"
            )
        elif "foreign key" in detail.lower():
            error = ValidationError("A referenced record does not exist.", code="invalid_reference")
        elif "check constraint" in detail.lower():
            error = ValidationError(
                "One of the submitted values is not permitted.", code="value_not_permitted"
            )
        else:
            error = ConflictError("The change could not be saved.", code="constraint_violation")
        logger.warning("integrity_error", code=error.code)
        return JSONResponse(
            status_code=error.status_code, content=error.to_payload(current_request_id())
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapping = {
            400: MalformedRequestError,
            404: NotFoundError,
            405: MalformedRequestError,
        }
        error_class = mapping.get(exc.status_code)
        if error_class is not None:
            error = error_class()
        else:
            error = MaanakError(
                str(exc.detail) if isinstance(exc.detail, str) else "Request failed.",
                code=f"http_{exc.status_code}",
            )
            error.status_code = exc.status_code
        return JSONResponse(
            status_code=error.status_code,
            content=error.to_payload(current_request_id()),
            headers=getattr(exc, "headers", None),
        )


app = create_app()
