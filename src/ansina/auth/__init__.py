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
from ansina.auth.bootstrap import (
    ensure_bootstrap_admin,
    ensure_configured_admin,
    is_bootstrap_identity,
)
from ansina.auth.clock import Clock, iso, parse_iso, utc_now
from ansina.auth.management import (
    BootstrapIdentityError,
    LastAdminError,
    NotFoundError,
    SelfEscalationError,
    TokenAlreadyIssuedError,
    assert_admin_remains,
    assert_may_assign_role,
    assert_no_existing_api_token,
    assert_not_bootstrap_identity,
)
from ansina.auth.principal import AuthMethod, Principal
from ansina.auth.reconciler import reconcile_builtin_roles, sync_resources
from ansina.auth.step_up import (
    PasswordStepUpVerifier,
    StepUpRegistry,
    StepUpVerifier,
    build_step_up_verifiers,
)
from ansina.auth.sudo import SudoLockedOutError, SudoService, build_sudo_service

__all__ = [
    "ApiTokenAuthenticator",
    "AuthMethod",
    "Authenticator",
    "BootstrapIdentityError",
    "Clock",
    "ForbiddenError",
    "LastAdminError",
    "NotFoundError",
    "PasswordStepUpVerifier",
    "Principal",
    "SelfEscalationError",
    "StepUpRegistry",
    "StepUpVerifier",
    "SudoLockedOutError",
    "SudoRequiredError",
    "SudoService",
    "TokenAlreadyIssuedError",
    "assert_admin_remains",
    "assert_may_assign_role",
    "assert_no_existing_api_token",
    "assert_not_bootstrap_identity",
    "authorize",
    "build_authenticators",
    "build_step_up_verifiers",
    "build_sudo_service",
    "ensure_bootstrap_admin",
    "ensure_configured_admin",
    "is_bootstrap_identity",
    "iso",
    "parse_iso",
    "reconcile_builtin_roles",
    "resolve_principal",
    "sync_resources",
    "utc_now",
]
