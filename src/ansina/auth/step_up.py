"""`StepUpVerifier` — the pluggable "prove it's still you" port behind `POST /auth/
sudo`. See issue #26, generalized to a per-principal verifier *set* by issue #37.

`auth.sudo.SudoService` never names `PasswordStepUpVerifier`/`TotpStepUpVerifier`
directly — it only ever talks to a `StepUpRegistry`. M2 shipped exactly one verifier
(password, against #24's `credentials` table); issue #37 made `StepUpRegistry
.for_principal` return every verifier a caller is actually *enrolled* in rather than a
single fixed one, the foundation issue #41's `TotpStepUpVerifier` and the TUI's
multi-factor `auth sudo` (#44) both build on — without a rewrite of the grant/TTL/
revocation machinery in `auth.sudo` or #25's `sensitive`-gated enforcement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ansina.auth.clock import Clock, utc_now
from ansina.auth.encryption import DecryptionError, decrypt, resolve_key
from ansina.auth.hashing import Argon2Params
from ansina.auth.models import CredentialType
from ansina.auth.repositories import CredentialRepository
from ansina.auth.totp import find_valid_step
from ansina.storage.database import Database

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from ansina.auth.principal import Principal
    from ansina.config.settings import Settings


class StepUpVerifier(Protocol):
    """One credential-verification strategy for step-up. `name` is recorded on the
    issued grant (`sudo_grants.verifier`) — an audit trail that stays meaningful once
    more than one verifier exists. `verify` returns `False` for "credential didn't
    match," never raises for that case — only for a genuine failure of the check
    itself. `is_enrolled` (issue #37) is the enrollment question `StepUpRegistry.
    for_principal` asks of every verifier: does this principal hold a credential this
    verifier could ever succeed against? Each verifier owns its own answer — the
    registry never inspects `credentials` itself.
    """

    @property
    def name(self) -> str: ...

    def verify(self, principal: Principal, payload: Mapping[str, Any]) -> bool: ...

    def is_enrolled(self, principal: Principal) -> bool: ...


class PasswordStepUpVerifier:
    """M2's one verifier: re-checks `payload["password"]` against the caller's own
    argon2id password credential (`CredentialRepository.verify_password`, issue #24).
    A missing or non-string `password` key is treated as a non-match, not a distinct
    error shape — a malformed body is just one more failed attempt.
    """

    name = "password"

    def __init__(self, db: Database, params: Argon2Params) -> None:
        self._credentials = CredentialRepository(db)
        self._params = params

    def verify(self, principal: Principal, payload: Mapping[str, Any]) -> bool:
        password = payload.get("password")
        if not isinstance(password, str) or not password:
            return False
        return self._credentials.verify_password(
            principal.user.id, password, self._params
        )

    def is_enrolled(self, principal: Principal) -> bool:
        return self._credentials.has_credential(
            principal.user.id, CredentialType.PASSWORD
        )


class TotpStepUpVerifier:
    """M3's second verifier (issue #41): RFC 6238 time-based codes — the only factor
    available to a local, token-only user with no password (the default shape of any
    user `POST /auth/users` creates without `password` set) and no external identity
    to fall back on.

    A verified code can never be replayed: `auth.totp.find_valid_step`'s anti-replay
    floor is persisted on the credential's own `last_used_at` column as the matched
    step index (a plain integer, not an ISO timestamp — see
    `storage/migrations/0006_totp_credential.sql`'s docstring for why that divergence
    is safe). `clock` is injectable, like `auth.sudo.SudoService`'s own, so a step
    boundary and a replay attempt are both exactly reproducible in a test rather than
    merely approximated with real sleeping.
    """

    name = "totp"

    def __init__(
        self, db: Database, key: bytes | None, *, clock: Clock = utc_now
    ) -> None:
        self._credentials = CredentialRepository(db)
        self._key = key
        self._clock = clock

    def verify(self, principal: Principal, payload: Mapping[str, Any]) -> bool:
        code = payload.get("code")
        if not isinstance(code, str) or not code:
            return False
        if self._key is None:
            # No [security.encryption] key configured. `ensure_key_configured_if_
            # needed` refuses to boot once any totp credential exists at all, so this
            # is unreachable in a correctly configured deployment — kept as a safe
            # default (never raise from `verify`, matching `PasswordStepUpVerifier`'s
            # own discipline for a malformed payload) rather than an assertion.
            return False

        credential = self._credentials.get_totp_secret(principal.user.id)
        if credential is None:
            return False
        try:
            secret = decrypt(credential.hash, self._key)
        except DecryptionError:
            return False

        not_before = (
            int(credential.last_used_at)
            if credential.last_used_at is not None
            else None
        )
        matched_step = find_valid_step(
            secret,
            code,
            at=int(self._clock().timestamp()),
            not_before_step=not_before,
        )
        if matched_step is None:
            return False
        self._credentials.touch_last_used(credential.id, now=str(matched_step))
        return True

    def is_enrolled(self, principal: Principal) -> bool:
        return self._credentials.has_credential(principal.user.id, CredentialType.TOTP)


class StepUpRegistry:
    """The ordered set of verifiers step-up can resolve against, in preference order.
    Issue #37: `for_principal` filters to the subset a given principal is actually
    enrolled in (`StepUpVerifier.is_enrolled`) — a password-less user (the default
    shape of any user created without `CreateUserRequest.password`) gets `()`, not a
    verifier it can never satisfy, which is what let a password-less caller burn
    failed-attempt lockouts against a credential it could never hold (see
    `auth.sudo.StepUpUnavailableError`). The constructor's non-empty check is
    unrelated and unchanged — "the server has at least one verifier configured" is a
    different question from "this caller is enrolled in one."
    """

    def __init__(self, verifiers: tuple[StepUpVerifier, ...]) -> None:
        if not verifiers:
            raise ValueError("StepUpRegistry needs at least one StepUpVerifier")
        self._verifiers = verifiers

    def for_principal(self, principal: Principal) -> tuple[StepUpVerifier, ...]:
        return tuple(v for v in self._verifiers if v.is_enrolled(principal))


def build_step_up_verifiers(
    db: Database, settings: Settings
) -> tuple[StepUpVerifier, ...]:
    """The chain `StepUpRegistry` wraps, in order. Mirrors `authenticator.
    build_authenticators`'s shape exactly. Issue #41 adds `TotpStepUpVerifier` as the
    second entry — always present in the chain regardless of whether
    `[security.encryption] key` is configured (a caller only ever resolves to it via
    `StepUpRegistry.for_principal`'s enrollment filter, and nobody can be enrolled in
    TOTP without a key having been configured at the time they enrolled), so no
    conditional wiring is needed here.
    """
    params = Argon2Params.from_settings(settings)
    key = resolve_key(settings)
    return (PasswordStepUpVerifier(db, params), TotpStepUpVerifier(db, key))
