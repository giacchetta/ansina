"""`SudoService` — everything above the `StepUpVerifier` port (issue #26): grant
issuance, TTL, revocation, and failed-attempt lockout. Verifier-agnostic by
construction — this module never imports or names `PasswordStepUpVerifier`, only
`auth.step_up.StepUpRegistry`, so a follow-up milestone's second verifier changes
nothing here.

`api.routes.sudo` is the one HTTP-facing caller; `api.auth.BearerAuthMiddleware` is the
other, calling `resolve()` to elevate a `Principal` when a request carries a live
`X-Sudo-Token`.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

from ansina.auth.clock import Clock, iso, parse_iso, utc_now
from ansina.auth.repositories import SudoGrantRepository, SudoLockoutRepository
from ansina.auth.step_up import StepUpRegistry, build_step_up_verifiers
from ansina.errors import AuthError
from ansina.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from ansina.auth.models import SudoGrant, SudoLockout
    from ansina.auth.principal import Principal
    from ansina.auth.step_up import StepUpVerifier
    from ansina.config.settings import Settings, SudoSettings
    from ansina.storage.database import Database

logger = get_logger(__name__)

# 32 raw bytes -> 43 base64url characters, the same generation shape
# `auth.bootstrap`'s bootstrap token already uses.
_GRANT_TOKEN_BYTES = 32


class SudoLockedOutError(AuthError):
    """Too many consecutive failed step-up attempts for this user — further attempts
    are refused until the cooldown window (`details["retry_after_seconds"]`) elapses.
    Mapped to 429, not 401: the caller is already authenticated, so disclosing the
    lockout itself leaks nothing a wrong-password 401 wouldn't already suggest.
    """

    code: ClassVar[str] = "ansina.auth.sudo_locked_out"


class StepUpUnavailableError(AuthError):
    """`principal` has no usable step-up factor to resolve against (issue #37) — one
    of three causes, distinguished only by `details["available_factors"]` and the
    message, not by a separate code each: zero enrolled factors at all (the default
    shape of any user created without a password), a requested `payload["factor"]`
    that names a verifier the caller isn't enrolled in, or 2+ enrolled factors with no
    `factor` given to disambiguate. Raised *before* `_record_failure` ever runs, so
    none of the three consumes a failed-attempt lockout slot — a caller who could never
    have succeeded shouldn't be locked out as if they'd guessed wrong. Mapped to 403,
    same family as `ForbiddenError`/`SudoRequiredError`: the caller is authenticated,
    just not equipped to step up this way.
    """

    code: ClassVar[str] = "ansina.auth.step_up_unavailable"


@dataclass(frozen=True, slots=True)
class IssuedGrant:
    """The result of a successful `SudoService.step_up()` call. `token` is the raw,
    one-time-visible grant value — never stored, never logged; only `grant_id` is.
    """

    token: str
    grant_id: str
    verifier: str
    expires_at: str


class SudoService:
    """Issues, checks, and revokes sudo grants; enforces the failed-attempt lockout.
    Constructed once in `create_app` and stashed on `app.state.sudo`.
    """

    def __init__(
        self,
        db: Database,
        settings: SudoSettings,
        *,
        registry: StepUpRegistry,
        clock: Clock = utc_now,
    ) -> None:
        self._grants = SudoGrantRepository(db)
        self._lockouts = SudoLockoutRepository(db)
        self._settings = settings
        self._registry = registry
        self._clock = clock

    def _active_lockout(
        self, lockout: SudoLockout | None, now: datetime
    ) -> SudoLockout | None:
        """`lockout` if it still names a `locked_until` in the future, else `None` —
        an expired lockout is treated as if it were never recorded.
        """
        if lockout is None or lockout.locked_until is None:
            return None
        return lockout if parse_iso(lockout.locked_until) > now else None

    def step_up(
        self, principal: Principal, payload: Mapping[str, Any]
    ) -> IssuedGrant | None:
        """Verify `payload` against the registry's resolved verifier for `principal`.

        Raises `SudoLockedOutError` if this user is currently locked out (without
        consuming another attempt), checked first since a locked-out caller is
        refused regardless of what factors they hold. Raises `StepUpUnavailableError`
        (issue #37) if no usable factor can be resolved — also without consuming an
        attempt, since the caller could never have succeeded here. Returns `None` on
        a failed verification (the route maps that to 401) — records the failure and
        locks the user out once `max_failed_attempts` is reached within
        `attempt_window_seconds`. Returns the `IssuedGrant` on success, after clearing
        any lockout state.
        """
        user_id = principal.user.id
        now = self._clock()
        existing = self._active_lockout(self._lockouts.get(user_id), now)
        if existing is not None:
            assert existing.locked_until is not None  # narrowed by _active_lockout
            retry_after = (parse_iso(existing.locked_until) - now).total_seconds()
            logger.warning(
                "sudo step-up refused — user is locked out",
                extra={"actor": principal.actor, "user_id": user_id},
            )
            raise SudoLockedOutError(
                f"{principal.actor!r} is locked out of sudo step-up",
                details={"retry_after_seconds": max(0.0, retry_after)},
            )

        verifier = self._resolve_factor(principal, payload)
        if not verifier.verify(principal, payload):
            self._record_failure(user_id, now)
            logger.warning(
                "sudo step-up denied",
                extra={
                    "actor": principal.actor,
                    "user_id": user_id,
                    "verifier": verifier.name,
                },
            )
            return None

        self._lockouts.clear(user_id)
        token = secrets.token_urlsafe(_GRANT_TOKEN_BYTES)
        expires_at = now + timedelta(seconds=self._settings.ttl_seconds)
        grant = self._grants.create(
            user_id,
            token,
            verifier.name,
            issued_at=iso(now),
            expires_at=iso(expires_at),
        )
        logger.info(
            "sudo step-up granted",
            extra={
                "actor": principal.actor,
                "user_id": user_id,
                "verifier": verifier.name,
                "grant_id": grant.id,
            },
        )
        return IssuedGrant(
            token=token,
            grant_id=grant.id,
            verifier=verifier.name,
            expires_at=grant.expires_at,
        )

    def _resolve_factor(
        self, principal: Principal, payload: Mapping[str, Any]
    ) -> StepUpVerifier:
        """Pick which of `principal`'s enrolled verifiers `payload` targets (issue
        #37). A non-empty string `payload["factor"]` matches by `verifier.name`;
        absent `factor` with exactly one enrolled verifier uses it (M2's existing
        password-only shape keeps working with a bare `{"password": ...}` body — no
        `factor` key required). Anything else — zero enrolled, an unrecognized
        `factor`, or 2+ enrolled with no `factor` to disambiguate — raises
        `StepUpUnavailableError` without ever calling `verify()`.
        """
        factors = self._registry.for_principal(principal)
        requested = payload.get("factor")
        available = [f.name for f in factors]

        if isinstance(requested, str) and requested:
            for verifier in factors:
                if verifier.name == requested:
                    return verifier
            logger.warning(
                "sudo step-up refused — unrecognized factor",
                extra={
                    "actor": principal.actor,
                    "user_id": principal.user.id,
                    "factor": requested,
                },
            )
            raise StepUpUnavailableError(
                f"{principal.actor!r} holds no step-up factor named {requested!r}",
                details={"available_factors": available},
            )

        if len(factors) == 1:
            return factors[0]

        logger.warning(
            "sudo step-up refused — no usable step-up factor",
            extra={
                "actor": principal.actor,
                "user_id": principal.user.id,
                "available_factors": available,
            },
        )
        detail = (
            f"{principal.actor!r} has no enrolled step-up factor"
            if not factors
            else f"{principal.actor!r} must specify which of {available} to use"
        )
        raise StepUpUnavailableError(detail, details={"available_factors": available})

    def enrolled_factors(self, principal: Principal) -> list[str]:
        """The verifier names `principal` could currently succeed with — backs `GET
        /auth/me`'s `step_up_factors` (issue #37). Never raises.
        """
        return [v.name for v in self._registry.for_principal(principal)]

    def _record_failure(self, user_id: str, now: datetime) -> None:
        current = self._lockouts.get(user_id)
        window = timedelta(seconds=self._settings.attempt_window_seconds)
        if current is not None and current.first_failed_at is not None:
            first_failed_at = parse_iso(current.first_failed_at)
        else:
            first_failed_at = now

        if now - first_failed_at > window:
            # The window since the first failure in this streak has elapsed — start a
            # fresh streak rather than accumulating against a long-past attempt.
            failed_count = 1
            first_failed_at = now
        else:
            failed_count = (current.failed_count if current is not None else 0) + 1

        locked_until: datetime | None = None
        if failed_count >= self._settings.max_failed_attempts:
            locked_until = now + timedelta(seconds=self._settings.lockout_seconds)
            logger.warning(
                "sudo step-up locked out",
                extra={"user_id": user_id, "failed_count": failed_count},
            )

        self._lockouts.set(
            user_id,
            failed_count=failed_count,
            first_failed_at=iso(first_failed_at),
            locked_until=iso(locked_until) if locked_until is not None else None,
        )

    def resolve(self, user_id: str, token: str) -> SudoGrant | None:
        """The read side `BearerAuthMiddleware` calls: the caller's own live grant,
        or `None` — never raises, an absent/expired/wrong grant simply fails to
        elevate rather than rejecting the request outright (see `api.auth`'s
        docstring for why that's deliberate).
        """
        return self._grants.find_active(user_id, token, now=iso(self._clock()))

    def revoke_for_user(self, user_id: str) -> None:
        """`DELETE /auth/sudo` — the caller stepping back down deliberately."""
        self._grants.revoke_for_user(user_id, now=iso(self._clock()))

    def revoke_all(self) -> None:
        """The break-glass path (`DELETE /auth/sudo/grants`) — revokes every user's
        active grant, including the caller's own.
        """
        self._grants.revoke_all(now=iso(self._clock()))


def build_sudo_service(db: Database, settings: Settings) -> SudoService:
    """The default `SudoService` factory — wires `build_step_up_verifiers` into a
    `StepUpRegistry` and reads `[security.sudo]`. `create_app` calls this once and
    hands the result to both `BearerAuthMiddleware` and `api.routes.sudo`.
    """
    registry = StepUpRegistry(build_step_up_verifiers(db, settings))
    return SudoService(db, settings.security.sudo, registry=registry)
