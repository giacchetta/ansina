-- Migration 0011: brain-call outcome columns on heart_journal (issue #64).
--
-- Wiring `escalate` -> `BrainProvider.stream()` needs a durable record of what
-- happened when the Brain was actually invoked (or why it wasn't) -- additive columns
-- on the same table rather than a new one, so `GET /heart/journal` stays the single
-- source of truth for "what did a tick do" with no second table to join.
--
-- Every column is nullable with no default beyond NULL (`brain_detail` aside): every
-- row written before this migration, and every non-escalate row written after it, has
-- no Brain interaction at all -- NULL means "not applicable", not "unknown".
--
-- Plain `ALTER TABLE ... ADD COLUMN`, including one with a `CHECK` -- unlike
-- `0006_totp_credential.sql`'s 12-step rebuild, that dance is only needed to widen an
-- *existing* `CHECK` (SQLite has no `ALTER CONSTRAINT`); adding a brand new column
-- with its own new `CHECK` is a plain, directly-supported `ALTER TABLE` operation.
ALTER TABLE heart_journal ADD COLUMN brain_status TEXT
    CHECK (brain_status IS NULL OR brain_status IN ('called', 'declined', 'error'));
ALTER TABLE heart_journal ADD COLUMN brain_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE heart_journal ADD COLUMN brain_prompt_tokens INTEGER;
ALTER TABLE heart_journal ADD COLUMN brain_completion_tokens INTEGER;
