"""`require(resource)` — the FastAPI dependency every non-public route declares. See
issue #25.

Every route wires this in as a route-level dependency (`dependencies=[Depends(require(
"heart.tick"))]`), never as a handler parameter — a handler's own signature stays about
its own inputs, not about who's allowed to call it. `ansina.api.route_audit` reads the
`ResourceDeclaration` this attaches to the returned closure to build the `resources`
catalog and to enforce that every route has one.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import anyio.to_thread
from fastapi import Request

from ansina.auth.authorization import ForbiddenError, SudoRequiredError, authorize
from ansina.auth.models import Verb
from ansina.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ansina.auth.principal import Principal
    from ansina.storage.database import Database

logger = get_logger(__name__)

_VERB_BY_METHOD: dict[str, Verb] = {verb.value: verb for verb in Verb}

# The attribute `route_audit.audit_route_coverage` looks for on a route's dependency
# callables — the marker that distinguishes a `require(...)` dependency from any other.
DECLARATION_ATTR = "ansina_resource"


class ResourceDeclaration:
    """What one `require(...)` call declared — carried on the returned dependency
    closure as `DECLARATION_ATTR`, read back by `route_audit` to build the `resources`
    catalog and to prove every non-public route has exactly one of these.
    """

    __slots__ = ("description", "name", "sensitive")

    def __init__(self, name: str, description: str, *, sensitive: bool) -> None:
        self.name = name
        self.description = description
        self.sensitive = sensitive


def require(
    resource: str, *, description: str = "", sensitive: bool = False
) -> Callable[[Request], Awaitable[None]]:
    """Build a fresh dependency gating `resource` — the verb is read from `request.
    method`, never declared per-route, so one `require()` call covers every HTTP
    method a route answers to. `sensitive=True` additionally requires a live sudo
    grant whenever the resolved role is `Maintain` (not `Admin`); the flag is inert
    until issue #26 ships a way to set `Principal.sudo_active`.
    """
    declaration = ResourceDeclaration(resource, description, sensitive=sensitive)

    async def _require(request: Request) -> None:
        settings = request.app.state.settings
        if not settings.security.enabled:
            # Dev mode: `BearerAuthMiddleware` let every request through with no
            # `Principal` resolved — there is nothing to authorize against.
            return

        principal: Principal | None = getattr(request.state, "principal", None)
        assert principal is not None, (
            "require() reached with no Principal on request.state — "
            "BearerAuthMiddleware must reject an unauthenticated request with 401 "
            "before any route dependency runs"
        )

        verb = _VERB_BY_METHOD.get(request.method)
        db: Database = request.app.state.db
        log_extra: dict[str, str] = {
            "actor": principal.actor,
            "resource": resource,
            "verb": request.method,
        }
        if principal.sudo_grant_id is not None:
            # Issue #26 AC: "every sensitive action taken under a grant is logged
            # with the grant id" — never the grant token itself.
            log_extra["sudo_grant_id"] = principal.sudo_grant_id
        if verb is None:
            # No route in this codebase answers a method outside Verb today (Starlette
            # itself 405s an unregistered method before any dependency runs) — this is
            # a fail-closed backstop, not a reachable path.
            logger.warning("authorization denied — unmapped verb", extra=log_extra)
            raise ForbiddenError(
                f"{request.method} is not an authorizable verb",
                details={"resource": resource, "verb": request.method},
            )

        try:
            await anyio.to_thread.run_sync(
                partial(authorize, db, principal, resource, verb, sensitive=sensitive)
            )
        except (ForbiddenError, SudoRequiredError) as exc:
            logger.warning(
                "authorization denied", extra={**log_extra, "code": exc.code}
            )
            raise
        logger.info("authorization granted", extra=log_extra)

    setattr(_require, DECLARATION_ATTR, declaration)
    return _require
