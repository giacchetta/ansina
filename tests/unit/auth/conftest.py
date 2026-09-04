from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ansina.auth.hashing import Argon2Params
from ansina.storage.database import Database
from ansina.storage.migrator import run_migrations


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    """A `Database` migrated to the latest schema (including `0002_rbac.sql`),
    following `tests/unit/storage/test_migrator.py`'s own fixture shape.
    """
    database = Database(tmp_path / "ansina.db")
    database.connect()
    run_migrations(database)
    yield database
    database.close()


@pytest.fixture
def cheap_argon2() -> Argon2Params:
    """Minimal argon2id work factors so hashing doesn't dominate test runtime — never
    used for a real credential, only in this test suite.
    """
    return Argon2Params(time_cost=1, memory_cost_kib=8, parallelism=1)
