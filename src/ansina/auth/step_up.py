"""`StepUpVerifier` — the pluggable "prove it's still you" port behind `POST /auth/
sudo`. See issue #26, generalized to a per-principal verifier *set* by issue #37.

`auth.sudo.SudoService` never names `PasswordStepUpVerifier` directly — it only ever
talks to a `StepUpRegistry`. M2 shipped exactly one verifier (password, against #24's
`credentials` table); issue #37 makes `StepUpRegistry.for_principal` return every
verifier a caller is actually *enrolled* in rather than a single fixed one, the
foundation a follow-up TOTP verifier (#41) and the TUI's multi-factor `auth sudo`
(#44) both build on — without a rewrite of the grant/TTL/revocation machinery in
`auth.sudo` or #25's `sensitive`-gated enforcement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ansina.auth.hashing import Argon2Params
from ansina.auth.models import CredentialType
from ansina.auth.repositories import CredentialRepository
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
    build_authenticators`'s shape exactly — a follow-up milestone appends to this
    tuple, nothing else about its callers changes.
    """
    params = Argon2Params.from_settings(settings)
    return (PasswordStepUpVerifier(db, params),)
