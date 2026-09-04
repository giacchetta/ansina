"""`Principal` — a caller's already-authenticated identity, resolved once per request.

See issue #25. Built by `ansina.auth.authenticator.resolve_principal` and attached to
`request.state` by `ansina.api.auth.BearerAuthMiddleware`; `ansina.api.authorization
.require()` reads it back to answer "is this caller allowed to do this." Nothing here
talks to the database or to FastAPI — this module is pure data.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum

from ansina.auth.models import User


class AuthMethod(StrEnum):
    """How a `Principal` was authenticated. One member today (the DB-backed API-token
    lookup #24 already ships); a follow-up milestone's OIDC authenticator adds another
    member here, not a new field elsewhere.
    """

    API_TOKEN = "api_token"


@dataclass(frozen=True, slots=True)
class Principal:
    """The resolved caller: who they are, and every role they hold — directly assigned
    or reachable via group membership (`RoleAssignmentRepository.roles_for_user`, issue
    #24's union query, built for exactly this).

    `sudo_active` is always `False` until issue #26's `BearerAuthMiddleware` extension
    resolves a live `X-Sudo-Token` header into a grant and calls `with_sudo()` below.
    """

    user: User
    role_ids: frozenset[str]
    role_slugs: frozenset[str] = field(default_factory=frozenset)
    auth_method: AuthMethod = AuthMethod.API_TOKEN
    sudo_active: bool = False
    sudo_grant_id: str | None = None

    @property
    def actor(self) -> str:
        """The identity to record in an audit log line — the caller's username."""
        return self.user.username

    def with_sudo(self, grant_id: str) -> Principal:
        """A copy of this `Principal` elevated by a live sudo grant (issue #26) —
        `auth.authorization.authorize()`'s `sensitive=True` branch reads `sudo_active`
        back; `api.authorization.require()`'s audit log line reads `sudo_grant_id`.
        """
        return replace(self, sudo_active=True, sudo_grant_id=grant_id)
