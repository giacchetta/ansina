"""Forward-only SQLite migration runner backed by a `schema_version` table.

See issue #6. Every `.sql` file under `ansina/storage/migrations/` is one migration,
numbered `NNNN_name.sql`; `run_migrations` applies whichever ones a given database
hasn't seen yet, in order, each inside its own transaction. Migrations already applied
are never re-run; applying out of order or skipping a version is a hard error rather
than something this runner tries to reconcile.

Named `migrator.py`, not `migrations.py` — a same-named module and the `migrations/`
package directory can't coexist (both would resolve to `ansina.storage.migrations`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import ClassVar

from ansina.errors import StorageError
from ansina.logging import get_logger
from ansina.storage.database import Database

logger = get_logger(__name__)

_MIGRATION_PATTERN = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_MIGRATIONS_PACKAGE = "ansina.storage.migrations"


class MigrationError(StorageError):
    """A migration is malformed, out of order, or the database is newer than the
    code applying it.
    """

    code: ClassVar[str] = "ansina.storage.migration_error"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def _discover_migrations(root: Traversable | None = None) -> list[Migration]:
    """Every migration under `root`, sorted by version.

    `root` defaults to the real bundled `migrations/` package; tests pass a
    `pathlib.Path` to a temp directory of fake `.sql` files instead — `Path` satisfies
    the same `iterdir()`/`is_file()`/`read_text()` surface `Traversable` requires.

    Raises `MigrationError` if the set isn't a contiguous `1..N` run — a gap or a
    duplicate version is a packaging bug, not something to silently tolerate.
    """
    if root is None:
        root = resources.files(_MIGRATIONS_PACKAGE)
    found: dict[int, Migration] = {}
    for entry in root.iterdir():
        if not entry.is_file():
            continue
        match = _MIGRATION_PATTERN.match(entry.name)
        if match is None:
            continue
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version}: "
                f"{found[version].name!r} and {entry.name!r}"
            )
        found[version] = Migration(
            version=version,
            name=match.group(2),
            sql=entry.read_text(encoding="utf-8"),
        )

    migrations = [found[version] for version in sorted(found)]
    expected = list(range(1, len(migrations) + 1))
    actual = [m.version for m in migrations]
    if actual != expected:
        raise MigrationError(
            f"migration versions must be contiguous starting at 1, got {actual}"
        )
    return migrations


def _applied_versions(db: Database) -> list[int]:
    """Versions already recorded in `schema_version`, or `[]` for a brand-new
    database — `schema_version` itself doesn't exist until migration 0001 creates it.
    """
    conn = db.connection()
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if exists is None:
        return []
    rows = conn.execute(
        "SELECT version FROM schema_version ORDER BY version"
    ).fetchall()
    return [row[0] for row in rows]


def run_migrations(db: Database, *, root: Traversable | None = None) -> None:
    """Bring `db` up to the latest bundled schema version.

    Idempotent: a database already at the latest version is untouched. Refuses to run
    at all if the applied versions aren't a contiguous `1..K` prefix, or if the
    database is *ahead* of the code (`K` beyond the highest bundled migration) — both
    are treated as corruption, never silently accepted.

    `root` overrides where migrations are discovered from (see `_discover_migrations`);
    only tests pass it, to drive this function against a fake set of `.sql` files.
    """
    migrations = _discover_migrations(root)
    applied = _applied_versions(db)

    expected_prefix = list(range(1, len(applied) + 1))
    if applied != expected_prefix:
        raise MigrationError(
            f"applied migrations are not a contiguous sequence from 1: {applied}"
        )

    current = applied[-1] if applied else 0
    latest = migrations[-1].version if migrations else 0
    if current > latest:
        raise MigrationError(
            f"database is at migration {current}, but only {latest} are bundled "
            "with this build — refusing to run against a newer schema"
        )

    pending = [m for m in migrations if m.version > current]
    conn = db.connection()
    for migration in pending:
        logger.info(
            "applying migration",
            # `LogRecord` already has a reserved `name` attribute (the *logger's*
            # name) — passing our own `"name"` through `extra` collides with it and
            # raises `KeyError` inside `logging.Logger.makeRecord`, so this uses
            # `migration_name` instead.
            extra={"version": migration.version, "migration_name": migration.name},
        )
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executescript(migration.sql)
            conn.execute(
                "INSERT INTO schema_version (version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
