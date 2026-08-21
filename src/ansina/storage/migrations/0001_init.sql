-- Migration 0001: schema_version bookkeeping.
--
-- The only table M0 ships (issue #6) — no domain tables until the feature that needs
-- one arrives. `run_migrations` inserts this migration's own row in the same
-- transaction that creates the table, so the two can never drift apart.
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
