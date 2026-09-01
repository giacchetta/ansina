"""CRUD on Users/Groups/Roles/Permissions. See issue #24.

The first domain repository in the codebase — built directly on
`Database.connection()`/`Database.transaction()`, per the existing conventions in
`ansina.storage` (no premature ORM). Data-layer only: no HTTP routes here, those are
#25/#27.

Every id is a `uuid.uuid4().hex` string, the same convention `logging.context` already
uses for request ids — no autoincrement integer identity leaks through this layer.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import ClassVar

from ansina.auth.hashing import (
    Argon2Params,
    hash_password,
    hash_token,
    new_token_salt,
    verify_password,
    verify_token_hash,
)
from ansina.auth.models import (
    Credential,
    CredentialType,
    ExternalIdentity,
    Group,
    Resource,
    Role,
    RoleAssignment,
    RolePermission,
    SubjectType,
    User,
    Verb,
)
from ansina.errors import AuthError
from ansina.storage.database import Database


class BuiltinRoleError(AuthError):
    """A caller tried to delete a builtin role or clear its `builtin` flag."""

    code: ClassVar[str] = "ansina.auth.builtin_role_immutable"


class DuplicateError(AuthError):
    """A unique constraint (username, group slug, provider/subject, ...) was
    violated.
    """

    code: ClassVar[str] = "ansina.auth.duplicate"


class UnknownSubjectError(AuthError):
    """A `role_assignments` row was requested for a subject id that doesn't exist in
    `users` or `"groups"` — `subject_id` is deliberately not a foreign key (SQLite has
    no polymorphic FK), so this repository enforces the reference itself.
    """

    code: ClassVar[str] = "ansina.auth.unknown_subject"


def _new_id() -> str:
    return uuid.uuid4().hex


class ResourceRepository:
    """CRUD on `resources` — the catalog `auth.reconciler` reconciles builtin-role
    grants against.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def list_all(self) -> list[Resource]:
        rows = (
            self._db.connection()
            .execute("SELECT * FROM resources ORDER BY name")
            .fetchall()
        )
        return [Resource.from_row(row) for row in rows]

    def upsert(self, name: str, description: str) -> Resource:
        with self._db.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO resources (name, description) VALUES (?, ?)
                ON CONFLICT (name) DO UPDATE SET description = excluded.description
                """,
                (name, description),
            )
            row = cursor.execute(
                "SELECT * FROM resources WHERE name = ?", (name,)
            ).fetchone()
        return Resource.from_row(row)

    def delete(self, name: str) -> None:
        """Cascades to `role_permissions` rows referencing this resource."""
        with self._db.transaction() as cursor:
            cursor.execute("DELETE FROM resources WHERE name = ?", (name,))


class RoleRepository:
    """CRUD on `roles`. Builtin roles (`builtin=True`) can never be deleted or demoted
    to non-builtin through this repository — both raise `BuiltinRoleError`.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def list_all(self) -> list[Role]:
        rows = (
            self._db.connection()
            .execute("SELECT * FROM roles ORDER BY slug")
            .fetchall()
        )
        return [Role.from_row(row) for row in rows]

    def get(self, role_id: str) -> Role | None:
        row = (
            self._db.connection()
            .execute("SELECT * FROM roles WHERE id = ?", (role_id,))
            .fetchone()
        )
        return Role.from_row(row) if row is not None else None

    def get_by_slug(self, slug: str) -> Role | None:
        row = (
            self._db.connection()
            .execute("SELECT * FROM roles WHERE slug = ?", (slug,))
            .fetchone()
        )
        return Role.from_row(row) if row is not None else None

    def create(
        self, slug: str, name: str, description: str, *, builtin: bool = False
    ) -> Role:
        role_id = _new_id()
        try:
            with self._db.transaction() as cursor:
                cursor.execute(
                    """
                    INSERT INTO roles (id, slug, name, description, builtin)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (role_id, slug, name, description, int(builtin)),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateError(f"a role with slug {slug!r} already exists") from exc
        role = self.get(role_id)
        assert role is not None  # unreachable — just inserted, in a committed txn
        return role

    def ensure_builtin(self, slug: str, name: str, description: str) -> Role:
        """Idempotent seed/verify for one builtin role — creates it if missing,
        otherwise leaves it as-is (its `builtin` flag is already `True` by
        construction, since only this method and `create(builtin=True)` ever set it).
        Used exclusively by `auth.reconciler.reconcile_builtin_roles`.
        """
        existing = self.get_by_slug(slug)
        if existing is not None:
            return existing
        return self.create(slug, name, description, builtin=True)

    def delete(self, role_id: str) -> None:
        role = self.get(role_id)
        if role is None:
            return
        if role.builtin:
            raise BuiltinRoleError(
                f"role {role.slug!r} is builtin and cannot be deleted"
            )
        with self._db.transaction() as cursor:
            cursor.execute("DELETE FROM roles WHERE id = ?", (role_id,))


class RolePermissionRepository:
    """CRUD on `role_permissions` — the entire grant model."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def list_for_role(self, role_id: str) -> list[RolePermission]:
        rows = (
            self._db.connection()
            .execute(
                "SELECT * FROM role_permissions WHERE role_id = ? "
                "ORDER BY resource, verb",
                (role_id,),
            )
            .fetchall()
        )
        return [RolePermission.from_row(row) for row in rows]

    def grant(self, role_id: str, resource: str, verb: Verb) -> None:
        with self._db.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO role_permissions (role_id, resource, verb)
                VALUES (?, ?, ?)
                ON CONFLICT (role_id, resource, verb) DO NOTHING
                """,
                (role_id, resource, verb.value),
            )

    def revoke(self, role_id: str, resource: str, verb: Verb) -> None:
        with self._db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM role_permissions "
                "WHERE role_id = ? AND resource = ? AND verb = ?",
                (role_id, resource, verb.value),
            )

    def effective_verbs(
        self, role_ids: frozenset[str], resource: str
    ) -> frozenset[Verb]:
        """Every verb granted on `resource` to any of `role_ids` — the read side of
        #25's authorization question. Empty `role_ids` short-circuits to no grants,
        never a query that matches every row.
        """
        if not role_ids:
            return frozenset()
        placeholders = ",".join("?" for _ in role_ids)
        rows = (
            self._db.connection()
            .execute(
                f"SELECT DISTINCT verb FROM role_permissions "
                f"WHERE resource = ? AND role_id IN ({placeholders})",
                (resource, *role_ids),
            )
            .fetchall()
        )
        return frozenset(Verb(row["verb"]) for row in rows)


class UserRepository:
    """CRUD on `users`. `create()` also inserts the user's single
    `provider="local"` `external_identities` row in the same transaction — that
    invariant (issue #24's "every M2 user gets exactly one local row" AC) is
    structurally guaranteed here, not left as a convention callers must remember.

    The one caller that opts out is `auth.bootstrap.ensure_bootstrap_admin`: the
    synthetic Admin gets a `provider="local-bootstrap"` row instead of `"local"` (it
    authenticates via the configured `api_token`, not a password-login-shaped local
    account), so `create()` takes `local_identity=False` there rather than inserting a
    row the caller immediately has to reconcile against a second one it adds itself.
    """

    _LOCAL_PROVIDER: ClassVar[str] = "local"

    def __init__(self, db: Database) -> None:
        self._db = db

    def list_all(self) -> list[User]:
        rows = (
            self._db.connection()
            .execute("SELECT * FROM users ORDER BY username")
            .fetchall()
        )
        return [User.from_row(row) for row in rows]

    def get(self, user_id: str) -> User | None:
        row = (
            self._db.connection()
            .execute("SELECT * FROM users WHERE id = ?", (user_id,))
            .fetchone()
        )
        return User.from_row(row) if row is not None else None

    def get_by_username(self, username: str) -> User | None:
        row = (
            self._db.connection()
            .execute("SELECT * FROM users WHERE username = ?", (username,))
            .fetchone()
        )
        return User.from_row(row) if row is not None else None

    def create(
        self, username: str, *, display_name: str = "", local_identity: bool = True
    ) -> User:
        user_id = _new_id()
        try:
            with self._db.transaction() as cursor:
                cursor.execute(
                    "INSERT INTO users (id, username, display_name) VALUES (?, ?, ?)",
                    (user_id, username, display_name),
                )
                if local_identity:
                    cursor.execute(
                        """
                        INSERT INTO external_identities
                            (id, user_id, provider, subject)
                        VALUES (?, ?, ?, ?)
                        """,
                        (_new_id(), user_id, self._LOCAL_PROVIDER, username),
                    )
        except sqlite3.IntegrityError as exc:
            raise DuplicateError(
                f"a user with username {username!r} already exists"
            ) from exc
        user = self.get(user_id)
        assert user is not None  # unreachable — just inserted, in a committed txn
        return user

    def set_active(self, user_id: str, *, active: bool) -> None:
        with self._db.transaction() as cursor:
            cursor.execute(
                "UPDATE users SET active = ? WHERE id = ?", (int(active), user_id)
            )


class GroupRepository:
    """CRUD on `"groups"` — always quoted (`GROUPS` is a SQLite keyword)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def list_all(self) -> list[Group]:
        rows = (
            self._db.connection()
            .execute('SELECT * FROM "groups" ORDER BY slug')
            .fetchall()
        )
        return [Group.from_row(row) for row in rows]

    def get(self, group_id: str) -> Group | None:
        row = (
            self._db.connection()
            .execute('SELECT * FROM "groups" WHERE id = ?', (group_id,))
            .fetchone()
        )
        return Group.from_row(row) if row is not None else None

    def create(self, slug: str, name: str, *, description: str = "") -> Group:
        group_id = _new_id()
        try:
            with self._db.transaction() as cursor:
                cursor.execute(
                    'INSERT INTO "groups" (id, slug, name, description) '
                    "VALUES (?, ?, ?, ?)",
                    (group_id, slug, name, description),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateError(f"a group with slug {slug!r} already exists") from exc
        group = self.get(group_id)
        assert group is not None  # unreachable — just inserted, in a committed txn
        return group

    def add_member(self, group_id: str, user_id: str) -> None:
        with self._db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO user_groups (user_id, group_id) VALUES (?, ?) "
                "ON CONFLICT (user_id, group_id) DO NOTHING",
                (user_id, group_id),
            )


class RoleAssignmentRepository:
    """CRUD on `role_assignments` — direct-or-group grants. `roles_for_user` is the
    union query #25 depends on: every role reachable directly, plus every role reachable
    via group membership.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def _subject_exists(self, subject_type: SubjectType, subject_id: str) -> bool:
        table = "users" if subject_type is SubjectType.USER else '"groups"'
        row = (
            self._db.connection()
            .execute(f"SELECT 1 FROM {table} WHERE id = ?", (subject_id,))
            .fetchone()
        )
        return row is not None

    def assign(
        self, subject_type: SubjectType, subject_id: str, role_id: str
    ) -> RoleAssignment:
        if not self._subject_exists(subject_type, subject_id):
            raise UnknownSubjectError(
                f"no {subject_type.value} with id {subject_id!r} exists"
            )
        assignment_id = _new_id()
        with self._db.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO role_assignments (id, subject_type, subject_id, role_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (subject_type, subject_id, role_id) DO NOTHING
                """,
                (assignment_id, subject_type.value, subject_id, role_id),
            )
            row = cursor.execute(
                "SELECT * FROM role_assignments "
                "WHERE subject_type = ? AND subject_id = ? AND role_id = ?",
                (subject_type.value, subject_id, role_id),
            ).fetchone()
        return RoleAssignment.from_row(row)

    def unassign(
        self, subject_type: SubjectType, subject_id: str, role_id: str
    ) -> None:
        with self._db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM role_assignments "
                "WHERE subject_type = ? AND subject_id = ? AND role_id = ?",
                (subject_type.value, subject_id, role_id),
            )

    def roles_for_user(self, user_id: str) -> list[Role]:
        """Every role reachable by `user_id`, direct or via group membership."""
        rows = (
            self._db.connection()
            .execute(
                """
            SELECT DISTINCT r.* FROM roles r
            JOIN role_assignments ra ON ra.role_id = r.id
            WHERE (ra.subject_type = 'user' AND ra.subject_id = ?)
               OR (ra.subject_type = 'group' AND ra.subject_id IN (
                     SELECT group_id FROM user_groups WHERE user_id = ?
                   ))
            ORDER BY r.slug
            """,
                (user_id, user_id),
            )
            .fetchall()
        )
        return [Role.from_row(row) for row in rows]


class CredentialRepository:
    """CRUD on `credentials`. Passwords are argon2id (see `auth.hashing`); API tokens
    are salted SHA-256, looked up by scanning active `api_token` rows and comparing with
    `hmac.compare_digest` via `auth.hashing.verify_token_hash`.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def set_password(
        self, user_id: str, raw_password: str, params: Argon2Params
    ) -> Credential:
        """Replaces any existing password credential — `credentials` has a partial
        unique index enforcing at most one `PASSWORD` row per user.
        """
        password_hash = hash_password(raw_password, params)
        credential_id = _new_id()
        with self._db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM credentials WHERE user_id = ? AND type = 'password'",
                (user_id,),
            )
            cursor.execute(
                """
                INSERT INTO credentials (id, user_id, type, hash, salt)
                VALUES (?, ?, 'password', ?, NULL)
                """,
                (credential_id, user_id, password_hash),
            )
            row = cursor.execute(
                "SELECT * FROM credentials WHERE id = ?", (credential_id,)
            ).fetchone()
        return Credential.from_row(row)

    def verify_password(
        self, user_id: str, raw_password: str, params: Argon2Params
    ) -> bool:
        row = (
            self._db.connection()
            .execute(
                "SELECT * FROM credentials WHERE user_id = ? AND type = 'password'",
                (user_id,),
            )
            .fetchone()
        )
        if row is None:
            return False
        return verify_password(raw_password, row["hash"], params)

    def create_api_token(
        self, user_id: str, raw_token: str, *, label: str = ""
    ) -> Credential:
        salt = new_token_salt()
        token_hash = hash_token(raw_token, salt)
        credential_id = _new_id()
        with self._db.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO credentials (id, user_id, type, hash, salt, label)
                VALUES (?, ?, 'api_token', ?, ?, ?)
                """,
                (credential_id, user_id, token_hash, salt, label),
            )
            row = cursor.execute(
                "SELECT * FROM credentials WHERE id = ?", (credential_id,)
            ).fetchone()
        return Credential.from_row(row)

    def replace_api_token(
        self, user_id: str, raw_token: str, *, label: str = ""
    ) -> Credential:
        """Deletes every existing `api_token` credential for `user_id` and issues a
        fresh one. Used by `auth.bootstrap` to keep the synthetic Admin's token in sync
        with a rotated `ANSINA_SECURITY__API_TOKEN`.
        """
        with self._db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM credentials WHERE user_id = ? AND type = 'api_token'",
                (user_id,),
            )
        return self.create_api_token(user_id, raw_token, label=label)

    def delete_credentials(self, user_id: str, credential_type: CredentialType) -> None:
        with self._db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM credentials WHERE user_id = ? AND type = ?",
                (user_id, credential_type.value),
            )

    def find_user_by_api_token(self, token: str) -> User | None:
        """Scans every active `api_token` credential, comparing in constant time.
        Fine at M2 scale — a token-id-prefix index is the change to make if this table
        ever grows large enough for the scan to matter.
        """
        rows = (
            self._db.connection()
            .execute(
                "SELECT user_id, hash, salt FROM credentials WHERE type = 'api_token'"
            )
            .fetchall()
        )
        for row in rows:
            if row["salt"] is not None and verify_token_hash(
                token, row["salt"], row["hash"]
            ):
                user_row = (
                    self._db.connection()
                    .execute("SELECT * FROM users WHERE id = ?", (row["user_id"],))
                    .fetchone()
                )
                return User.from_row(user_row) if user_row is not None else None
        return None


class ExternalIdentityRepository:
    """CRUD on `external_identities`."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def get_by_provider_subject(
        self, provider: str, subject: str
    ) -> ExternalIdentity | None:
        row = (
            self._db.connection()
            .execute(
                "SELECT * FROM external_identities WHERE provider = ? AND subject = ?",
                (provider, subject),
            )
            .fetchone()
        )
        return ExternalIdentity.from_row(row) if row is not None else None

    def list_for_user(self, user_id: str) -> list[ExternalIdentity]:
        rows = (
            self._db.connection()
            .execute(
                "SELECT * FROM external_identities WHERE user_id = ? ORDER BY provider",
                (user_id,),
            )
            .fetchall()
        )
        return [ExternalIdentity.from_row(row) for row in rows]

    def create(self, user_id: str, provider: str, subject: str) -> ExternalIdentity:
        identity_id = _new_id()
        try:
            with self._db.transaction() as cursor:
                cursor.execute(
                    """
                    INSERT INTO external_identities (id, user_id, provider, subject)
                    VALUES (?, ?, ?, ?)
                    """,
                    (identity_id, user_id, provider, subject),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateError(
                f"identity ({provider!r}, {subject!r}) already exists"
            ) from exc
        identity = self.get_by_provider_subject(provider, subject)
        assert identity is not None  # unreachable — just inserted, in a committed txn
        return identity
