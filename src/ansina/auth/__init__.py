"""RBAC identity & permission model. See issue #24.

Permissions are rows, not an enum: every authorization check answers one question —
"is there a `role_permissions` row granting (one of the caller's roles, this resource,
this verb)?" — regardless of whether the role is builtin or, in a later milestone,
admin-defined. `ansina.auth.policy` is the one place the fixed builtin-role grant
policy is expressed; `ansina.auth.reconciler` materializes it as rows at every boot.

Data + domain layer only: no HTTP routes, no request-scoped `Principal`, no
authenticator chain — those are issue #25 (enforcement) and #26 (sudo step-up).
"""

from ansina.auth.bootstrap import ensure_bootstrap_admin
from ansina.auth.reconciler import reconcile_builtin_roles, sync_resources

__all__ = [
    "ensure_bootstrap_admin",
    "reconcile_builtin_roles",
    "sync_resources",
]
