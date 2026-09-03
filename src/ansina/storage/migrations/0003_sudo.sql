-- Migration 0003: sudo step-up grants & lockouts (issue #26).
--
-- Gives `Maintain` a way to earn `Principal.sudo_active = True` for a sensitive
-- `auth.*` action, mirroring Linux `sudo`: a short-lived, revocable grant issued after
-- re-verifying identity through a pluggable `auth.step_up.StepUpVerifier`.
--
-- Two departures from 0002_rbac.sql's conventions, both deliberate:
--   1. No `strftime(...)` column defaults — every timestamp here is written from
--      `auth.sudo.SudoService`'s injectable clock, not SQLite's own `now`, so grant
--      expiry and lockout windows stay testable without real sleeping.
--   2. `hash`/`salt`, never the raw grant token — the same salted-SHA-256 scheme
--      `credentials.api_token` rows already use (see `auth.hashing`), for the same
--      reason: a grant token is high-entropy, machine-generated, and verified on every
--      request that carries it, so argon2's deliberate work factor buys nothing here.

-- One active grant per user (`SudoGrantRepository.create` deletes any prior row for
-- the same user in the same transaction) — a re-step-up replaces, never accumulates.
-- `verifier` records which `StepUpVerifier` satisfied the step-up, so a grant issued
-- under a future second verifier stays distinguishable in an audit trail.
CREATE TABLE sudo_grants (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    hash       TEXT NOT NULL,
    salt       TEXT NOT NULL,
    verifier   TEXT NOT NULL,
    issued_at  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX idx_sudo_grants_user ON sudo_grants (user_id);

-- At most one row per user: N consecutive failed step-up attempts within
-- `[security.sudo] attempt_window_seconds` locks further attempts out until
-- `locked_until`. A failure older than the window resets `failed_count` to 1 rather
-- than accumulating across unrelated attempts long in the past.
CREATE TABLE sudo_lockouts (
    user_id         TEXT PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    failed_count    INTEGER NOT NULL DEFAULT 0,
    first_failed_at TEXT,
    locked_until    TEXT
);
