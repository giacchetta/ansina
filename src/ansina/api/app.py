"""`create_app()` — the FastAPI application factory. See issue #4.

Boots with zero business logic: health/readiness/version routes, request-id middleware,
and an error spine that maps every failure (`AnsinaError`, HTTP errors, validation
errors, unhandled exceptions) to `application/problem+json`. Every later endpoint plugs
into this shape rather than growing its own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from ansina import __version__
from ansina.api.auth import BearerAuthMiddleware
from ansina.api.exception_handlers import (
    ansina_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
    validation_error_handler,
)
from ansina.api.middleware import RequestIdMiddleware
from ansina.api.readiness import Readiness
from ansina.api.routes.health import router as health_router
from ansina.config import Settings, load_settings
from ansina.errors import AnsinaError
from ansina.logging import get_logger

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application. Loads `Settings` via `load_settings()` when none
    is given — tests and `python -m ansina` both pass an already-loaded one instead, so
    config is loaded exactly once per process.
    """
    resolved_settings = settings if settings is not None else load_settings()
    readiness = Readiness()

    if resolved_settings.security.api_token is None:
        logger.warning(
            "ansina starting with no api_token configured — every route except "
            "/healthz and /readyz is reachable with no authentication"
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("ansina starting up")
        readiness.register("startup", lambda: True)
        try:
            yield
        finally:
            logger.info("ansina shutting down")

    app = FastAPI(
        title="Ansina",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.readiness = readiness

    # `add_middleware` inserts at the front of the stack, so registration order is
    # reversed at request time: the last one added runs outermost. BearerAuthMiddleware
    # goes first (innermost) and RequestIdMiddleware last (outermost) so a rejected
    # request still gets a request id bound in context — a 401's `problem+json` body
    # carries a real `request_id`, the response still echoes `X-Request-ID`, and the
    # "request completed" access-log line still fires for it.
    app.add_middleware(BearerAuthMiddleware, token=resolved_settings.security.api_token)
    app.add_middleware(RequestIdMiddleware)

    app.add_exception_handler(AnsinaError, ansina_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    app.include_router(health_router)

    return app
