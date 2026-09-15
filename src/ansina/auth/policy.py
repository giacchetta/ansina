"""The fixed builtin-role grant policy and the M2 resource bootstrap list.

`permitted_verbs` is the *one* place the builtin policy (Read/Write/Maintain/Admin) is
expressed — `auth.reconciler.reconcile_builtin_roles` calls it for every builtin role,
for every catalogued resource, and materializes whatever it returns as
`role_permissions` rows. Nothing else in the codebase encodes this policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ansina.auth.models import MUTATING_VERBS, RoleSlug, Verb

# Any resource whose dotted name starts with this prefix is the identity/access-control
# management surface itself — restricted to Maintain/Admin regardless of verb, per
# issue #24's fixed policy. `auth.role_mappings`/`auth.credentials` etc. all match this
# the moment they're catalogued, with no separate listing to keep in sync.
_SENSITIVE_RESOURCE_PREFIX = "auth."

# Any resource whose dotted name starts with this prefix is a *self* surface — one
# whose subject is always the authenticated caller acting on their own account (issue
# #30, e.g. `me.profile`, and #28's later `me.tokens`). Granting every verb here to
# every builtin role is not an escalation: a `me.*` route can never reach another
# user's data, by construction of what "self" means, so there is nothing for a role's
# ordinary GET-only/no-DELETE restrictions to protect against.
_SELF_RESOURCE_PREFIX = "me."


def is_sensitive_resource(resource: str) -> bool:
    """`True` for any `auth.*` resource — the identity/access-control surface itself."""
    return resource.startswith(_SENSITIVE_RESOURCE_PREFIX)


def is_self_resource(resource: str) -> bool:
    """`True` for any `me.*` resource — a caller acting on their own account only."""
    return resource.startswith(_SELF_RESOURCE_PREFIX)


class PolicyClass(StrEnum):
    """Which of the three fixed policy branches `permitted_verbs` applies to a
    resource — surfaced by `GET /auth/permissions` (issue #38) so a future custom-role
    editor can render *why* a resource grants what it does, not just what it grants.
    """

    ORDINARY = "ordinary"
    AUTH = "auth"
    SELF = "self"


def policy_class(resource: str) -> PolicyClass:
    """`resource`'s policy class — checked in the same order as `permitted_verbs`
    itself (`self` first, then `auth`) so the two functions can never disagree about
    which branch a resource falls under.
    """
    if is_self_resource(resource):
        return PolicyClass.SELF
    if is_sensitive_resource(resource):
        return PolicyClass.AUTH
    return PolicyClass.ORDINARY


def is_grantable(resource: str) -> bool:
    """`False` for any `me.*` resource. Every builtin role already holds every verb
    there (`permitted_verbs`'s self-resource carve-out), so offering e.g.
    `me.tokens:DELETE` as a grantable row in a custom-role picker would communicate an
    escalation that doesn't exist — the subject of a `me.*` action is always the
    caller themselves, by construction.
    """
    return not is_self_resource(resource)


def permitted_verbs(role: RoleSlug, resource: str) -> frozenset[Verb]:
    """The fixed builtin policy: which verbs `role` may issue against `resource`.

    - Any `me.*` resource: every verb, for every role, unconditionally — checked
      first, ahead of the `auth.*` restriction below. Not an escalation: the subject
      of a `me.*` action is always the caller themselves (issue #30), so there is no
      grant here that reaches beyond what the caller already owns.
    - Read: GET only.
    - Write: GET plus every mutating verb except DELETE.
    - Maintain/Admin: every verb, including DELETE.
    - Any `auth.*` resource: Maintain/Admin only — Read and Write get nothing there,
      regardless of verb.
    """
    if is_self_resource(resource):
        return frozenset({Verb.GET, *MUTATING_VERBS})

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

    Issue #24 seeded this from a hand-written constant (`BOOTSTRAP_RESOURCES`); issue
    #25 replaces that source with `ansina.api.route_audit.audit_route_coverage`'s real
    `app.routes` walk — every route's own `require(resource, description=...)`
    declaration becomes one `ResourceSpec`, so the catalog can never drift from the
    actual surface. `sync_resources`'s contract (make `resources` match the given specs
    exactly) is unchanged. `verbs` (issue #38) is the union of every surviving route's
    `route.methods` for this resource — no default, since every call site should state
    what a route actually serves rather than silently claiming "everything".
    """

    name: str
    description: str
    verbs: frozenset[Verb]
