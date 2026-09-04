"""Server-side guards for the RBAC management API. See issue #27.

Pure domain logic, no FastAPI — `ansina.api.routes.users`/`groups`/`role_assignments`
are the callers, mapping these exceptions to `problem+json` the same way `ansina.auth.
authorization` and `ansina.api.authorization.require()` already do for the
authorization decision itself.

Two invariants live here because they are the difference between a permission system
and a suggestion, and both must hold regardless of which route reaches them:

- **No caller can grant a permission it does not itself effectively hold**
  (`assert_may_assign_role`) — stated in permission terms (a subset-of-grants check) so
  it keeps working unchanged once non-builtin roles exist, per issue #27's own framing.
- **The last remaining `Admin` can never lose that role** (`assert_admin_remains`) —
  there is no route back from a database with zero admins.
"""

from __future__ import annotations

from typing import ClassVar

from ansina.auth.models import Role, RoleSlug
from ansina.auth.policy import is_sensitive_resource
from ansina.auth.principal import Principal
from ansina.auth.repositories import (
    RoleAssignmentRepository,
    RolePermissionRepository,
    RoleRepository,
)
from ansina.errors import AuthError
from ansina.storage.database import Database


class SelfEscalationError(AuthError):
    """A caller tried to assign a role it could not have granted itself — either the
    `admin` role while not holding it, a role carrying any `auth.*` grant while not
    `admin`, or (the general case) a role whose grant set isn't a subset of the caller's
    own.
    """

    code: ClassVar[str] = "ansina.auth.self_escalation"


class LastAdminError(AuthError):
    """A caller tried to delete, deactivate, or demote the sole remaining holder of the
    `admin` role — refused outright, since there is no route to recovery with zero
    admins able to fix it.
    """

    code: ClassVar[str] = "ansina.auth.last_admin"


class NotFoundError(AuthError):
    """A path referenced a user/group/role id that doesn't exist."""

    code: ClassVar[str] = "ansina.auth.not_found"


def assert_may_assign_role(db: Database, principal: Principal, role: Role) -> None:
    """Refuse (`SelfEscalationError`) unless `principal` could have granted `role` to
    itself under its own current permissions.

    Checked in three steps, in order — the first two are *not* redundant with the
    third: under M2's fixed policy `Maintain` and `Admin` hold **identical**
    `role_permissions` rows (they diverge only on sudo step-up, which isn't a
    `role_permissions` concern — see `auth.policy.permitted_verbs`), so the subset
    check alone would let a sudo'd `Maintain` mint a fresh `Admin` assignment. Issue
    #27 is explicit that only `Admin` may ever do that, so it's checked directly first.

    1. `role` is `admin` and the caller doesn't hold `admin`.
    2. `role` grants any `auth.*` permission and the caller doesn't hold `admin`.
    3. General case: `role`'s grant set is not a subset of the caller's own effective
       grant set.
    """
    permissions = RolePermissionRepository(db)
    is_admin_caller = RoleSlug.ADMIN.value in principal.role_slugs

    if role.slug == RoleSlug.ADMIN.value and not is_admin_caller:
        raise SelfEscalationError(
            f"{principal.actor!r} cannot assign the admin role without holding it",
            details={"role": role.slug},
        )

    role_grants = permissions.grants_for_roles(frozenset({role.id}))
    if not is_admin_caller and any(
        is_sensitive_resource(resource) for resource, _verb in role_grants
    ):
        raise SelfEscalationError(
            f"{principal.actor!r} cannot assign role {role.slug!r} — it grants an "
            "auth.* permission, which only Admin may hand out",
            details={"role": role.slug},
        )

    caller_grants = permissions.grants_for_roles(principal.role_ids)
    excess = role_grants - caller_grants
    if excess:
        raise SelfEscalationError(
            f"{principal.actor!r} cannot assign role {role.slug!r} — it grants "
            "permissions the caller does not itself hold",
            details={
                "role": role.slug,
                "excess": sorted(
                    f"{resource}:{verb.value}" for resource, verb in excess
                ),
            },
        )


def assert_admin_remains(db: Database, losing_user_ids: frozenset[str]) -> None:
    """Refuse (`LastAdminError`) if removing `admin` access from every id in
    `losing_user_ids` (a delete, a deactivation, a role detach, or a group's own
    detach — every member of a demoted group passes through here) would leave zero
    active, non-deleted `Admin` holders.
    """
    admin_role = RoleRepository(db).get_by_slug(RoleSlug.ADMIN.value)
    if admin_role is None:
        return  # unreachable — reconcile_builtin_roles seeds it at every boot
    remaining = RoleAssignmentRepository(db).user_ids_with_role(admin_role.id)
    if remaining - losing_user_ids:
        return
    raise LastAdminError(
        "refusing to remove the sole remaining Admin — there would be no admin left "
        "able to recover",
        details={"losing_user_ids": sorted(losing_user_ids)},
    )
