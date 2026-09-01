"""Startup-time reconciliation: the `resources` catalog and builtin-role grants.

`ensure_bootstrap_admin` (see `auth.bootstrap`) runs after both of these — a role can't
be assigned before it exists, and a resource can't be granted before it's catalogued.
`api.app.create_app`'s lifespan calls all three in that fixed order, immediately after
`run_migrations`.
"""

from __future__ import annotations

from ansina.auth.policy import BUILTIN_ROLES, ResourceSpec, permitted_verbs
from ansina.auth.repositories import (
    ResourceRepository,
    RolePermissionRepository,
    RoleRepository,
)
from ansina.logging import get_logger
from ansina.storage.database import Database

logger = get_logger(__name__)


def sync_resources(db: Database, specs: tuple[ResourceSpec, ...]) -> None:
    """Make `resources` match `specs` exactly: upsert every named resource, delete any
    catalogued resource not in `specs`. Deleting a resource cascades away its
    `role_permissions` rows (`ON DELETE CASCADE`) — the catalog is derived data, so a
    resource that's no longer declared loses its grants along with it.

    Issue #24 calls this with `auth.policy.BOOTSTRAP_RESOURCES`; issue #25 replaces that
    argument with a real `app.routes` walk — this function's contract doesn't change.
    """
    resources = ResourceRepository(db)
    wanted = {spec.name: spec.description for spec in specs}
    for name, description in wanted.items():
        resources.upsert(name, description)

    stale = [r.name for r in resources.list_all() if r.name not in wanted]
    for name in stale:
        logger.info("retiring stale resource", extra={"resource": name})
        resources.delete(name)


def reconcile_builtin_roles(db: Database) -> None:
    """Seed the four builtin roles, then make every builtin role's `role_permissions`
    rows match `auth.policy.permitted_verbs` exactly, for every currently-catalogued
    resource.

    Scoped to `builtin = 1` roles only — a future custom role's grants are never read,
    inserted, or deleted by this function, by construction (the query it diffs against
    is filtered to builtin role ids).
    """
    roles = RoleRepository(db)
    permissions = RolePermissionRepository(db)
    resources = ResourceRepository(db)

    role_by_slug = {
        spec.slug: roles.ensure_builtin(spec.slug.value, spec.name, spec.description)
        for spec in BUILTIN_ROLES
    }

    catalogued = [r.name for r in resources.list_all()]

    for slug, role in role_by_slug.items():
        current = {(p.resource, p.verb) for p in permissions.list_for_role(role.id)}
        desired = {
            (resource, verb)
            for resource in catalogued
            for verb in permitted_verbs(slug, resource)
        }

        for resource, verb in desired - current:
            permissions.grant(role.id, resource, verb)
        for resource, verb in current - desired:
            permissions.revoke(role.id, resource, verb)
