-- Migration 0010: heart_journal (issue #55).
--
-- A tick leaves a trace an operator can read: one row per completed tick, written by
-- `heart.tick.journal_handler.JournalDecisionHandler` (replacing the log-only
-- `LoggingDecisionHandler` as `build_tick_loop`'s default).
--
-- No foreign key — this table has no owner beyond the daemon itself, the same
-- reasoning `0009_login_throttle.sql`'s `login_attempts` already documents for its own
-- lack of one.
--
-- `created_at` is caller-supplied ISO 8601 via `ansina.auth.clock`'s `iso(utc_now())` —
-- never a SQL-side `now`, the discipline every table in this codebase follows since
-- `sudo_grants` (0003_sudo.sql).
CREATE TABLE heart_journal (
    id               TEXT PRIMARY KEY,
    created_at       TEXT NOT NULL,
    tick_number      INTEGER NOT NULL,
    decision         TEXT NOT NULL CHECK (decision IN ('idle', 'act', 'escalate')),
    note             TEXT NOT NULL DEFAULT '',
    prompt_tokens    INTEGER NOT NULL,
    duration_seconds REAL NOT NULL
);

-- Backs both `HeartJournalRepository.list_recent()` (newest-first) and `.sweep()`'s
-- bounded-retention `LIMIT` — `id DESC` as the tiebreaker because `created_at`'s
-- millisecond precision lets two rows written in the same millisecond tie, which
-- `created_at` alone would order nondeterministically.
CREATE INDEX idx_heart_journal_created_at ON heart_journal (created_at DESC, id DESC);
