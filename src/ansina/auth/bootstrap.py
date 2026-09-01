"""The bootstrap Admin identity: on first boot with no users, Ansina provisions a
single synthetic Admin so the service is reachable at all — attributable in audit logs
(via its own `external_identities` row), disableable via config once a real Admin user
exists. See issue #24.

Two ways the bootstrap credential is sourced, mutually exclusive per boot:

- No `security.api_token` configured (the recommended path): Ansina generates its own
  high-entropy token, prints it to stdout **exactly once**, and stores only its salted
  hash — the plaintext is never written to config, never logged, and is not
  recoverable afterward by any means, including reading the database. Stable across
  restarts: once created, the credential is never silently rotated, since that would
  invalidate a token the operator already copied down.
- `security.api_token` explicitly configured: an operator-supplied override (validated
  for length/charset/entropy by `config.settings.SecuritySettings`), re-synced onto the
  credential on *every* boot — this is what makes rotating
  `ANSINA_SECURITY__API_TOKEN` in config actually take effect.

Runs after `auth.reconciler.reconcile_builtin_roles` in `api.app.create_app`'s lifespan
— the `admin` role must already exist before this assigns it.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from ansina.auth.models import CredentialType, RoleSlug, SubjectType
from ansina.auth.repositories import (
    CredentialRepository,
    ExternalIdentityRepository,
    RoleAssignmentRepository,
    RoleRepository,
    UserRepository,
)
from ansina.logging import get_logger, register_secret
from ansina.storage.database import Database

if TYPE_CHECKING:
    from ansina.config.settings import Settings

logger = get_logger(__name__)

# The one deliberate exception to "every M2 user gets exactly one provider='local'
# row" (issue #24) — the bootstrap identity is distinguishable from a real local
# account precisely because it uses this provider instead.
BOOTSTRAP_PROVIDER = "local-bootstrap"
_BOOTSTRAP_USERNAME = "bootstrap-admin"
_BOOTSTRAP_TOKEN_LABEL_GENERATED = "bootstrap (auto-generated)"
_BOOTSTRAP_TOKEN_LABEL_OVERRIDE = "bootstrap (security.api_token override)"

# 32 raw bytes -> 43 base64url characters, ~256 bits of entropy. Comfortably clears
# `config.settings.SecuritySettings`'s own strength bar for a manually-supplied token,
# by design — an operator who copies this value into `ANSINA_SECURITY__API_TOKEN`
# later (e.g. to pin it across a redeploy) should never hit that validator.
_GENERATED_TOKEN_BYTES = 32

_BANNER = """
================================================================================
 Ansina bootstrap Admin API token — shown ONCE, then forgotten forever.

 Copy it now. It is not stored in plaintext anywhere (not in this log, not in
 the database, not in config) and cannot be recovered if lost — only replaced.

   {token}

 Use it to authenticate your first request and create a real Admin user; once
 you have one, this bootstrap identity can be retired by setting
 ANSINA_SECURITY__BOOTSTRAP_ADMIN_ENABLED=false.
================================================================================
""".strip("\n")


def _print_bootstrap_token_banner(token: str) -> None:
    """Writes the one-time banner straight to stdout — deliberately bypassing
    `logging` entirely. Going through `get_logger`/`JsonFormatter` would either get the
    token redacted into `***` by `logging.redaction` (since #24 registers every
    configured secret for redaction) or, if printed before registration, risk being
    captured verbatim by log aggregation alongside everything else Ansina logs. A
    plain, human-addressed stdout banner is the one place this value is allowed to
    appear. `flush=True` since stdout is block-buffered (not line-buffered) when it's
    not a tty — e.g. a subprocess pipe/file, as in `tests/e2e` — so this must be
    forced out immediately rather than sitting in an internal buffer until some
    unrelated later write fills it.
    """
    print(_BANNER.format(token=token), flush=True)


def ensure_bootstrap_admin(db: Database, settings: Settings) -> None:
    """Resolve the bootstrap Admin identity, generating or syncing its credential per
    the module docstring's two paths above.

    - `security.enabled = False`: no-op entirely — dev mode, no authentication of any
      kind is enforced, so no credential is needed.
    - `security.bootstrap_admin_enabled = False`: revoke the bootstrap identity's
      `api_token` credential (so it stops authenticating) while keeping the user and
      its `external_identities` row, so historic audit log lines referring to it stay
      attributable.
    - No bootstrap identity yet and `users` is empty: create it, assign `admin`, and
      either store the configured override or generate-and-print a fresh token.
    - Bootstrap identity already exists: an override, if configured, is re-synced onto
      it every boot; an auto-generated token is left untouched.
    """
    if not settings.security.enabled:
        return

    identities = ExternalIdentityRepository(db)
    users = UserRepository(db)
    credentials = CredentialRepository(db)
    roles = RoleRepository(db)
    assignments = RoleAssignmentRepository(db)

    identity = identities.get_by_provider_subject(
        BOOTSTRAP_PROVIDER, _BOOTSTRAP_USERNAME
    )

    if not settings.security.bootstrap_admin_enabled:
        if identity is not None:
            credentials.delete_credentials(identity.user_id, CredentialType.API_TOKEN)
            logger.info(
                "bootstrap admin credential revoked (bootstrap_admin_enabled=false)"
            )
        return

    override = settings.security.api_token

    if identity is not None:
        if override is not None:
            credentials.replace_api_token(
                identity.user_id,
                override.get_secret_value(),
                label=_BOOTSTRAP_TOKEN_LABEL_OVERRIDE,
            )
        # No override: the auto-generated token stays exactly as first created — a
        # restart must never silently invalidate a token the operator already copied
        # down (see module docstring).
        return

    if users.list_all():
        # A real user already exists (created via a management API in a later
        # milestone, or a previous bootstrap run whose identity row was since
        # removed by hand) — never create a second bootstrap identity.
        return

    # `local_identity=False`: this user gets a `local-bootstrap` identity below
    # instead of `UserRepository.create()`'s default `local` one — it authenticates
    # via an api_token credential, not a password-login-shaped local account.
    user = users.create(
        _BOOTSTRAP_USERNAME, display_name="Bootstrap Admin", local_identity=False
    )
    identities.create(user.id, BOOTSTRAP_PROVIDER, _BOOTSTRAP_USERNAME)
    admin_role = roles.get_by_slug(RoleSlug.ADMIN.value)
    assert admin_role is not None  # reconcile_builtin_roles runs before this, always
    assignments.assign(SubjectType.USER, user.id, admin_role.id)

    if override is not None:
        credentials.create_api_token(
            user.id, override.get_secret_value(), label=_BOOTSTRAP_TOKEN_LABEL_OVERRIDE
        )
        logger.info(
            "created bootstrap admin identity (operator-supplied token)",
            extra={"user_id": user.id},
        )
    else:
        token = secrets.token_urlsafe(_GENERATED_TOKEN_BYTES)
        # Defense in depth: an operator-supplied override is already registered for
        # redaction by `logging.setup.configure_logging` (it comes from `Settings`,
        # loaded before this runs); a generated token exists only in this local
        # variable and is never routed through `logging` by design (see
        # `_print_bootstrap_token_banner`) — registering it too means a future call
        # site that accidentally logs it gets caught by the same backstop, not a
        # silent leak.
        register_secret(token)
        credentials.create_api_token(
            user.id, token, label=_BOOTSTRAP_TOKEN_LABEL_GENERATED
        )
        _print_bootstrap_token_banner(token)
        logger.info(
            "created bootstrap admin identity (auto-generated token)",
            extra={"user_id": user.id},
        )
