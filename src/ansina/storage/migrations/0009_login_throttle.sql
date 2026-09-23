-- Migration 0009: login throttle machinery (issue #49).
--
-- Standalone brute-force protection for POST /auth/login (#50) — built and tested here
-- with no caller yet, the same "primitive first, its caller next" sequencing #42 used
-- for role_mappings ahead of #43's login exchange.
--
-- Deliberately not sudo_lockouts (0003_sudo.sql): that table is keyed on user_id and
-- assumes an already-resolved caller stepping up. A login attempt has no resolved
-- user — an unknown username must throttle identically to a real one or the throttle
-- becomes a user-enumeration oracle, and an IP spraying many usernames must be caught
-- even though no single username bucket ever trips. Different key, different threat
-- model, so this is new machinery rather than a reuse.
--
-- Two departures from 0002_rbac.sql's conventions, both deliberate and both mirroring
-- 0003_sudo.sql's own reasoning:
--   1. No `strftime(...)` column defaults — every timestamp here is written from
--      `auth.login_throttle.LoginThrottle`'s injectable clock, not SQLite's own `now`,
--      so window/lockout expiry stays testable without real sleeping.
--   2. `key` holds the submitted, casefolded username or a plain IP string in the
--      clear — never a hash — since both are attacker-supplied inputs already, not
--      secrets to protect at rest.
--
-- One row per (scope, key): a failing login bumps both a 'username' row (keyed on the
-- submitted, casefolded username, whether or not such a user exists) and an 'ip' row
-- (keyed on the caller's IP) in the same call. No foreign key to `users` — that absence
-- is the entire point: a 'username' row must be able to exist for a username nobody
-- has ever registered.
CREATE TABLE login_attempts (
    scope           TEXT NOT NULL CHECK (scope IN ('username', 'ip')),
    key             TEXT NOT NULL,
    failed_count    INTEGER NOT NULL DEFAULT 0,
    first_failed_at TEXT,
    locked_until    TEXT,
    PRIMARY KEY (scope, key)
);

-- Backs `LoginAttemptRepository.delete_expired()` — a periodic sweep (a documented side
-- effect of `LoginThrottle.record_failure`, mirroring `OidcLoginService.start_login`'s
-- own sweep of `oidc_login_states`) of rows that are both unlocked and outside their
-- failure-streak window, so the table doesn't grow unbounded under sustained scanning.
CREATE INDEX idx_login_attempts_locked_until ON login_attempts (locked_until);
