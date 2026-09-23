"""`POST /auth/login` — exchange a local username + password for an ordinary,
bounded-lifetime `api_token`. See issue #50.

The third non-health-probe entry in `api/auth.py`'s `PUBLIC_PATHS`, alongside
`/auth/oidc/login`/`/auth/oidc/callback` (issue #43): a caller cannot hold a bearer
token before it has logged in, so this route must be reachable with no token even when
auth is enforced. It declares no `require(...)` authorization dependency —
`api.route_audit.audit_route_coverage` already skips every `PUBLIC_PATHS` entry, so
this contributes no `resources` catalog entry and `GET /auth/permissions` never lists
it, exactly like the two OIDC routes.

This is Ansina's **first unauthenticated, brute-forceable route beyond the two health
probes**, which drives two disciplines the rest of the codebase's `auth.*` surface
doesn't need:

- **One indistinguishable 401.** Unknown username, wrong password, a user with no
  password credential at all, an inactive user, a tombstoned user, and the bootstrap
  identity all produce the exact same `problem+json` body (`ansina.unauthorized`) —
  the same no-oracle discipline `BearerAuthMiddleware` already applies to a bad bearer
  token. There is exactly one `problem_response(...)` call site below so the body can
  never drift between failure modes. The bootstrap identity is refused explicitly,
  *before* `ansina.api.tokens.issue_token` would ever see it — that helper's own
  `assert_not_bootstrap_identity` raises a 403 `BootstrapIdentityError`, a
  distinguishable shape that would blow this discipline open for the one identity
  whose printed-once banner token must stay its only way in.
- **Equalized failure timing**, beyond what the issue's own written AC asks for but
  necessary to make the point above actually hold: a *value* difference in the 401
  body means nothing if the response *clock* still tells the two apart. Verifying a
  real password against a stored argon2id hash costs tens of milliseconds under the
  OWASP-baseline defaults; an unknown username (or any of the other refusals above)
  would otherwise short-circuit before argon2 ever runs, in roughly a thousandth of
  that time — a gap readable off one request, with no repeated sampling needed, that
  survives #49's throttle (enumeration needs only one probe per username; the throttle
  bounds guessing, not a single reconnaissance pass). Every refusal path below runs
  exactly one `ansina.auth.hashing.verify_password` call — a real one when a password
  credential exists, otherwise one against `hashing.dummy_password_hash`, discarded,
  paid purely for its cost.

Strict order of operations (the issue's own AC): `LoginThrottle.check` (#49) -> user
lookup -> active/tombstone/bootstrap-identity refusals -> `verify_password` (real or
dummy) -> `record_success`/`record_failure`. A failure is recorded for an unknown
username too — what keeps #49's "throttles identically whether or not the username
exists" property intact all the way through this route, not just at the throttle's own
layer.

Minted via `ansina.api.tokens.issue_token(..., ttl_seconds=..., clock=...)` — #39's
seam, its second HTTP-reachable caller after #43's OIDC route — with the throttle's own
injected clock (`LoginThrottle.clock`), the same "mint with the clock this request
already validated against" shape `routes/oidc.py`'s `_complete_login` uses. Token label
`"password login"`, so `GET /auth/me/tokens` distinguishes one at a glance, the same
trick `routes/oidc.py`'s `"oidc login"` label already uses.

Blocking work (the throttle's own sqlite reads, the user/credential lookups, and the
argon2 verify) is offloaded via a single `anyio.to_thread.run_sync` hop around the
whole synchronous sequence, like every other DB-touching route in this codebase.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ansina.api import tokens as tokens_api
from ansina.api.problems import CODE_UNAUTHORIZED, problem_response
from ansina.auth.bootstrap import is_bootstrap_identity
from ansina.auth.hashing import Argon2Params, dummy_password_hash, verify_password
from ansina.auth.models import CredentialType
from ansina.auth.repositories import CredentialRepository, UserRepository

if TYPE_CHECKING:
    from ansina.auth.login_throttle import LoginThrottle
    from ansina.config.settings import Settings
    from ansina.storage.database import Database

router = APIRouter(prefix="/auth")

# Visible in `GET /auth/me/tokens`/`GET /auth/users/{id}/tokens` so an operator can
# tell a password-login-minted token apart from a manually-issued or OIDC-minted one.
_TOKEN_LABEL = "password login"

# A fixed, non-error-leaking key for the rare clientless ASGI scope (`request.client
# is None` — a raw test-transport call or a nonstandard reverse proxy, never a real
# `TestClient`/uvicorn request, both of which always set `client`). Every such caller
# collapses into one throttle bucket rather than crashing the route.
_NO_CLIENT_IP_KEY = "unknown"

_UNAUTHORIZED_DETAIL = "Incorrect username or password."


class LoginRequest(BaseModel):
    """No pydantic-level `min_length` on either field — deliberately, mirroring
    `api.routes.users.SetPasswordRequest`'s own reasoning: an empty username or
    password must take the ordinary path to the one 401 below, never a distinct 422
    `ansina.request.invalid` shape that would itself be a (trivial) oracle.
    """

    username: str
    password: str


def _client_ip(request: Request) -> str:
    client = request.client
    return client.host if client is not None else _NO_CLIENT_IP_KEY


def _login(
    db: Database,
    settings: Settings,
    throttle: LoginThrottle,
    payload: LoginRequest,
    ip: str,
) -> tokens_api.IssuedTokenResponse | None:
    """The whole synchronous login sequence — see the module docstring for the
    ordering and timing-equalization discipline this implements. Returns the minted
    token on success, `None` on any failure (the route layer turns that into the one
    401 shape).
    """
    throttle.check(payload.username, ip)

    user = UserRepository(db).get_by_username(payload.username)
    refused = (
        user is None
        or not user.active
        or user.deleted_at is not None
        or is_bootstrap_identity(db, user.id)
    )

    credentials = CredentialRepository(db)
    params = Argon2Params.from_settings(settings)

    if refused:
        # No real password hash to check (or none we're willing to check against) —
        # pay a dummy verify's cost anyway, so this path takes the same time as a
        # real, wrong-password rejection below. See the module docstring's
        # "equalized failure timing" paragraph.
        verify_password(payload.password, dummy_password_hash(params), params)
        throttle.record_failure(payload.username, ip)
        return None

    assert user is not None  # narrowed by `refused` above being False
    if not credentials.has_credential(user.id, CredentialType.PASSWORD):
        verify_password(payload.password, dummy_password_hash(params), params)
        throttle.record_failure(payload.username, ip)
        return None

    if not credentials.verify_password(user.id, payload.password, params):
        throttle.record_failure(payload.username, ip)
        return None

    throttle.record_success(payload.username, ip)
    return tokens_api.issue_token(
        db,
        user.id,
        _TOKEN_LABEL,
        ttl_seconds=settings.security.login.token_ttl_seconds,
        clock=throttle.clock,
    )


@router.post(
    "/login",
    response_model=None,
    status_code=201,
    responses={
        201: {"model": tokens_api.IssuedTokenResponse},
        401: {
            "description": "Incorrect username or password — the single shape "
            "for every failure mode (unknown username, wrong password, a "
            "password-less/inactive/tombstoned user, or the bootstrap identity)."
        },
        429: {
            "description": "Too many failed attempts for this username or IP "
            "(issue #49) — retry after the returned `Retry-After`."
        },
    },
)
async def login(
    request: Request, payload: LoginRequest
) -> tokens_api.IssuedTokenResponse | JSONResponse:
    issued = await anyio.to_thread.run_sync(
        _login,
        request.app.state.db,
        request.app.state.settings,
        request.app.state.login_throttle,
        payload,
        _client_ip(request),
    )
    if issued is None:
        return problem_response(
            status=401,
            code=CODE_UNAUTHORIZED,
            title="Unauthorized",
            detail=_UNAUTHORIZED_DETAIL,
        )
    return issued
