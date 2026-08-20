"""SQLite persistence foundation. See issue #6.

`Database` owns connection lifecycle and PRAGMAs (WAL, foreign keys); `run_migrations`
brings a `Database` up to the latest bundled schema version. Both are driven from the
FastAPI lifespan in `api/app.py` — nothing outside that boot path should construct or
migrate a `Database` on its own.
"""

from ansina.storage.database import Database
from ansina.storage.migrator import Migration, MigrationError, run_migrations

__all__ = [
    "Database",
    "Migration",
    "MigrationError",
    "run_migrations",
]
