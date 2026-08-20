"""Shared fixtures — the dependency-injected equivalent of Jest fixture files.

Requested by name in a test's signature and scoped per-test, rather than imported.
"""

from __future__ import annotations

import io
import json
import logging
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from ansina.logging.formatter import JsonFormatter


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


@pytest.fixture
def captured_logs() -> Iterator[Callable[[], list[dict[str, Any]]]]:
    """A root-logger handler writing `JsonFormatter` output to an in-memory buffer.

    Returns a callable that parses every line written so far as JSON — the assertion
    surface tests use instead of talking to stderr directly. Lives at the top level (not
    just under `tests/unit/logging/`) so `tests/unit/api/` can assert the request id
    lands in emitted log lines too.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)

    def _read() -> list[dict[str, Any]]:
        lines = stream.getvalue().splitlines()
        return [json.loads(line) for line in lines]

    try:
        yield _read
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
