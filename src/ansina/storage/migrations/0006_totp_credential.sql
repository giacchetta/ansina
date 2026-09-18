-- Migration 0006: TOTP credential type (issue #41).
--
-- `credentials.type`'s `CHECK (type IN ('password', 'api_token'))` has no `ALTER` path
-- in SQLite — widening a CHECK constraint requires the documented 12-step
-- table-rebuild procedure (https://www.sqlite.org/lang_altertable.html, "Making Other
-- Kinds Of Table Schema Changes"). Nothing references `credentials.id` via a foreign
-- key, so no other table needs touching as part of the rebuild.
--
-- Two column-meaning notes specific to a `totp` row, both deliberate reuse rather than
-- new columns:
--   - `hash` stores a `v1:<nonce>:<ciphertext>` AES-GCM envelope (see
--     `ansina.auth.encryption`), not a one-way hash — TOTP verification needs the
--     plaintext seed back, which a hash structurally cannot give it.
--   - `last_used_at` stores the last-accepted RFC 6238 step index as a plain integer
--     string, not an ISO timestamp — the anti-replay floor `ansina.auth.totp
--     .find_valid_step` checks candidate codes against. Every other credential type
--     keeps storing an ISO instant there unchanged; nothing generic reads this column
--     assuming one format across all types.

CREATE TABLE credentials_new (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('password', 'api_token', 'totp')),
    hash TEXT NOT NULL,
    salt TEXT,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_used_at TEXT,
    expires_at TEXT
);

INSERT INTO credentials_new
    (id, user_id, type, hash, salt, label, created_at, last_used_at, expires_at)
    SELECT id, user_id, type, hash, salt, label, created_at, last_used_at, expires_at
    FROM credentials;

DROP TABLE credentials;

ALTER TABLE credentials_new RENAME TO credentials;

CREATE INDEX idx_credentials_user_type ON credentials (user_id, type);

-- At most one password credential per user (carried over from 0002_rbac.sql).
CREATE UNIQUE INDEX idx_credentials_one_password_per_user
    ON credentials (user_id)
    WHERE type = 'password';

-- At most one totp credential per user, mirroring the password partial index above —
-- `POST /auth/me/totp` refuses (409) a second enrollment ahead of ever reaching this
-- constraint, but the database is the backstop, not the only guard.
CREATE UNIQUE INDEX idx_credentials_one_totp_per_user
    ON credentials (user_id)
    WHERE type = 'totp';
