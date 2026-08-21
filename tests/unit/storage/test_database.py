from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from ansina.errors import StorageError
from ansina.storage.database import Database


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "ansina.db")
    database.connect()
    yield database
    database.close()  # idempotent — safe even if a test already closed it


def test_connect_creates_the_database_file(tmp_path: Path) -> None:
    database = Database(tmp_path / "ansina.db")

    database.connect()
    try:
        assert (tmp_path / "ansina.db").exists()
    finally:
        database.close()


def test_connect_creates_nested_parent_directories(tmp_path: Path) -> None:
    database = Database(tmp_path / "nested" / "deeper" / "ansina.db")

    database.connect()
    try:
        assert (tmp_path / "nested" / "deeper" / "ansina.db").exists()
    finally:
        database.close()


def test_journal_mode_is_wal(db: Database) -> None:
    (mode,) = db.connection().execute("PRAGMA journal_mode").fetchone()

    assert str(mode).lower() == "wal"


def test_foreign_keys_pragma_is_on(db: Database) -> None:
    (value,) = db.connection().execute("PRAGMA foreign_keys").fetchone()

    assert value == 1


def test_foreign_keys_are_actually_enforced(db: Database) -> None:
    conn = db.connection()
    conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER "
        "REFERENCES parent(id))"
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")


def test_each_thread_gets_its_own_connection(db: Database) -> None:
    main_conn = db.connection()
    other: dict[str, sqlite3.Connection] = {}

    def worker() -> None:
        other["conn"] = db.connection()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert other["conn"] is not main_conn


def test_thread_local_connections_each_have_pragmas_applied(db: Database) -> None:
    results: dict[str, tuple[object, object]] = {}

    def worker() -> None:
        conn = db.connection()
        (journal,) = conn.execute("PRAGMA journal_mode").fetchone()
        (fk,) = conn.execute("PRAGMA foreign_keys").fetchone()
        results["worker"] = (journal, fk)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert str(results["worker"][0]).lower() == "wal"
    assert results["worker"][1] == 1


def test_same_thread_reuses_the_same_connection(db: Database) -> None:
    assert db.connection() is db.connection()


def test_is_healthy_true_when_connected(db: Database) -> None:
    assert db.is_healthy() is True


def test_is_healthy_false_when_query_raises(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`is_healthy()` is a `Readiness` check (api/readiness.py) — it must report
    `False` on a `sqlite3.Error`, never let it propagate as a caller-visible exception.
    `sqlite3.Connection` is a C extension type whose methods can't be monkeypatched
    directly, so this stubs the connection `Database` hands back instead.
    """

    class _FailingConnection:
        def execute(self, *_: Any) -> sqlite3.Cursor:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "connection", lambda: _FailingConnection())

    assert db.is_healthy() is False


def test_transaction_commits_on_success(db: Database) -> None:
    db.connection().execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    with db.transaction() as cursor:
        cursor.execute("INSERT INTO t (id) VALUES (1)")

    rows = db.connection().execute("SELECT id FROM t").fetchall()
    assert [row[0] for row in rows] == [1]


def test_transaction_rolls_back_on_error(db: Database) -> None:
    db.connection().execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    with pytest.raises(ValueError, match="boom"), db.transaction() as cursor:
        cursor.execute("INSERT INTO t (id) VALUES (1)")
        raise ValueError("boom")

    rows = db.connection().execute("SELECT id FROM t").fetchall()
    assert rows == []


def test_close_closes_connections_opened_on_other_threads(db: Database) -> None:
    other: dict[str, sqlite3.Connection] = {}

    def worker() -> None:
        other["conn"] = db.connection()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    db.close()

    with pytest.raises(sqlite3.ProgrammingError):
        other["conn"].execute("SELECT 1")


def test_connection_after_close_raises(db: Database) -> None:
    db.close()

    with pytest.raises(StorageError, match="after close"):
        db.connection()


def test_connect_raises_storage_error_when_wal_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`journal_mode=WAL` can silently no-op (e.g. on certain network filesystems),
    returning some other mode instead of raising — `Database` must catch that rather
    than boot against a database it only believes is in WAL mode.
    """
    database = Database(tmp_path / "ansina.db")

    class _RollbackJournalConnection(sqlite3.Connection):
        """Pretends `PRAGMA journal_mode=WAL` was rejected by SQLite."""

        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql == "PRAGMA journal_mode=WAL":
                return super().execute("SELECT 'delete'")
            return super().execute(sql, parameters)

    real_connect = sqlite3.connect

    def _fake_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = _RollbackJournalConnection
        return cast(sqlite3.Connection, real_connect(*args, **kwargs))

    monkeypatch.setattr("ansina.storage.database.sqlite3.connect", _fake_connect)

    with pytest.raises(StorageError, match="WAL"):
        database.connect()
