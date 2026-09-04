from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ansina.storage.database import Database
from ansina.storage.migrator import MigrationError, _discover_migrations, run_migrations

# What the real 0001_init.sql creates, reused by tests that fake a migration set.
_SCHEMA_VERSION_SQL = (
    "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL);"
)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "ansina.db")
    database.connect()
    yield database
    database.close()


def _write(migrations_dir: Path, filename: str, sql: str) -> None:
    (migrations_dir / filename).write_text(sql, encoding="utf-8")


def test_fresh_database_reaches_version_2(db: Database) -> None:
    run_migrations(db)

    rows = (
        db.connection().execute("SELECT version, name FROM schema_version").fetchall()
    )
    assert [tuple(row) for row in rows] == [
        (1, "init"),
        (2, "rbac"),
        (3, "sudo"),
        (4, "user_tombstone"),
    ]


def test_second_run_is_idempotent(db: Database) -> None:
    run_migrations(db)
    run_migrations(db)

    rows = db.connection().execute("SELECT version FROM schema_version").fetchall()
    assert [row[0] for row in rows] == [1, 2, 3, 4]


def test_applies_only_pending_migrations(db: Database, tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir, "0001_init.sql", _SCHEMA_VERSION_SQL)
    _write(migrations_dir, "0002_second.sql", "CREATE TABLE t (id INTEGER);")

    run_migrations(db, root=migrations_dir)
    rows = db.connection().execute("SELECT version FROM schema_version").fetchall()
    assert [row[0] for row in rows] == [1, 2]

    # A second call with the same migration set applies nothing further.
    run_migrations(db, root=migrations_dir)
    rows = db.connection().execute("SELECT version FROM schema_version").fetchall()
    assert [row[0] for row in rows] == [1, 2]


def test_a_failing_migration_is_rolled_back_entirely(
    db: Database, tmp_path: Path
) -> None:
    """A migration that fails partway (bad SQL after valid DDL) leaves no trace: its
    own transaction rolls back, so neither the DDL nor the `schema_version` row lands.
    """
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(
        migrations_dir,
        "0001_bad.sql",
        f"{_SCHEMA_VERSION_SQL}\nTHIS IS NOT VALID SQL;",
    )

    with pytest.raises(Exception, match="syntax error"):
        run_migrations(db, root=migrations_dir)

    table = (
        db.connection()
        .execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        .fetchone()
    )
    assert table is None


def test_a_later_failing_migration_leaves_earlier_ones_committed(
    db: Database, tmp_path: Path
) -> None:
    """Migrations apply one transaction each, not one transaction for the whole
    batch — an already-committed earlier migration survives a later failure.
    """
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir, "0001_init.sql", _SCHEMA_VERSION_SQL)
    _write(migrations_dir, "0002_bad.sql", "THIS IS NOT VALID SQL;")

    with pytest.raises(Exception, match="syntax error"):
        run_migrations(db, root=migrations_dir)

    rows = db.connection().execute("SELECT version FROM schema_version").fetchall()
    assert [row[0] for row in rows] == [1]


def test_discover_rejects_a_gap_in_versions(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir, "0001_init.sql", "CREATE TABLE t (id INTEGER);")
    _write(migrations_dir, "0003_skip.sql", "CREATE TABLE u (id INTEGER);")

    with pytest.raises(MigrationError, match="contiguous"):
        _discover_migrations(migrations_dir)


def test_discover_rejects_a_duplicate_version(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir, "0001_init.sql", "CREATE TABLE t (id INTEGER);")
    _write(migrations_dir, "0001_also_init.sql", "CREATE TABLE u (id INTEGER);")

    with pytest.raises(MigrationError, match="duplicate"):
        _discover_migrations(migrations_dir)


def test_ignores_files_that_do_not_match_the_naming_pattern(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir, "0001_init.sql", "CREATE TABLE t (id INTEGER);")
    _write(migrations_dir, "README.md", "not a migration")

    migrations = _discover_migrations(migrations_dir)

    assert [m.version for m in migrations] == [1]


def test_ignores_subdirectories(tmp_path: Path) -> None:
    """A stray subdirectory under `migrations/` (e.g. `__pycache__`) is skipped by the
    `is_file()` check rather than blowing up on `read_text()`.
    """
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir, "0001_init.sql", "CREATE TABLE t (id INTEGER);")
    (migrations_dir / "0002_not_a_file").mkdir()

    migrations = _discover_migrations(migrations_dir)

    assert [m.version for m in migrations] == [1]


def test_rejects_applied_versions_with_a_gap(db: Database, tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir, "0001_init.sql", _SCHEMA_VERSION_SQL)
    _write(migrations_dir, "0002_second.sql", "CREATE TABLE t (id INTEGER);")
    _write(migrations_dir, "0003_third.sql", "CREATE TABLE u (id INTEGER);")

    # Simulate a database that somehow has 1 and 3 applied but not 2.
    conn = db.connection()
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO schema_version (version, name) VALUES (1, 'init')")
    conn.execute("INSERT INTO schema_version (version, name) VALUES (3, 'third')")

    with pytest.raises(MigrationError, match="not a contiguous sequence"):
        run_migrations(db, root=migrations_dir)


def test_rejects_a_database_ahead_of_the_bundled_migrations(
    db: Database, tmp_path: Path
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir, "0001_init.sql", _SCHEMA_VERSION_SQL)

    conn = db.connection()
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO schema_version (version, name) VALUES (1, 'init')")
    conn.execute("INSERT INTO schema_version (version, name) VALUES (2, 'future')")

    with pytest.raises(MigrationError, match="newer schema"):
        run_migrations(db, root=migrations_dir)
