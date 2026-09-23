"""`LoginThrottle` — per-username and per-IP brute-force protection for `POST
/auth/login` (#50). See issue #49.

Ships and is tested standalone, against a synthetic `(username, ip)` pair, with no
HTTP caller yet: #50's login route is the only intended caller, added in a follow-up
issue specifically so the throttle arithmetic is proven independent of any code path
that is also validating a password — the same "primitive first, its caller next"
sequencing #42 used for `auth.role_sync.sync_mapped_roles` ahead of #43.

Deliberately not built on `auth.sudo.SudoLockoutRepository`/`SudoService`'s lockout
half, even though the arithmetic below mirrors it closely: that table is keyed on
`user_id` and assumes an already-resolved caller stepping up. A login attempt has no
resolved user — an unknown username must throttle *identically* to a real one or the
throttle itself becomes a user-enumeration oracle, and an IP spraying many usernames
must be caught even though no single username bucket ever reaches its own threshold.
Different key, different threat model: new machinery, not a reuse.

Two known limitations, both deliberate and documented here rather than solved:

- No `X-Forwarded-For` / `[server] trusted_proxies` trust. Ansina binds loopback by
  default; a forwarded header is freely spoofable on a direct bind. Behind a reverse
  proxy, every request collapsing to one IP means this throttle fails *closed* (the
  proxy's own IP gets throttled sooner than intended), never open — a real proxy
  deployment is a follow-up issue's problem, not this one's.
- No throttling of authenticated routes — the bearer-token path is not
  brute-forceable at these odds (a 43-character machine secret, not a human-chosen
  password), so nothing there needs this machinery.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

from ansina.auth.clock import Clock, iso, parse_iso, utc_now
from ansina.auth.models import LoginAttemptScope
from ansina.auth.repositories import LoginAttemptRepository
from ansina.errors import AuthError
from ansina.logging import get_logger

if TYPE_CHECKING:
    from ansina.auth.models import LoginAttempt
    from ansina.config.settings import LoginSettings
    from ansina.storage.database import Database

logger = get_logger(__name__)


class LoginThrottledError(AuthError):
    """Either the submitted username's or the caller's IP's failure bucket is
    currently locked out — `POST /auth/login` (#50) is refused before a password is
    even checked. Mapped to 429, not 401: this is a rate-limiting concern about the
    *attempt*, not a statement about the credential itself, the same reasoning
    `SudoLockedOutError` uses for its own 429. `details["retry_after_seconds"]` is
    picked up by `api.exception_handlers`'s existing generic path (issue #26) with no
    handler-side special case, and by `tui/`'s `failures.report()` the same way.
    """

    code: ClassVar[str] = "ansina.auth.login_throttled"


def _active(attempt: LoginAttempt | None, now: datetime) -> LoginAttempt | None:
    """`attempt` if it still names a `locked_until` in the future, else `None` — an
    expired lockout is treated as if it were never recorded. Mirrors
    `SudoService._active_lockout` exactly.
    """
    if attempt is None or attempt.locked_until is None:
        return None
    return attempt if parse_iso(attempt.locked_until) > now else None


class LoginThrottle:
    """Checks and records login attempts against the per-username and per-IP buckets.
    Constructed directly (no `build_*` factory — there's nothing to assemble here, no
    registry, no HTTP client) by whatever wires it into `app.state` (#50).
    """

    def __init__(
        self, db: Database, settings: LoginSettings, *, clock: Clock = utc_now
    ) -> None:
        self._attempts = LoginAttemptRepository(db)
        self._settings = settings
        self._clock = clock

    @property
    def clock(self) -> Clock:
        """The injected clock this throttle checks attempts against — `api.routes
        .login` (#50) mints the resulting `api_token` with this same clock, rather
        than a second, independently-resolved `utc_now()`, mirroring
        `OidcLoginService.clock`'s exact reasoning.
        """
        return self._clock

    def check(self, username: str, ip: str) -> None:
        """Raises `LoginThrottledError` if either bucket for `(username, ip)` is
        currently locked out — checked before a password is ever verified.
        `retry_after_seconds` is the longer of the two remaining locks, so the
        reported figure is never shorter than the caller's actual wait.
        """
        now = self._clock()
        username_key = username.casefold()
        username_lock = _active(
            self._attempts.get(LoginAttemptScope.USERNAME, username_key), now
        )
        ip_lock = _active(self._attempts.get(LoginAttemptScope.IP, ip), now)

        retry_afters = [
            (parse_iso(lock.locked_until) - now).total_seconds()
            for lock in (username_lock, ip_lock)
            if lock is not None and lock.locked_until is not None
        ]
        if not retry_afters:
            return

        logger.warning("login throttled", extra={"username": username_key, "ip": ip})
        raise LoginThrottledError(
            "too many failed login attempts",
            details={"retry_after_seconds": max(0.0, max(retry_afters))},
        )

    def record_failure(self, username: str, ip: str) -> None:
        """Bumps both the username and IP buckets for a failed login. Reads the clock
        once so both buckets and the sweep below agree on "now," then sweeps rows that
        are both unlocked and outside their streak window — a documented side effect,
        the same shape `OidcLoginService.start_login` uses to sweep
        `oidc_login_states` on its own already-writing path.
        """
        now = self._clock()
        window = timedelta(seconds=self._settings.attempt_window_seconds)
        self._attempts.delete_expired(
            now=iso(now), first_failed_before=iso(now - window)
        )
        username_key = username.casefold()
        self._bump(
            LoginAttemptScope.USERNAME,
            username_key,
            now,
            max_failed_attempts=self._settings.max_failed_attempts_per_username,
        )
        self._bump(
            LoginAttemptScope.IP,
            ip,
            now,
            max_failed_attempts=self._settings.max_failed_attempts_per_ip,
        )

    def _bump(
        self,
        scope: LoginAttemptScope,
        key: str,
        now: datetime,
        *,
        max_failed_attempts: int,
    ) -> None:
        current = self._attempts.get(scope, key)
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
        if failed_count >= max_failed_attempts:
            locked_until = now + timedelta(seconds=self._settings.lockout_seconds)
            logger.warning(
                "login attempt bucket locked out",
                extra={"scope": scope.value, "key": key, "failed_count": failed_count},
            )

        self._attempts.set(
            scope,
            key,
            failed_count=failed_count,
            first_failed_at=iso(first_failed_at),
            locked_until=iso(locked_until) if locked_until is not None else None,
        )

    def record_success(self, username: str, ip: str) -> None:
        """Clears both buckets for `(username, ip)` — a successful login resets any
        partial failure streak, mirroring `SudoService.step_up`'s own clear-on-success.
        """
        self._attempts.clear(LoginAttemptScope.USERNAME, username.casefold())
        self._attempts.clear(LoginAttemptScope.IP, ip)
