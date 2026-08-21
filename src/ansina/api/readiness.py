"""A tiny named-check registry backing `GET /readyz`.

Each milestone that adds a real dependency (issue #6's SQLite connection) registers its
own check here instead of editing the `/readyz` route — the route only ever asks "is
everything registered currently true," never what those things are. Issue #5's auth is a
static token, not a dependency with its own state, so it has no readiness check of its
own.
"""

from __future__ import annotations

from collections.abc import Callable


class Readiness:
    """Named boolean checks, evaluated fresh on every read."""

    def __init__(self) -> None:
        self._checks: dict[str, Callable[[], bool]] = {}

    def register(self, name: str, check: Callable[[], bool]) -> None:
        """Add (or replace) the named check. `check` is called, not cached, on every
        `snapshot()`/`is_ready` — so a check backed by mutable state (e.g. "has the
        lifespan finished startup") reflects the current value, not the value at
        registration time.
        """
        self._checks[name] = check

    def snapshot(self) -> dict[str, bool]:
        """Every registered check's current result, keyed by name."""
        return {name: check() for name, check in self._checks.items()}

    @property
    def is_ready(self) -> bool:
        """`True` only when every registered check currently passes (vacuously `True`
        with no checks registered).
        """
        return all(self.snapshot().values())
