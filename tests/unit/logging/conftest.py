from __future__ import annotations

from collections.abc import Iterator

import pytest

from ansina.logging.redaction import clear_secrets


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    """Never let a secret registered by one test leak into the next."""
    clear_secrets()
    yield
    clear_secrets()
