"""RBAC identity & permission model. See issues #24 and #25.

Permissions are rows, not an enum: every authorization check answers one question —
"is there a `role_permissions` row granting (one of the caller's roles, this resource,
this verb)?" — regardless of whether the role is builtin or, in a later milestone,
admin-defined. `ansina.auth.policy` is the one place the fixed builtin-role grant
policy is expressed; `ansina.auth.reconciler` materializes it as rows at every boot.

Issue #25 adds the request-scoped `Principal` (`principal.py`), the `Authenticator`
chain that resolves one (`authenticator.py`, formalizing #24's inline DB lookup), and
the pure authorization decision (`authorization.py`) that `ansina.api.authorization
.require()` wraps as a FastAPI dependency. Still no HTTP routes here, and sudo-grant
issuance itself is issue #26 — this module only knows how to *check* a grant already
on `Principal.sudo_active`.
"""

from ansina.auth.authenticator import (
    ApiTokenAuthenticator,
    Authenticator,
    build_authenticators,
    resolve_principal,
)
from ansina.auth.authorization import ForbiddenError, SudoRequiredError, authorize
from ansina.auth.bootstrap import ensure_bootstrap_admin
from ansina.auth.principal import AuthMethod, Principal
from ansina.auth.reconciler import reconcile_builtin_roles, sync_resources

__all__ = [
    "ApiTokenAuthenticator",
    "AuthMethod",
    "Authenticator",
    "ForbiddenError",
    "Principal",
    "SudoRequiredError",
    "authorize",
    "build_authenticators",
    "ensure_bootstrap_admin",
    "reconcile_builtin_roles",
    "resolve_principal",
    "sync_resources",
]
