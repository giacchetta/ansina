"""`create_app()` — the FastAPI application factory. See issue #4.

Boots with zero business logic: health/readiness/version routes, request-id middleware,
an error spine that maps every failure (`AnsinaError`, HTTP errors, validation errors,
unhandled exceptions) to `application/problem+json`, and the SQLite connection +
migration lifecycle from issue #6. Every later endpoint plugs into this shape rather
than growing its own.

Issue #25 adds one more mandatory step at the end of assembly: `audit_route_coverage`
walks every registered route and refuses to build the app at all if any non-public one
lacks a `require(...)` authorization declaration — the same "fail loudly before uvicorn
binds a port" pattern `HeartUnavailableError`/`BrainUnavailableError` already use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
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
from ansina.api.route_audit import audit_route_coverage
from ansina.api.routes.health import router as health_router
from ansina.api.routes.heart import router as heart_router
from ansina.api.routes.openapi import router as openapi_router
from ansina.auth import ensure_bootstrap_admin, reconcile_builtin_roles, sync_resources
from ansina.brain import BrainProvider, build_brain_provider
from ansina.config import Settings, load_settings
from ansina.errors import AnsinaError
from ansina.heart import (
    HeartRuntime,
    TickLifecycle,
    build_heart_runtime,
    build_tick_loop,
)
from ansina.logging import get_logger
from ansina.storage import Database, run_migrations

logger = get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    heart_factory: Callable[[Settings], HeartRuntime] = build_heart_runtime,
    tick_loop_factory: Callable[
        [Settings, HeartRuntime], TickLifecycle
    ] = build_tick_loop,
    brain_factory: Callable[[Settings], BrainProvider] = build_brain_provider,
) -> FastAPI:
    """Build the FastAPI application. Loads `Settings` via `load_settings()` when none
    is given — tests and `python -m ansina` both pass an already-loaded one instead, so
    config is loaded exactly once per process.

    `heart_factory` defaults to `ansina.heart.build_heart_runtime`; tests inject a fake
    so the unit suite never needs a real model or MLX installed. When
    `settings.heart.enabled` is `False` (the default), it's never called at all — no
    probe runs, and `app.state.heart` is `None`. `tick_loop_factory` (default
    `ansina.heart.build_tick_loop`, issue #11) follows the same shape, gated by both
    `heart.enabled` and `heart.tick.enabled` — `app.state.tick_loop` is `None` unless
    both are true. `brain_factory` (default `ansina.brain.build_brain_provider`, issue
    #12) follows the same shape again, gated by `brain.enabled` alone — the Brain has
    no dependency on the Heart being enabled. Nothing calls `BrainProvider.stream()`
    yet (the tick loop's `escalate` branch stays log-only until a follow-up issue wires
    it up), so `app.state.brain` exists only for that future consumer to reach.
    """
    resolved_settings = settings if settings is not None else load_settings()
    readiness = Readiness()
    db = Database(resolved_settings.database.path)

    # Built here, not inside `lifespan`, so a `HeartUnavailableError` (issue #10) is
    # raised while the app is still being assembled — before uvicorn ever binds a
    # port — the same "fail loudly at boot" shape a `ConfigError` already has.
    heart: HeartRuntime | None = None
    if resolved_settings.heart.enabled:
        heart = heart_factory(resolved_settings)

    tick_loop: TickLifecycle | None = None
    if heart is not None and resolved_settings.heart.tick.enabled:
        tick_loop = tick_loop_factory(resolved_settings, heart)

    # Same "fail loudly before uvicorn binds a port" shape as `heart` above —
    # `BrainUnavailableError` (issue #12) surfaces here, not on the first `stream()`
    # call. Independent of `heart.enabled`: the Brain has no dependency on the Heart.
    brain: BrainProvider | None = None
    if resolved_settings.brain.enabled:
        brain = brain_factory(resolved_settings)

    if not resolved_settings.security.enabled:
        logger.warning(
            "ansina starting with security.enabled = false — every route except "
            "/healthz and /readyz is reachable with no authentication"
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("ansina starting up")
        db.connect()
        run_migrations(db)
        # RBAC identity/permission foundation (issue #24, catalog source replaced by
        # #25): catalog the resources the route-coverage audit already extracted below,
        # reconcile the builtin roles' grants against that catalog, then resolve the
        # configured api_token to a bootstrap Admin identity — in that order, since a
        # role can't be granted a resource that isn't catalogued yet, and the bootstrap
        # identity can't be assigned the "admin" role before it exists. No dedicated
        # `/readyz` check: `database` already covers the only failure mode this could
        # have, the same reasoning already recorded for why the Brain has none.
        sync_resources(db, _app.state.resource_specs)
        reconcile_builtin_roles(db)
        ensure_bootstrap_admin(db, resolved_settings)
        readiness.register("database", db.is_healthy)
        if heart is not None:
            heart.load()
            readiness.register("heart", heart.is_healthy)
            if tick_loop is not None:
                await tick_loop.start()
                readiness.register("heart_tick", tick_loop.is_healthy)
        # No `/readyz` check for the Brain: unlike `Database.is_healthy`/`HeartRuntime.
        # is_healthy` (both a cheap local check), the only meaningful liveness signal
        # for a remote provider is a real network round-trip, and paying for one on
        # every `/readyz` poll is the wrong trade. `brain is not None` already tells an
        # operator whether it's configured; issue #12 doesn't ask for more than that.
        readiness.register("startup", lambda: True)
        try:
            yield
        finally:
            logger.info("ansina shutting down")
            if tick_loop is not None:
                await tick_loop.stop()
            if heart is not None:
                heart.unload()
            if brain is not None:
                await brain.aclose()
            db.close()

    app = FastAPI(
        title="Ansina",
        version=__version__,
        lifespan=lifespan,
        # FastAPI's default `/openapi.json`/`/docs`/`/redoc`/`/docs/oauth2-redirect`
        # are plain Starlette routes that can't carry a `require(...)` dependency, so
        # the route-coverage audit below could never see them — and `/docs`/`/redoc`
        # are non-functional with auth enabled regardless (no `fastapi.security`
        # scheme is declared for Swagger UI's "Authorize" button, since auth here is
        # middleware-level). `routes/openapi.py` serves `/openapi.json` as a real,
        # gated `APIRoute` instead; nothing re-serves the HTML viewers.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = resolved_settings
    app.state.readiness = readiness
    app.state.db = db
    app.state.heart = heart
    app.state.tick_loop = tick_loop
    app.state.brain = brain

    # `add_middleware` inserts at the front of the stack, so registration order is
    # reversed at request time: the last one added runs outermost. BearerAuthMiddleware
    # goes first (innermost) and RequestIdMiddleware last (outermost) so a rejected
    # request still gets a request id bound in context — a 401's `problem+json` body
    # carries a real `request_id`, the response still echoes `X-Request-ID`, and the
    # "request completed" access-log line still fires for it.
    app.add_middleware(
        BearerAuthMiddleware, enabled=resolved_settings.security.enabled, db=db
    )
    app.add_middleware(RequestIdMiddleware)

    app.add_exception_handler(AnsinaError, ansina_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    app.include_router(health_router)
    app.include_router(heart_router)
    app.include_router(openapi_router)

    # Issue #25: refuses to boot (`RouteCoverageError`, before uvicorn ever binds a
    # port — same "fail loudly" shape as `HeartUnavailableError`) if any non-public
    # route above lacks a `require(...)` declaration. The surviving declarations are
    # also the `resources` catalog's entire source (see `lifespan`'s `sync_resources`
    # call), replacing issue #24's hand-written `BOOTSTRAP_RESOURCES` seed.
    app.state.resource_specs = audit_route_coverage(app)

    return app
