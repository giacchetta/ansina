"""Server-side guards for the RBAC management API. See issue #27, extended by #28.

Pure domain logic, no FastAPI — `ansina.api.routes.users`/`groups`/`role_assignments`
(and, as of #28, `ansina.api.tokens`) are the callers, mapping these exceptions to
`problem+json` the same way `ansina.auth.authorization` and
`ansina.api.authorization.require()` already do for the authorization decision itself.

Seven invariants live here because they are the difference between a permission system
and a suggestion, and all must hold regardless of which route reaches them:

- **No caller can grant a permission it does not itself effectively hold**
  (`assert_may_assign_role`, generalized to a raw grant set by
  `assert_may_grant_permissions`, issue #40) — stated in permission terms (a
  subset-of-grants check) so it keeps working unchanged once non-builtin roles exist,
  per issue #27's own framing, and checked at grant-*edit* time as well as at
  assignment time, per issue #40's resolution of M3's open consideration #8.
- **A submitted `(resource, verb)` grant must actually be grantable**
  (`assert_grants_grantable`, issue #40) — the resource must be catalogued, not a
  `me.*` self-resource, and the verb must be one that resource's routes actually
  serve (`GET /auth/permissions`'s own fields, issue #38).
- **The last remaining `Admin` can never lose that role** (`assert_admin_remains`) —
  there is no route back from a database with zero admins.
- **The bootstrap identity holds exactly one `api_token`, ever**
  (`assert_not_bootstrap_identity`, issue #28) — nothing reachable over the API may
  mint it a second one or delete the one it has; see `ansina.auth.bootstrap`'s module
  docstring for the full reasoning behind that identity's redesign.
- **An Admin issues a user's *first* `api_token` credential, never a second**
  (`assert_no_existing_api_token`, issue #28) — beyond the first, a user mints their
  own via `POST /auth/me/tokens`.
- **Enrolling a second TOTP secret requires disabling the first**
  (`assert_totp_not_enrolled`, issue #41) — `POST /auth/me/totp` never implicitly
  overwrites a live enrollment.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from ansina.auth.bootstrap import is_bootstrap_identity
from ansina.auth.models import CredentialType, Role, RoleSlug, Verb
from ansina.auth.policy import is_grantable, is_sensitive_resource
from ansina.auth.principal import Principal
from ansina.auth.repositories import (
    CredentialRepository,
    ResourceRepository,
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


class InvalidGrantError(AuthError):
    """A submitted `(resource, verb)` grant names a resource that isn't catalogued,
    isn't grantable (`auth.policy.is_grantable` — every `me.*` resource), or a verb
    that resource doesn't actually serve (`Resource.verbs`, issue #38's served-verb
    set). See `assert_grants_grantable`.
    """

    code: ClassVar[str] = "ansina.auth.invalid_grant"


class TotpAlreadyEnrolledError(AuthError):
    """A caller tried to enroll a second TOTP secret via `POST /auth/me/totp` without
    disabling the first — issue #41 AC: replacing an enrollment requires disabling it
    first (`DELETE /auth/me/totp` or an Admin's `DELETE /auth/users/{id}/totp` reset),
    never an implicit overwrite.
    """

    code: ClassVar[str] = "ansina.auth.totp_already_enrolled"


class TokenAlreadyIssuedError(AuthError):
    """An Admin (or sudo'd Maintain) may issue a user's *first* `api_token`
    credential via `POST /auth/users/{id}/tokens`, never a second — beyond the first,
    the user mints their own via `POST /auth/me/tokens`. Revoking a user's last token
    drops the count back to zero, which is what makes the admin route double as the
    lost-credential recovery path.
    """

    code: ClassVar[str] = "ansina.auth.token_already_issued"


def _assert_no_excess_grants(
    db: Database,
    principal: Principal,
    grants: frozenset[tuple[str, Verb]],
    *,
    message: str,
    details: Mapping[str, Any],
) -> None:
    """Shared step 2+3 of the self-escalation check — issue #27's `auth.*`/subset
    logic, lifted into one place so `assert_may_assign_role` and
    `assert_may_grant_permissions` (issue #40) can never disagree about it.

    1. `grants` contains any `auth.*` permission and the caller doesn't hold `admin`.
    2. General case: `grants` is not a subset of the caller's own effective grant set.
    """
    is_admin_caller = RoleSlug.ADMIN.value in principal.role_slugs

    if not is_admin_caller and any(
        is_sensitive_resource(resource) for resource, _verb in grants
    ):
        raise SelfEscalationError(
            f"{message} — it grants an auth.* permission, which only Admin may "
            "hand out",
            details=details,
        )

    caller_grants = RolePermissionRepository(db).grants_for_roles(principal.role_ids)
    excess = grants - caller_grants
    if excess:
        raise SelfEscalationError(
            f"{message} — it grants permissions the caller does not itself hold",
            details={
                **details,
                "excess": sorted(
                    f"{resource}:{verb.value}" for resource, verb in excess
                ),
            },
        )


def assert_may_assign_role(db: Database, principal: Principal, role: Role) -> None:
    """Refuse (`SelfEscalationError`) unless `principal` could have granted `role` to
    itself under its own current permissions.

    Checked in three steps, in order — the first two are *not* redundant with the
    third: under M2's fixed policy `Maintain` and `Admin` hold **identical**
    `role_permissions` rows (they diverge only on sudo step-up, which isn't a
    `role_permissions` concern — see `auth.policy.permitted_verbs`), so the subset
    check alone would let a sudo'd `Maintain` mint a fresh `Admin` assignment. Issue
    #27 is explicit that only `Admin` may ever do that, so it's checked directly first.

    1. `role` is `admin` and the caller doesn't hold `admin` — checked here, by slug,
       since it's specific to *assigning a role* (a raw grant set has no slug).
    2. `role` grants any `auth.*` permission and the caller doesn't hold `admin`.
    3. General case: `role`'s grant set is not a subset of the caller's own effective
       grant set.

    Steps 2 and 3 are `_assert_no_excess_grants`, the same check
    `assert_may_grant_permissions` (issue #40) runs over a raw grant set rather than
    an existing role's — the two can never disagree about what counts as escalation.
    """
    is_admin_caller = RoleSlug.ADMIN.value in principal.role_slugs

    if role.slug == RoleSlug.ADMIN.value and not is_admin_caller:
        raise SelfEscalationError(
            f"{principal.actor!r} cannot assign the admin role without holding it",
            details={"role": role.slug},
        )

    role_grants = RolePermissionRepository(db).grants_for_roles(frozenset({role.id}))
    _assert_no_excess_grants(
        db,
        principal,
        role_grants,
        message=f"{principal.actor!r} cannot assign role {role.slug!r}",
        details={"role": role.slug},
    )


def assert_may_grant_permissions(
    db: Database, principal: Principal, grants: frozenset[tuple[str, Verb]]
) -> None:
    """Refuse (`SelfEscalationError`) unless `principal` could have granted `grants`
    to itself under its own current permissions — `assert_may_assign_role`'s steps 2
    and 3, generalized to a raw grant set rather than an existing role's (issue #40),
    so `POST`/`PATCH /auth/roles` can run the identical check at grant-*edit* time,
    not only when a role is later attached to a subject (M3's open consideration #8).
    """
    _assert_no_excess_grants(
        db,
        principal,
        grants,
        message=f"{principal.actor!r} cannot grant the submitted permissions",
        details={},
    )


def assert_grants_grantable(db: Database, grants: frozenset[tuple[str, Verb]]) -> None:
    """Refuse (`InvalidGrantError`) any grant in `grants` whose resource isn't
    catalogued, isn't `grantable` (`auth.policy.is_grantable` — every `me.*`
    resource), or whose verb isn't in that resource's actually-served set
    (`Resource.verbs`, issue #38) — a role granting a verb no route answers to is a
    dead grant, refused at write time rather than stored.

    Called by `POST`/`PATCH /auth/roles` (issue #40) *ahead of*
    `assert_may_grant_permissions`: an uncatalogued or non-grantable resource can
    never be an escalation (no route enforces it either way), so checking this first
    means a typo'd resource name is never answered with a misleading
    `self_escalation` refusal.
    """
    catalog = {
        resource.name: resource for resource in ResourceRepository(db).list_all()
    }
    invalid: list[str] = []
    for resource_name, verb in grants:
        resource = catalog.get(resource_name)
        if resource is None:
            invalid.append(f"{resource_name}:{verb.value} (not a catalogued resource)")
        elif not is_grantable(resource_name):
            invalid.append(f"{resource_name}:{verb.value} (not grantable)")
        elif verb not in resource.verbs:
            invalid.append(f"{resource_name}:{verb.value} (verb not served)")

    if invalid:
        raise InvalidGrantError(
            "one or more grants are invalid: " + ", ".join(sorted(invalid)),
            details={"invalid": sorted(invalid)},
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


def assert_totp_not_enrolled(db: Database, user_id: str) -> None:
    """Refuse (`TotpAlreadyEnrolledError`) if `user_id` already holds a `totp`
    credential — issue #41's "replacing requires disabling first" AC. Called by
    `POST /auth/me/totp` ahead of `CredentialRepository.create_totp_secret`; the
    partial unique index `idx_credentials_one_totp_per_user` is the backstop if a
    caller ever races past this check, not the primary enforcement.
    """
    if CredentialRepository(db).has_credential(user_id, CredentialType.TOTP):
        raise TotpAlreadyEnrolledError(
            f"user {user_id!r} already has a TOTP credential enrolled — disable it "
            "first",
            details={"user_id": user_id},
        )
