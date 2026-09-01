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
    future second-factor type is a new member, not a schema change.
    """

    PASSWORD = "password"
    API_TOKEN = "api_token"


@dataclass(frozen=True, slots=True)
class Resource:
    """A row in `resources` — a stable, dotted, URL-independent identifier a route
    declares for itself (e.g. `heart.tick`), decoupled from the URL so renaming a route
    never orphans a stored permission grant.
    """

    name: str
    description: str
    registered_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            name=row["name"],
            description=row["description"],
            registered_at=row["registered_at"],
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
    """A row in `users`."""

    id: str
    username: str
    display_name: str
    active: bool
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            active=bool(row["active"]),
            created_at=row["created_at"],
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
    """

    id: str
    subject_type: SubjectType
    subject_id: str
    role_id: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(
            id=row["id"],
            subject_type=SubjectType(row["subject_type"]),
            subject_id=row["subject_id"],
            role_id=row["role_id"],
            created_at=row["created_at"],
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
