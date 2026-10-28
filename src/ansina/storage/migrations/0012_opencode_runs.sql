-- Migration 0012: opencode_runs (issue #77).
--
-- The first MUTABLE table this codebase ships, where every other table since
-- `sudo_grants` (0003_sudo.sql) through `heart_journal` (0010_heart_journal.sql) and
-- `login_attempts` (0009_login_throttle.sql) is either a snapshot row updated by one
-- narrow column (`sudo_lockouts`) or deliberately append-only. A run has a real
-- lifecycle with real state transitions (queued -> running -> one of
-- succeeded/failed/timeout/cancelled) that this row tracks as "what is true right
-- now" -- the APPEND-ONLY trace of that same lifecycle is the telemetry stream
-- (`kind=opencode/`, issue #76, `ansina.opencode.telemetry.OpencodeTelemetryWriter`),
-- not this table. `ansina.opencode.repository.OpencodeRunRepository.update_status()`
-- is the one mutator, and it refuses any transition out of a terminal status.
--
-- No foreign key on `retry_of` (a later retry action's self-reference) -- this table
-- has no owner beyond the daemon itself, the same reasoning `0009_login_throttle.sql`/
-- `0010_heart_journal.sql` already document for their own lack of one.
--
-- Every timestamp (`created_at`/`started_at`/`completed_at`) is caller-supplied ISO
-- 8601 via `ansina.auth.clock`'s `iso(utc_now())` -- never a SQL-side `now`, the
-- discipline every table in this codebase follows since `sudo_grants`.
CREATE TABLE opencode_runs (
    id                       TEXT PRIMARY KEY,
    created_at               TEXT NOT NULL,
    started_at               TEXT,
    completed_at             TEXT,
    status                   TEXT NOT NULL CHECK (status IN
        ('queued', 'running', 'succeeded', 'failed', 'timeout', 'cancelled')),
    session_id               TEXT,
    prompt                   TEXT NOT NULL,
    model                    TEXT,
    agent                    TEXT,
    exit_code                INTEGER,
    error_detail             TEXT NOT NULL DEFAULT '',
    event_count              INTEGER NOT NULL DEFAULT 0,
    tool_call_count          INTEGER NOT NULL DEFAULT 0,
    permission_denied_count  INTEGER NOT NULL DEFAULT 0,
    parse_error_count        INTEGER NOT NULL DEFAULT 0,
    prompt_tokens            INTEGER,
    completion_tokens        INTEGER,
    duration_seconds         REAL,
    retry_of                 TEXT,
    intervention_count       INTEGER NOT NULL DEFAULT 0
);

-- Backs `OpencodeRunRepository.list_recent()` (newest-first) and `.sweep()`'s
-- bounded-retention `LIMIT` -- `id DESC` as the tiebreaker, the same reasoning
-- `idx_heart_journal_created_at` already documents for a same-millisecond tie.
CREATE INDEX idx_opencode_runs_created_at ON opencode_runs (created_at DESC, id DESC);

-- Backs `OpencodeRunRepository.count_active()` (`status IN ('queued', 'running')`,
-- the `OpencodeRunService`'s own backlog check and a later issue's active-run count)
-- and the boot-time crash-recovery sweep (`sweep_stale_on_boot`).
CREATE INDEX idx_opencode_runs_status ON opencode_runs (status);
