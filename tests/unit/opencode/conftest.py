from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ansina.storage.database import Database
from ansina.storage.migrator import run_migrations


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    """A `Database` migrated to the latest schema (including
    `0012_opencode_runs.sql`) — mirrors `tests/unit/heart/conftest.py`'s own fixture
    of the same name, duplicated here rather than shared across directories since
    cross-file imports aren't reliable under this suite's `--import-mode=importlib`
    (`tests/` has no `__init__.py`).
    """
    database = Database(tmp_path / "ansina.db")
    database.connect()
    run_migrations(database)
    yield database
    database.close()
