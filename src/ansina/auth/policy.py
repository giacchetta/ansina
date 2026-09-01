"""The fixed builtin-role grant policy and the M2 resource bootstrap list.

`permitted_verbs` is the *one* place the builtin policy (Read/Write/Maintain/Admin) is
expressed — `auth.reconciler.reconcile_builtin_roles` calls it for every builtin role,
for every catalogued resource, and materializes whatever it returns as
`role_permissions` rows. Nothing else in the codebase encodes this policy.
"""

from __future__ import annotations

from dataclasses import dataclass

from ansina.auth.models import MUTATING_VERBS, RoleSlug, Verb

# Any resource whose dotted name starts with this prefix is the identity/access-control
# management surface itself — restricted to Maintain/Admin regardless of verb, per
# issue #24's fixed policy. `auth.role_mappings`/`auth.credentials` etc. all match this
# the moment they're catalogued, with no separate listing to keep in sync.
_SENSITIVE_RESOURCE_PREFIX = "auth."


def is_sensitive_resource(resource: str) -> bool:
    """`True` for any `auth.*` resource — the identity/access-control surface itself."""
    return resource.startswith(_SENSITIVE_RESOURCE_PREFIX)


def permitted_verbs(role: RoleSlug, resource: str) -> frozenset[Verb]:
    """The fixed builtin policy: which verbs `role` may issue against `resource`.

    - Read: GET only.
    - Write: GET plus every mutating verb except DELETE.
    - Maintain/Admin: every verb, including DELETE.
    - Any `auth.*` resource: Maintain/Admin only — Read and Write get nothing there,
      regardless of verb.
    """
    if is_sensitive_resource(resource) and role not in (
        RoleSlug.MAINTAIN,
        RoleSlug.ADMIN,
    ):
        return frozenset()

    if role is RoleSlug.READ:
        return frozenset({Verb.GET})
    if role is RoleSlug.WRITE:
        return frozenset({Verb.GET, Verb.POST, Verb.PUT, Verb.PATCH})
    # MAINTAIN and ADMIN hold identical grants under M2's fixed policy — they diverge
    # only in #26's sudo step-up requirement, which is not a `role_permissions` concern.
    return frozenset({Verb.GET, *MUTATING_VERBS})


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """One builtin role's seed data — slug, display name, description. Consumed by
    `auth.reconciler.reconcile_builtin_roles` to seed/verify the `roles` table.
    """

    slug: RoleSlug
    name: str
    description: str


BUILTIN_ROLES: tuple[RoleSpec, ...] = (
    RoleSpec(RoleSlug.READ, "Read", "GET-only access to every non-sensitive resource."),
    RoleSpec(
        RoleSlug.WRITE,
        "Write",
        "GET/POST/PUT/PATCH access to every non-sensitive resource.",
    ),
    RoleSpec(
        RoleSlug.MAINTAIN,
        "Maintain",
        "Full access including DELETE; sensitive auth.* actions require an active "
        "sudo grant (issue #26).",
    ),
    RoleSpec(
        RoleSlug.ADMIN,
        "Admin",
        "Full access including DELETE and auth.*, with no sudo step-up required.",
    ),
)


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """One catalogued resource's seed data. Consumed by
    `auth.reconciler.sync_resources`.
    """

    name: str
    description: str


# The real surface as of issue #24: `/version` and the Heart tick routes (issue #11),
# plus the `auth.*` management resources #27 will expose routes for. Issue #25 replaces
# this constant as the catalog's source, walking `app.routes` instead — this is
# deliberately real, observable seed data now rather than a no-op placeholder.
BOOTSTRAP_RESOURCES: tuple[ResourceSpec, ...] = (
    ResourceSpec("system.version", "GET /version — build/version metadata."),
    ResourceSpec("heart.tick", "The autonomic tick loop's status and pause/resume."),
    ResourceSpec("auth.users", "User account management."),
    ResourceSpec("auth.groups", "Group management."),
    ResourceSpec(
        "auth.roles", "Role listing (builtin roles are read-only, issue #27)."
    ),
    ResourceSpec("auth.role_assignments", "Attaching/detaching a role to a subject."),
    ResourceSpec("auth.permissions", "The resource/verb discovery catalog."),
)
