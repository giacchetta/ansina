"""Shared fixtures — the dependency-injected equivalent of Jest fixture files.

Requested by name in a test's signature and scoped per-test, rather than imported.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every ANSINA_* env var so a test never sees the dev machine's state."""
    for key in list(os.environ):
        if key.startswith("ANSINA_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def tmp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Run the test from an empty temp dir so a real ./ansina.toml can't leak in."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path
