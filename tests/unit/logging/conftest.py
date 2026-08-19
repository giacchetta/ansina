from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from ansina.logging.formatter import JsonFormatter
from ansina.logging.redaction import clear_secrets


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    """Never let a secret registered by one test leak into the next."""
    clear_secrets()
    yield
    clear_secrets()


@pytest.fixture
def captured_logs() -> Iterator[Callable[[], list[dict[str, Any]]]]:
    """A root-logger handler writing `JsonFormatter` output to an in-memory buffer.

    Returns a callable that parses every line written so far as JSON — the assertion
    surface tests use instead of talking to stderr directly.
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
