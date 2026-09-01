-- Migration 0002: RBAC identity & permission model (issue #24).
--
-- Permissions are rows, not an enum: every authorization check (issue #25) answers one
-- question — "is there a role_permissions row for (one of the caller's roles, this
-- resource, this verb)?" — regardless of whether the role is builtin or, in a later
-- milestone, admin-defined. That is the whole point of this schema.

-- A stable, dotted, URL-independent identifier a route declares for itself (e.g.
-- "heart.tick"). Populated from a bootstrap constant in #24 (`auth.policy
-- .BOOTSTRAP_RESOURCES`), replaced by a real route-coverage walk in #25 — "what can a
-- role be granted on" is discoverable data, not something read out of source.
CREATE TABLE resources (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    registered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Builtin roles (read/write/maintain/admin) are seeded records, `builtin = 1`, and
-- reconciled at every boot (`auth.reconciler.reconcile_builtin_roles`). A future
-- admin-defined custom role is just another row here with `builtin = 0` — the
-- reconciler never touches those.
CREATE TABLE roles (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    builtin INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- The entire grant model. One row = "this role may issue this verb against this
-- resource." Deleting a role or a resource cascades away the grants that reference it.
CREATE TABLE role_permissions (
    role_id TEXT NOT NULL REFERENCES roles (id) ON DELETE CASCADE,
    resource TEXT NOT NULL REFERENCES resources (name) ON DELETE CASCADE,
    verb TEXT NOT NULL CHECK (verb IN ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')),
    PRIMARY KEY (role_id, resource, verb)
);

CREATE INDEX idx_role_permissions_resource_verb ON role_permissions (resource, verb);

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Quoted everywhere ("groups", not groups): GROUPS is a SQLite keyword (window-frame
-- syntax) and an unquoted reference is a syntax error, not a lookup of this table.
CREATE TABLE "groups" (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE user_groups (
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    group_id TEXT NOT NULL REFERENCES "groups" (id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, group_id)
);

CREATE INDEX idx_user_groups_group_id ON user_groups (group_id);

-- subject_id is deliberately not a foreign key: subject_type picks which table it
-- refers to (users or "groups"), and SQLite has no polymorphic FK. The repository
-- layer (`auth.repositories.RoleAssignmentRepository`) is responsible for verifying the
-- subject exists before inserting here — this is intentional, not an oversight.
CREATE TABLE role_assignments (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'group')),
    subject_id TEXT NOT NULL,
    role_id TEXT NOT NULL REFERENCES roles (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (subject_type, subject_id, role_id)
);

CREATE INDEX idx_role_assignments_subject ON role_assignments (subject_type, subject_id);

-- Typed from day one (`type`): M2 populates only 'password' and 'api_token' — this is
-- deliberately the same table a future second-factor credential type would use, not a
-- schema change waiting to happen. `salt` is NULL for 'password' rows (argon2id embeds
-- its own salt in the PHC-format hash string); 'api_token' rows always set it (see
-- `auth.hashing`, salted SHA-256).
CREATE TABLE credentials (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('password', 'api_token')),
    hash TEXT NOT NULL,
    salt TEXT,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_used_at TEXT,
    expires_at TEXT
);

CREATE INDEX idx_credentials_user_type ON credentials (user_id, type);

-- At most one password credential per user.
CREATE UNIQUE INDEX idx_credentials_one_password_per_user
    ON credentials (user_id)
    WHERE type = 'password';

-- Every M2 user gets exactly one provider = 'local' row (the bootstrap admin, issue
-- #24's one deliberate exception, gets 'local-bootstrap' instead — see
-- `auth.bootstrap`). This is the seam a future federated-login milestone hooks into by
-- writing new-provider rows against existing users, not by migrating this table.
CREATE TABLE external_identities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (provider, subject)
);

CREATE INDEX idx_external_identities_user_id ON external_identities (user_id);

-- Ships empty in M2 — only meaningful for a claims-based external provider. Its
-- existence now means a future milestone adds data, not a table.
CREATE TABLE role_mappings (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    claim TEXT NOT NULL,
    value TEXT NOT NULL,
    role_id TEXT NOT NULL REFERENCES roles (id) ON DELETE CASCADE
);
