"""`StepUpVerifier` — the pluggable "prove it's still you" port behind `POST /auth/
sudo`. See issue #26.

`auth.sudo.SudoService` never names `PasswordStepUpVerifier` directly — it only ever
talks to a `StepUpRegistry`. M2 ships exactly one verifier (password, against #24's
`credentials` table); a follow-up milestone's TOTP or federated-session re-auth is a
second `StepUpVerifier` implementation plus a per-user choice of which one applies, not
a rewrite of the grant/TTL/revocation machinery in `auth.sudo` or #25's
`sensitive`-gated enforcement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ansina.auth.hashing import Argon2Params
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
    itself.
    """

    @property
    def name(self) -> str: ...

    def verify(self, principal: Principal, payload: Mapping[str, Any]) -> bool: ...


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


class StepUpRegistry:
    """The ordered set of verifiers step-up can resolve against. M2's rule is fixed —
    `for_principal` always returns the first (only) entry; a follow-up milestone's
    per-user verifier choice replaces this method's body alone, not its callers.
    """

    def __init__(self, verifiers: tuple[StepUpVerifier, ...]) -> None:
        if not verifiers:
            raise ValueError("StepUpRegistry needs at least one StepUpVerifier")
        self._verifiers = verifiers

    def for_principal(self, principal: Principal) -> StepUpVerifier:
        del principal  # unused until a follow-up milestone's per-user selection
        return self._verifiers[0]


def build_step_up_verifiers(
    db: Database, settings: Settings
) -> tuple[StepUpVerifier, ...]:
    """The chain `StepUpRegistry` wraps, in order. Mirrors `authenticator.
    build_authenticators`'s shape exactly — a follow-up milestone appends to this
    tuple, nothing else about its callers changes.
    """
    params = Argon2Params.from_settings(settings)
    return (PasswordStepUpVerifier(db, params),)
