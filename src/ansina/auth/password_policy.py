"""Password acceptability policy — issue #48. Pure domain logic, no FastAPI, mirroring
how `auth.management` sits under `api.routes.*`.

Makes a password safe to be a user's *sole* login credential ahead of issue #50, which
lets an anonymous caller exchange one for a token. Enforced at every password-setting
path (`POST /auth/users`, `PUT /auth/users/{id}/password`, `PUT /auth/me/password`) via
`assert_password_acceptable` — never inside `CredentialRepository.set_password` itself,
which stays policy-free so `auth.bootstrap`'s provisioning paths (which never set a
password) are structurally unaffected.

Deliberately **no character-class/composition rules** (no forced digit/symbol/mixed
case) — this is a decision, not an omission. NIST SP 800-63B §5.1.1.2 recommends
against composition rules: they push users toward predictable substitutions
("password" -> "P@ssw0rd1") without a proven strength gain, and the same guidance is
why length plus a common-password check is the whole policy here.

Four checks, in this order, first failure wins:

1. `min_length` — too short to resist guessing.
2. `max_length` — an argon2 work-factor DoS ceiling, not a strength rule: an
   unbounded input lets a caller force an expensive hash over an arbitrarily large
   payload on every request.
3. Contains the username, case-insensitively — a password *of* the account name is no
   secret at all.
4. On the bundled common-password list, case-insensitively (`reject_common`) — see
   `auth/data/common_passwords.txt` for provenance.

The rejected password is never echoed back — not in the exception message, not in
`details` — the same discipline `guardrails/secret-prevention.md` requires of source
code applies equally to an error response a caller (or a log line) might capture.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import TYPE_CHECKING, ClassVar

from ansina.errors import AuthError

if TYPE_CHECKING:
    from ansina.config.settings import Settings


class WeakPasswordError(AuthError):
    """A submitted password fails `assert_password_acceptable` — too short, too long,
    contains the username, or is on the common-password list. Mapped to 400 in
    `api.problems` (a well-formed, semantically resolvable request whose *value* policy
    refuses — not FastAPI's own 422 for a malformed/unresolvable one).
    """

    code: ClassVar[str] = "ansina.auth.weak_password"


@lru_cache(maxsize=1)
def _common_passwords() -> frozenset[str]:
    """The bundled rejection list, loaded once via `importlib.resources` (not
    `Path(__file__)`, so this resolves correctly from an installed wheel, not just a
    source checkout) and cached for the process lifetime — a few thousand short lines,
    cheap to hold in memory, expensive to re-parse on every password check.
    """
    text = (
        resources.files("ansina.auth")
        .joinpath("data", "common_passwords.txt")
        .read_text("utf-8")
    )
    return frozenset(
        line.casefold()
        for line in text.splitlines()
        if line and not line.startswith("#")
    )


def assert_password_acceptable(
    password: str, *, username: str, settings: Settings
) -> None:
    """Refuse (`WeakPasswordError`) a `password` unfit to be `username`'s sole login
    credential. Checked in the module docstring's stated order; `details` on the raised
    error names the rule and, where relevant, the configured bound — never the
    submitted value.
    """
    policy = settings.security.password

    if len(password) < policy.min_length:
        raise WeakPasswordError(
            f"password is shorter than the minimum of {policy.min_length} characters",
            details={"rule": "min_length", "min_length": policy.min_length},
        )

    if len(password) > policy.max_length:
        raise WeakPasswordError(
            f"password is longer than the maximum of {policy.max_length} characters",
            details={"rule": "max_length", "max_length": policy.max_length},
        )

    folded = password.casefold()

    if username and username.casefold() in folded:
        raise WeakPasswordError(
            "password must not contain the username",
            details={"rule": "contains_username"},
        )

    if policy.reject_common and folded in _common_passwords():
        raise WeakPasswordError(
            "password is on the common-password rejection list",
            details={"rule": "common_password"},
        )
