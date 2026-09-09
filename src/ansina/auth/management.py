"""Server-side guards for the RBAC management API. See issue #27, extended by #28.

Pure domain logic, no FastAPI — `ansina.api.routes.users`/`groups`/`role_assignments`
(and, as of #28, `ansina.api.tokens`) are the callers, mapping these exceptions to
`problem+json` the same way `ansina.auth.authorization` and
`ansina.api.authorization.require()` already do for the authorization decision itself.

Four invariants live here because they are the difference between a permission system
and a suggestion, and all must hold regardless of which route reaches them:

- **No caller can grant a permission it does not itself effectively hold**
  (`assert_may_assign_role`) — stated in permission terms (a subset-of-grants check) so
  it keeps working unchanged once non-builtin roles exist, per issue #27's own framing.
- **The last remaining `Admin` can never lose that role** (`assert_admin_remains`) —
  there is no route back from a database with zero admins.
- **The bootstrap identity holds exactly one `api_token`, ever**
  (`assert_not_bootstrap_identity`, issue #28) — nothing reachable over the API may
  mint it a second one or delete the one it has; see `ansina.auth.bootstrap`'s module
  docstring for the full reasoning behind that identity's redesign.
- **An Admin issues a user's *first* `api_token` credential, never a second**
  (`assert_no_existing_api_token`, issue #28) — beyond the first, a user mints their
  own via `POST /auth/me/tokens`.
"""

from __future__ import annotations

from typing import ClassVar

from ansina.auth.bootstrap import is_bootstrap_identity
from ansina.auth.models import Role, RoleSlug
from ansina.auth.policy import is_sensitive_resource
from ansina.auth.principal import Principal
from ansina.auth.repositories import (
    CredentialRepository,
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


class BootstrapIdentityError(AuthError):
    """The bootstrap identity (`ansina.auth.bootstrap`'s synthetic Admin) holds
    exactly one `api_token`, ever — nothing reachable over the API may mint it a
    second one or delete the one it has. Retiring its credential is
    `security.bootstrap_admin_enabled = False`, not a token-API call.
    """

    code: ClassVar[str] = "ansina.auth.bootstrap_identity"


class TokenAlreadyIssuedError(AuthError):
    """An Admin (or sudo'd Maintain) may issue a user's *first* `api_token`
    credential via `POST /auth/users/{id}/tokens`, never a second — beyond the first,
    the user mints their own via `POST /auth/me/tokens`. Revoking a user's last token
    drops the count back to zero, which is what makes the admin route double as the
    lost-credential recovery path.
    """

    code: ClassVar[str] = "ansina.auth.token_already_issued"


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


def assert_not_bootstrap_identity(db: Database, user_id: str) -> None:
    """Refuse (`BootstrapIdentityError`) if `user_id` is the synthetic bootstrap
    identity — issue #28's invariant A, enforced here so it can't be worked around by
    any future caller of `ansina.api.tokens.issue_token`/`revoke_token`.
    """
    if is_bootstrap_identity(db, user_id):
        raise BootstrapIdentityError(
            "the bootstrap identity's credential is managed by "
            "security.bootstrap_admin_enabled, not the token API",
            details={"user_id": user_id},
        )


def assert_no_existing_api_token(db: Database, user_id: str) -> None:
    """Refuse (`TokenAlreadyIssuedError`) if `user_id` already holds an `api_token`
    credential — issue #28's invariant B. Called by `POST /auth/users/{id}/tokens`
    ahead of `ansina.api.tokens.issue_token`.
    """
    if CredentialRepository(db).list_api_tokens(user_id):
        raise TokenAlreadyIssuedError(
            f"user {user_id!r} already holds an api_token — revoke it first, or "
            "have the user mint their own via POST /auth/me/tokens",
            details={"user_id": user_id},
        )
