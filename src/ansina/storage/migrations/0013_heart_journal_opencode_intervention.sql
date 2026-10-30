-- Migration 0013: OpenCode-intervention outcome columns on heart_journal (issue #79).
--
-- The Heart's bounded interventions on the OpenCode executor (retry/nudge/compact/
-- pause_intake/escalate_to_brain) need a durable record of what was attempted and
-- what happened -- additive columns on the same table, mirroring `0011`'s own
-- brain_* widening exactly (same reasoning: `GET /heart/journal` stays the single
-- source of truth for "what did a tick do," no second table to join).
--
-- Every column is nullable with no default beyond NULL (`opencode_detail` aside):
-- every row written before this migration, and every row where no OpenCode
-- intervention was attempted, has no intervention at all -- NULL means "not
-- applicable," not "unknown."
--
-- Plain `ALTER TABLE ... ADD COLUMN`, including two with a `CHECK` -- the same
-- directly-supported operation `0011_heart_journal_brain_outcome.sql` already used
-- (SQLite has no `ALTER CONSTRAINT`, but adding a brand new column with its own new
-- `CHECK` needs no 12-step rebuild the way widening an *existing* `CHECK` would).
--
-- No foreign key on `opencode_run_id` -- `heart_journal` has never had one, the
-- same "no owner beyond the daemon itself" reasoning every table since
-- `login_attempts`/`sudo_lockouts` already documents; it is also legitimately NULL
-- for `pause_intake`, which has no single triggering run.
ALTER TABLE heart_journal ADD COLUMN opencode_run_id TEXT;
ALTER TABLE heart_journal ADD COLUMN opencode_action TEXT
    CHECK (opencode_action IS NULL OR opencode_action IN
        ('retry', 'nudge', 'compact', 'pause_intake', 'escalate_to_brain'));
ALTER TABLE heart_journal ADD COLUMN opencode_outcome TEXT
    CHECK (opencode_outcome IS NULL OR opencode_outcome IN
        ('applied', 'declined', 'unsupported', 'error'));
ALTER TABLE heart_journal ADD COLUMN opencode_detail TEXT NOT NULL DEFAULT '';
