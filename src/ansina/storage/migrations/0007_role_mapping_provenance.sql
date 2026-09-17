-- Migration 0007: role-mapping provenance and a real uniqueness constraint (issue #42).
--
-- `role_assignments` has had no way to record *who* created a row since 0002_rbac.sql —
-- every assignment looked identical whether an Admin made it by hand or a future
-- claims-based sync (#43) derived it from an IdP group membership. Without that, a
-- per-login role refresh would have to choose between never revoking an IdP-side
-- removal, or wiping a manually-made grant on every login. `source` closes that gap:
-- `'local'` for everything created through the ordinary role-assignment routes (the
-- default, so every historical row backfills to it with no behavior change), or a
-- provider identifier for a row `ansina.auth.role_sync.sync_mapped_roles` created.
-- There's no `CHECK` on the value — the provider set is open by construction, the same
-- way `external_identities.provider` has no `CHECK` either — the repository layer owns
-- the `'local'` convention, not the schema.
--
-- The existing `UNIQUE (subject_type, subject_id, role_id)` on `role_assignments` is
-- untouched: one subject+role pairing is still exactly one row with exactly one
-- `source`. That is what makes "a local grant survives a provider sync" structural —
-- `sync_mapped_roles` can only ever see/touch rows whose `source` already equals the
-- provider it was called for — rather than a convention a future caller could violate.
ALTER TABLE role_assignments ADD COLUMN source TEXT NOT NULL DEFAULT 'local';

-- `role_mappings` has shipped with no uniqueness constraint since 0002_rbac.sql — two
-- identical (provider, claim, value, role_id) rows were previously indistinguishable
-- and both would silently apply. `POST /auth/role-mappings` relies on this index to
-- turn a duplicate submission into a 409 (`ansina.auth.duplicate`) rather than a
-- second, redundant row.
CREATE UNIQUE INDEX idx_role_mappings_tuple
    ON role_mappings (provider, claim, value, role_id);
