"""Boot-time identity provisioning: two independent mechanisms, two different jobs.
See issue #24, redesigned by issue #28.

**The bootstrap identity** (`ensure_bootstrap_admin`): on first boot, Ansina provisions
a single synthetic Admin so the service is reachable at all — attributable in audit
logs (via its own `external_identities` row), disableable via config once a real Admin
user exists. As of issue #28 this is purely a break-glass credential: it is *always*
auto-generated, unconditionally, printed to stdout **exactly once**, and stores only
its salted hash — the plaintext is never written to config, never logged, and is not
recoverable afterward by any means, including reading the database. Its one token is
created once and never touched again by any code path — there is no operator override
and no rotation, ever. The only way to change its credential state is
`bootstrap_admin_enabled`: `False` revokes it, and `True` regenerates one if (and only
if) the identity is currently credential-less — this is now the *only* break-glass
path left, so it must be recoverable rather than permanently gone, but it never
touches a *live* credential.

**The configured admin** (`ensure_configured_admin`, new in issue #28): an ordinary,
`provider='local'` user, created once at first boot from
`ANSINA_SECURITY__ADMIN_USERNAME` + `ANSINA_SECURITY__API_TOKEN` when both are set
(`config.settings.SecuritySettings` guarantees they're set together or neither is).
It exists so a scripted install or CI gets a known, reproducible Admin credential
without ever touching the bootstrap identity's own credential or scraping the
once-only banner off stdout. Once created it is indistinguishable from a user created
through the TUI/API except for how its first credential arrived — it self-mints and
revokes its own additional tokens the ordinary way, via `POST`/`DELETE
/auth/me/tokens`, and is never rotated by this module. On any later boot where a
non-bootstrap user already exists, the two env vars are a silent no-op (one info log
line, not a boot failure) — a systemd unit or container that keeps the same
environment across every restart, the normal deployment shape, must not crash on a
routine one.

Both functions run after `auth.reconciler.reconcile_builtin_roles` in
`api.app.create_app`'s lifespan — the `admin` role must already exist before either
assigns it. Order between the two no longer matters functionally (see
`ensure_configured_admin`'s own docstring for why), but `ensure_bootstrap_admin` runs
first to keep the narrative order — the identity that always exists, then the one
that's opt-in.
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
from ansina.config.settings import RESERVED_BOOTSTRAP_USERNAME as _BOOTSTRAP_USERNAME
from ansina.logging import get_logger, register_secret
from ansina.storage.database import Database

if TYPE_CHECKING:
    from ansina.config.settings import Settings

logger = get_logger(__name__)

# The one deliberate exception to "every M2 user gets exactly one provider='local'
# row" (issue #24) — the bootstrap identity is distinguishable from a real local
# account precisely because it uses this provider instead. `_BOOTSTRAP_USERNAME`
# itself (imported above as `RESERVED_BOOTSTRAP_USERNAME`) lives in `config.settings`,
# not here, so `SecuritySettings.admin_username`'s reserved-name check can reference
# the same value without `config` gaining a dependency on `auth`.
BOOTSTRAP_PROVIDER = "local-bootstrap"
_BOOTSTRAP_TOKEN_LABEL = "bootstrap (auto-generated)"
_CONFIGURED_ADMIN_TOKEN_LABEL = "configured admin (ANSINA_SECURITY__API_TOKEN)"

# 32 raw bytes -> 43 base64url characters, ~256 bits of entropy.
_GENERATED_TOKEN_BYTES = 32

_BANNER = """
================================================================================
 Ansina bootstrap Admin API token — shown ONCE, then forgotten forever.

 Copy it now. It is not stored in plaintext anywhere (not in this log, not in
 the database, not in config) and cannot be recovered if lost — only replaced by
 disabling and re-enabling security.bootstrap_admin_enabled.

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


def _generate_and_store_bootstrap_token(
    credentials: CredentialRepository, user_id: str
) -> None:
    """Generates a fresh high-entropy token, stores only its hash, and prints the
    one-time banner. Shared by first-ever creation and by `bootstrap_admin_enabled`
    being re-enabled over a currently credential-less identity — both need the exact
    same "generate, store, reveal once" sequence.
    """
    token = secrets.token_urlsafe(_GENERATED_TOKEN_BYTES)
    # Defense in depth: nothing is designed to log a generated token (see
    # `_print_bootstrap_token_banner`'s own docstring), but registering it means a
    # future call site that accidentally does gets caught by the same backstop
    # `logging.redaction` already provides for an operator-supplied secret.
    register_secret(token)
    credentials.create_api_token(user_id, token, label=_BOOTSTRAP_TOKEN_LABEL)
    _print_bootstrap_token_banner(token)


def is_bootstrap_identity(db: Database, user_id: str) -> bool:
    """`True` iff `user_id` is the synthetic bootstrap identity — the one user this
    codebase treats specially rather than as an ordinary account.

    Used by issue #28's self-service token routes
    (`ansina.auth.management.assert_not_bootstrap_identity`) to refuse a second token
    or a delete of its one token via the API. The configured admin
    (`ensure_configured_admin` below) is deliberately *not* subject to this check — by
    design it's an ordinary user from the moment it's created.
    """
    identity = ExternalIdentityRepository(db).get_by_provider_subject(
        BOOTSTRAP_PROVIDER, _BOOTSTRAP_USERNAME
    )
    return identity is not None and identity.user_id == user_id


def ensure_bootstrap_admin(db: Database, settings: Settings) -> None:
    """Resolve the bootstrap Admin identity — see module docstring for the full
    lifecycle. Concretely, in order:

    - `security.enabled = False`: no-op entirely — dev mode, no authentication of any
      kind is enforced, so no credential is needed.
    - `security.bootstrap_admin_enabled = False`: revoke the bootstrap identity's
      `api_token` credential (so it stops authenticating) while keeping the user and
      its `external_identities` row, so historic audit log lines referring to it stay
      attributable.
    - Bootstrap identity already exists and currently holds a live `api_token`
      credential: untouched. No override, no rotation — issue #28 removed both.
    - Bootstrap identity already exists but currently holds *no* `api_token`
      credential (revoked earlier, or an out-of-band deletion): regenerate one and
      print the banner again — this is the only break-glass path left, so it must be
      recoverable rather than permanently gone.
    - No bootstrap identity yet and `users` already has a non-bootstrap row: no-op —
      never create a second bootstrap identity.
    - Otherwise (genuine first boot): create it, assign `admin`, generate and print a
      fresh token.
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

    if identity is not None:
        if not credentials.list_api_tokens(identity.user_id):
            _generate_and_store_bootstrap_token(credentials, identity.user_id)
            logger.info(
                "regenerated bootstrap admin credential (was credential-less — "
                "bootstrap_admin_enabled re-enabled, or an out-of-band deletion)",
                extra={"user_id": identity.user_id},
            )
        # A live credential is never touched — no override, no rotation (issue #28).
        return

    if users.list_all():
        # A real user already exists (the configured admin below, one created via a
        # management API in a later milestone, or a previous bootstrap run whose
        # identity row was since removed by hand) — never create a second bootstrap
        # identity.
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

    _generate_and_store_bootstrap_token(credentials, user.id)
    logger.info(
        "created bootstrap admin identity (auto-generated token)",
        extra={"user_id": user.id},
    )


def ensure_configured_admin(db: Database, settings: Settings) -> None:
    """Issue #28: provisions the *configured admin* — an ordinary Admin user, created
    once, at first boot, from `ANSINA_SECURITY__ADMIN_USERNAME` +
    `ANSINA_SECURITY__API_TOKEN`. See module docstring for the full rationale.
    `config.settings.SecuritySettings` already guarantees the two are set together or
    neither is, so this only needs to check whether they're set at all.

    "First boot" here means "no user other than the bootstrap identity exists yet" —
    deliberately *not* "the `users` table is empty". `ensure_bootstrap_admin` runs
    immediately before this in `api.app.create_app`'s lifespan and, on a genuine first
    boot, has already inserted the bootstrap identity's own `users` row by the time
    this runs — gating on an empty table would see that row and conclude it's *not*
    the first boot, on every first boot, unconditionally. Excluding the bootstrap
    identity's own `user_id` from the check makes this correct regardless of
    execution order between the two functions, and regardless of whether the
    bootstrap identity was created this boot or a previous one.

    On any later boot where a non-bootstrap user already exists (ordinarily: the
    configured admin created on a previous boot), the env vars are silently ignored —
    a systemd unit or container that keeps the same environment across every restart
    must not crash on a routine one. There is no rotation past this point; the
    SysAdmin rotates this identity's token the ordinary way, via
    `POST`/`DELETE /auth/me/tokens`, once logged in with it.
    """
    if not settings.security.enabled:
        return
    username = settings.security.admin_username
    token = settings.security.api_token
    if username is None or token is None:
        return

    bootstrap_identity = ExternalIdentityRepository(db).get_by_provider_subject(
        BOOTSTRAP_PROVIDER, _BOOTSTRAP_USERNAME
    )
    bootstrap_user_id = (
        bootstrap_identity.user_id if bootstrap_identity is not None else None
    )
    non_bootstrap_users = [
        user for user in UserRepository(db).list_all() if user.id != bootstrap_user_id
    ]
    if non_bootstrap_users:
        logger.info(
            "ANSINA_SECURITY__ADMIN_USERNAME/API_TOKEN set but ignored — not the "
            "first boot (a non-bootstrap user already exists); rotate this "
            "identity's token via POST /auth/me/tokens instead"
        )
        return

    user = UserRepository(db).create(username)
    admin_role = RoleRepository(db).get_by_slug(RoleSlug.ADMIN.value)
    assert admin_role is not None  # reconcile_builtin_roles runs before this, always
    RoleAssignmentRepository(db).assign(SubjectType.USER, user.id, admin_role.id)

    raw_token = token.get_secret_value()
    # Defense in depth, same reasoning as `_generate_and_store_bootstrap_token` — this
    # value is already registered by `logging.setup.configure_logging` (it comes from
    # `Settings`, loaded before this runs), but a second registration here is harmless
    # and keeps this module's own posture consistent regardless of boot order.
    register_secret(raw_token)
    CredentialRepository(db).create_api_token(
        user.id, raw_token, label=_CONFIGURED_ADMIN_TOKEN_LABEL
    )
    logger.info(
        "created configured admin identity from "
        "ANSINA_SECURITY__ADMIN_USERNAME/API_TOKEN",
        extra={"user_id": user.id, "username": username},
    )
