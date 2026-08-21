"""SQLite connection lifecycle: WAL mode, foreign keys, thread-local pooling.

See issue #6. `Database` hands out one connection per thread (a real pool, not a lock
around a single connection) because sub-agents landing in a later milestone will read
from this same database concurrently — WAL lets those reads run in parallel with
whatever the writer is doing. Writes still serialize; that's a SQLite property, not a
choice this module makes.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ansina.errors import StorageError
from ansina.logging import get_logger

logger = get_logger(__name__)

# Applied on every connection, every thread. Without it, a second concurrent writer
# hits `SQLITE_BUSY` immediately instead of waiting for the first to finish — WAL only
# keeps *readers* from blocking on the writer, it does not queue writers.
_BUSY_TIMEOUT_MS = 5000


class Database:
    """Thread-local SQLite connection pool bound to one on-disk file.

    Each thread that calls `connection()` gets its own `sqlite3.Connection`, opened
    lazily on first use and cached for that thread's lifetime, with WAL, foreign keys,
    and `busy_timeout` applied fresh (those first two are per-connection state in
    SQLite; only `journal_mode` itself is a property of the file). `close()` closes
    every connection this instance has ever handed out, regardless of which thread
    opened it — safe to call from the lifespan-shutdown thread even though `connect()`
    and every subsequent `connection()` call may have run elsewhere.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._local = threading.local()
        self._all_connections: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()
        self._closed = False

    def connect(self) -> None:
        """Open the owning connection, creating the parent directory if needed, and
        verify WAL actually took effect. Call once, from the FastAPI lifespan startup.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("opening database", extra={"path": str(self._path)})
        self.connection()  # opens + verifies WAL on this (the lifespan) thread

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False, autocommit=True)
        conn.row_factory = sqlite3.Row
        (mode,) = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        if str(mode).lower() != "wal":
            conn.close()
            raise StorageError(
                f"failed to enable WAL journal mode on {self._path} (got {mode!r})"
            )
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def connection(self) -> sqlite3.Connection:
        """This thread's connection, opened and configured on first use."""
        if self._closed:
            raise StorageError("cannot use Database after close()")
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
            with self._all_lock:
                self._all_connections.append(conn)
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """A single write transaction on this thread's connection.

        `BEGIN IMMEDIATE` acquires the write lock up front, so a conflicting writer
        gets a clear, immediate `SQLITE_BUSY` (retried per `busy_timeout`) instead of
        the deferred-transaction failure mode where the conflict surfaces only at
        `COMMIT`, after the caller thinks its work already succeeded.
        """
        conn = self.connection()
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        try:
            yield cursor
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        finally:
            cursor.close()

    def is_healthy(self) -> bool:
        """`True` if this thread's connection can round-trip a trivial query.

        Feeds `Readiness` (`api/readiness.py`) — never raises; a readiness check
        reporting `False` is the intended signal, not a caller-visible exception.
        """
        try:
            self.connection().execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    def close(self) -> None:
        """Close every connection this instance has ever opened, on any thread.

        Further `connection()` calls raise `StorageError` rather than silently
        reopening — a closed `Database` stays closed.
        """
        with self._all_lock:
            for conn in self._all_connections:
                conn.close()
            self._all_connections.clear()
        self._closed = True
