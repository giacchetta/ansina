"""The authorization decision: does `principal` hold a role with a `role_permissions`
row for (this resource, this verb)? See issue #25.

Pure decision logic, no FastAPI — `ansina.api.authorization.require()` is the one
caller, wrapping this in a dependency and mapping its exceptions to `problem+json`.
Reuses `RolePermissionRepository.effective_verbs` (issue #24) rather than
re-deriving the grant query.
"""

from __future__ import annotations

from typing import ClassVar

from ansina.auth.models import RoleSlug, Verb
from ansina.auth.principal import Principal
from ansina.auth.repositories import RolePermissionRepository
from ansina.errors import AuthError
from ansina.storage.database import Database


class ForbiddenError(AuthError):
    """`principal` holds no role granting `verb` on `resource`."""

    code: ClassVar[str] = "ansina.forbidden"


class SudoRequiredError(AuthError):
    """`principal` holds only `maintain` (not `admin`) on a `sensitive=True` resource,
    with no live sudo grant. Inert until issue #26 ships a way to make `sudo_active`
    ever `True` — no route passes `sensitive=True` yet either.
    """

    code: ClassVar[str] = "ansina.auth.sudo_required"


def authorize(
    db: Database,
    principal: Principal,
    resource: str,
    verb: Verb,
    *,
    sensitive: bool = False,
) -> None:
    """Raise `ForbiddenError`/`SudoRequiredError`, or return, never both, never
    neither. The permission check (does any of `principal`'s roles grant `verb` on
    `resource`) always runs first — a caller with no grant at all gets `Forbidden`,
    not a sudo prompt for an action it couldn't take anyway.
    """
    granted = RolePermissionRepository(db).effective_verbs(principal.role_ids, resource)
    if verb not in granted:
        raise ForbiddenError(
            f"{principal.actor!r} holds no role granting {verb.value} on {resource!r}",
            details={"resource": resource, "verb": verb.value},
        )

    if (
        sensitive
        and RoleSlug.MAINTAIN.value in principal.role_slugs
        and RoleSlug.ADMIN.value not in principal.role_slugs
        and not principal.sudo_active
    ):
        raise SudoRequiredError(
            f"{principal.actor!r} must present a live sudo grant for "
            f"{verb.value} on {resource!r}",
            details={"resource": resource, "verb": verb.value},
        )
