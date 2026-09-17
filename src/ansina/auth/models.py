"""The RBAC identity/permission domain model. See issue #24.

Frozen dataclasses, one per table in `storage/migrations/0002_rbac.sql`, plus the
`StrEnum`s that constrain their string-typed columns. Each dataclass owns a
`from_row(sqlite3.Row) -> Self` classmethod — the one place a `Database` row's shape is
translated into a typed value, so `ansina.auth.repositories` never hand-unpacks a
`sqlite3.Row` itself.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Self


class Verb(StrEnum):
    """HTTP methods `role_permissions` can grant. Read from `request.method` by #25's
    `require()` dependency, never declared per-route — see issue #24/#25's design.
    """

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


# Every verb but GET — `policy.permitted_verbs` builds each role's grant set as a subset
# of this plus GET, so the fixed policy (Read/Write/Maintain/Admin) reads as one
# incremental statement instead of four independently spelled-out sets.
MUTATING_VERBS: frozenset[Verb] = frozenset(
    {Verb.POST, Verb.PUT, Verb.PATCH, Verb.DELETE}
)

# Canonical declaration order — used everywhere a set of verbs needs a stable, readable
# ordering: encoding a `resources.verbs` column value and rendering `GET /auth/
# permissions`'s own `verbs` list (issue #38).
_VERB_ORDER: tuple[Verb, ...] = tuple(Verb)


def ordered_verbs(verbs: frozenset[Verb]) -> tuple[Verb, ...]:
    """`verbs`, in `Verb`'s declaration order (GET, POST, PUT, PATCH, DELETE) — the one
    ordering every verb-list-shaped output uses, so two call sites never disagree.
    """
    return tuple(verb for verb in _VERB_ORDER if verb in verbs)


def encode_verbs(verbs: frozenset[Verb]) -> str:
    """A `resources.verbs` column value: a comma-separated, canonically ordered list
    (e.g. `"GET,POST,PATCH"`), empty string for no served verbs.
    """
    return ",".join(verb.value for verb in ordered_verbs(verbs))


def decode_verbs(raw: str) -> frozenset[Verb]:
    """The inverse of `encode_verbs` — tolerates the empty string (no verbs)."""
    if not raw:
        return frozenset()
    return frozenset(Verb(part) for part in raw.split(","))


class RoleSlug(StrEnum):
    """The four builtin roles (issue #24). Not compared by ordinal anywhere — every
    authorization check is a `role_permissions` row lookup keyed on `role_id`, not a
    comparison against this enum's declaration order.
    """

    READ = "read"
    WRITE = "write"
    MAINTAIN = "maintain"
    ADMIN = "admin"


class SubjectType(StrEnum):
    """Who a `role_assignments` row grants a role to — a user directly, or every member
    of a group.
    """

    USER = "user"
    GROUP = "group"


class CredentialType(StrEnum):
    """What a `credentials` row authenticates. Typed from day one (issue #24) so a
    future second-factor type is a new member, not a schema change. `TOTP` (issue #41)
    is the first to actually exercise that — its `credentials.type` CHECK widening
    (`storage/migrations/0006_totp_credential.sql`) is the schema change; the enum
    itself only grows a member.
    """

    PASSWORD = "password"
    API_TOKEN = "api_token"
    TOTP = "totp"


@dataclass(frozen=True, slots=True)
class Resource:
    """A row in `resources` — a stable, dotted, URL-independent identifier a route
    declares for itself (e.g. `heart.tick`), decoupled from the URL so renaming a route
    never orphans a stored permission grant. `verbs` (issue #38) is the set of HTTP
    methods a route actually answers to for this resource — not every grantable
    `Verb`, which `GET /auth/permissions` used to assume.
    """

    name: str
    description: str
    registered_at: str
    verbs: frozenset[Verb]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            name=row["name"],
            description=row["description"],
            registered_at=row["registered_at"],
            verbs=decode_verbs(row["verbs"]),
        )


@dataclass(frozen=True, slots=True)
class Role:
    """A row in `roles`. `builtin=True` roles are reconciled at every boot
    (`auth.reconciler.reconcile_builtin_roles`) and cannot be deleted or have `builtin`
    cleared through the repository layer.
    """

    id: str
    slug: str
    name: str
    description: str
    builtin: bool
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            description=row["description"],
            builtin=bool(row["builtin"]),
            created_at=row["created_at"],
        )


@dataclass(frozen=True, slots=True)
class RolePermission:
    """A row in `role_permissions` — the entire grant model. One row means "this role
    may issue this verb against this resource."
    """

    role_id: str
    resource: str
    verb: Verb

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            role_id=row["role_id"],
            resource=row["resource"],
            verb=Verb(row["verb"]),
        )


@dataclass(frozen=True, slots=True)
class User:
    """A row in `users`. `deleted_at` (issue #27) is a one-way tombstone, distinct
    from `active`'s suspend/resume flag — see `storage/migrations/
    0004_user_tombstone.sql` for why the two can't be merged into one column.
    """

    id: str
    username: str
    display_name: str
    active: bool
    created_at: str
    deleted_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            active=bool(row["active"]),
            created_at=row["created_at"],
            deleted_at=row["deleted_at"],
        )


@dataclass(frozen=True, slots=True)
class Group:
    """A row in `"groups"` (quoted in every query — `GROUPS` is a SQLite keyword)."""

    id: str
    slug: str
    name: str
    description: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            description=row["description"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    """A row in `role_assignments`. `subject_id` refers to a `users.id` or
    `"groups".id` depending on `subject_type` — deliberately not a foreign key (SQLite
    has no polymorphic FK); the repository layer verifies the subject exists.
    `source` (issue #42) is `'local'` for a row created through the ordinary
    role-assignment routes, or a provider identifier for one
    `ansina.auth.role_sync.sync_mapped_roles` created and owns.
    """

    id: str
    subject_type: SubjectType
    subject_id: str
    role_id: str
    created_at: str
    source: str = "local"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            id=row["id"],
            subject_type=SubjectType(row["subject_type"]),
            subject_id=row["subject_id"],
            role_id=row["role_id"],
            created_at=row["created_at"],
            source=row["source"],
        )


@dataclass(frozen=True, slots=True)
class RoleMapping:
    """A row in `role_mappings` (issue #42) — an IdP claim value mapped onto a role.
    `ansina.auth.role_sync.sync_mapped_roles` resolves which of a login's claims match
    a `(provider, claim, value)` triple and reconciles the user's `role_assignments`
    rows for that `provider` to match exactly. `(provider, claim, value, role_id)` is
    unique (`idx_role_mappings_tuple`), so a duplicate submission is a 409, not a
    second, redundant row.
    """

    id: str
    provider: str
    claim: str
    value: str
    role_id: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            id=row["id"],
            provider=row["provider"],
            claim=row["claim"],
            value=row["value"],
            role_id=row["role_id"],
        )


@dataclass(frozen=True, slots=True)
class Credential:
    """A row in `credentials`. `hash`/`salt` never carry a raw secret — see
    `ansina.auth.hashing`. `salt` is `None` for `PASSWORD` rows (argon2id embeds its own
    salt in the PHC-format hash string); `API_TOKEN` rows always set it.
    """

    id: str
    user_id: str
    type: CredentialType
    hash: str
    salt: str | None
    label: str
    created_at: str
    last_used_at: str | None
    expires_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            type=CredentialType(row["type"]),
            hash=row["hash"],
            salt=row["salt"],
            label=row["label"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            expires_at=row["expires_at"],
        )


@dataclass(frozen=True, slots=True)
class SudoGrant:
    """A row in `sudo_grants` (issue #26). `hash`/`salt` never carry the raw grant
    token — same salted-SHA-256 scheme as an `api_token` `Credential` (see
    `ansina.auth.hashing`). `verifier` names which `StepUpVerifier` satisfied the
    step-up that issued this grant; `revoked_at` is `None` until `DELETE /auth/sudo`
    (or the break-glass `DELETE /auth/sudo/grants`) revokes it early.
    """

    id: str
    user_id: str
    hash: str
    salt: str
    verifier: str
    issued_at: str
    expires_at: str
    revoked_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            hash=row["hash"],
            salt=row["salt"],
            verifier=row["verifier"],
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )


@dataclass(frozen=True, slots=True)
class SudoLockout:
    """A row in `sudo_lockouts` (issue #26) — at most one per user. `locked_until`
    is `None` until `failed_count` reaches `[security.sudo] max_failed_attempts`.
    """

    user_id: str
    failed_count: int
    first_failed_at: str | None
    locked_until: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            user_id=row["user_id"],
            failed_count=row["failed_count"],
            first_failed_at=row["first_failed_at"],
            locked_until=row["locked_until"],
        )


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    """A row in `external_identities`. Every M2-created user gets exactly one
    `provider="local"` row; the bootstrap admin (`auth.bootstrap`) gets
    `provider="local-bootstrap"` instead — the one deliberate exception.
    """

    id: str
    user_id: str
    provider: str
    subject: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            provider=row["provider"],
            subject=row["subject"],
            created_at=row["created_at"],
        )
