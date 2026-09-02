"""`Principal` — a caller's already-authenticated identity, resolved once per request.

See issue #25. Built by `ansina.auth.authenticator.resolve_principal` and attached to
`request.state` by `ansina.api.auth.BearerAuthMiddleware`; `ansina.api.authorization
.require()` reads it back to answer "is this caller allowed to do this." Nothing here
talks to the database or to FastAPI — this module is pure data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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

    `sudo_active` is always `False` here — issue #25 ships the field so #26's sudo
    step-up has somewhere to set it without touching every caller of this dataclass.
    """

    user: User
    role_ids: frozenset[str]
    role_slugs: frozenset[str] = field(default_factory=frozenset)
    auth_method: AuthMethod = AuthMethod.API_TOKEN
    sudo_active: bool = False

    @property
    def actor(self) -> str:
        """The identity to record in an audit log line — the caller's username."""
        return self.user.username
