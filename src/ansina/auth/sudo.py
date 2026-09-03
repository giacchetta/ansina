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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

from ansina.auth.repositories import SudoGrantRepository, SudoLockoutRepository
from ansina.auth.step_up import StepUpRegistry, build_step_up_verifiers
from ansina.errors import AuthError
from ansina.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from ansina.auth.models import SudoGrant, SudoLockout
    from ansina.auth.principal import Principal
    from ansina.config.settings import Settings, SudoSettings
    from ansina.storage.database import Database

logger = get_logger(__name__)

# 32 raw bytes -> 43 base64url characters, the same generation shape
# `auth.bootstrap`'s bootstrap token already uses.
_GRANT_TOKEN_BYTES = 32

Clock = Callable[[], datetime]


class SudoLockedOutError(AuthError):
    """Too many consecutive failed step-up attempts for this user — further attempts
    are refused until the cooldown window (`details["retry_after_seconds"]`) elapses.
    Mapped to 429, not 401: the caller is already authenticated, so disclosing the
    lockout itself leaks nothing a wrong-password 401 wouldn't already suggest.
    """

    code: ClassVar[str] = "ansina.auth.sudo_locked_out"


@dataclass(frozen=True, slots=True)
class IssuedGrant:
    """The result of a successful `SudoService.step_up()` call. `token` is the raw,
    one-time-visible grant value — never stored, never logged; only `grant_id` is.
    """

    token: str
    grant_id: str
    verifier: str
    expires_at: str


def _iso(dt: datetime) -> str:
    """Millisecond-precision ISO 8601 UTC, matching `0002_rbac.sql`'s
    `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')` column defaults closely enough that the
    two sort identically as text — `SudoGrantRepository.find_active`'s `expires_at > ?`
    comparison depends on that.
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
        clock: Clock = _utc_now,
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
        return lockout if _parse_iso(lockout.locked_until) > now else None

    def step_up(
        self, principal: Principal, payload: Mapping[str, Any]
    ) -> IssuedGrant | None:
        """Verify `payload` against the registry's resolved verifier for `principal`.

        Raises `SudoLockedOutError` if this user is currently locked out (without
        consuming another attempt). Returns `None` on a failed verification (the
        route maps that to 401) — records the failure and locks the user out once
        `max_failed_attempts` is reached within `attempt_window_seconds`. Returns the
        `IssuedGrant` on success, after clearing any lockout state.
        """
        user_id = principal.user.id
        now = self._clock()
        existing = self._active_lockout(self._lockouts.get(user_id), now)
        if existing is not None:
            assert existing.locked_until is not None  # narrowed by _active_lockout
            retry_after = (_parse_iso(existing.locked_until) - now).total_seconds()
            logger.warning(
                "sudo step-up refused — user is locked out",
                extra={"actor": principal.actor, "user_id": user_id},
            )
            raise SudoLockedOutError(
                f"{principal.actor!r} is locked out of sudo step-up",
                details={"retry_after_seconds": max(0.0, retry_after)},
            )

        verifier = self._registry.for_principal(principal)
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
            issued_at=_iso(now),
            expires_at=_iso(expires_at),
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

    def _record_failure(self, user_id: str, now: datetime) -> None:
        current = self._lockouts.get(user_id)
        window = timedelta(seconds=self._settings.attempt_window_seconds)
        if current is not None and current.first_failed_at is not None:
            first_failed_at = _parse_iso(current.first_failed_at)
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
            first_failed_at=_iso(first_failed_at),
            locked_until=_iso(locked_until) if locked_until is not None else None,
        )

    def resolve(self, user_id: str, token: str) -> SudoGrant | None:
        """The read side `BearerAuthMiddleware` calls: the caller's own live grant,
        or `None` — never raises, an absent/expired/wrong grant simply fails to
        elevate rather than rejecting the request outright (see `api.auth`'s
        docstring for why that's deliberate).
        """
        return self._grants.find_active(user_id, token, now=_iso(self._clock()))

    def revoke_for_user(self, user_id: str) -> None:
        """`DELETE /auth/sudo` — the caller stepping back down deliberately."""
        self._grants.revoke_for_user(user_id, now=_iso(self._clock()))

    def revoke_all(self) -> None:
        """The break-glass path (`DELETE /auth/sudo/grants`) — revokes every user's
        active grant, including the caller's own.
        """
        self._grants.revoke_all(now=_iso(self._clock()))


def build_sudo_service(db: Database, settings: Settings) -> SudoService:
    """The default `SudoService` factory — wires `build_step_up_verifiers` into a
    `StepUpRegistry` and reads `[security.sudo]`. `create_app` calls this once and
    hands the result to both `BearerAuthMiddleware` and `api.routes.sudo`.
    """
    registry = StepUpRegistry(build_step_up_verifiers(db, settings))
    return SudoService(db, settings.security.sudo, registry=registry)
