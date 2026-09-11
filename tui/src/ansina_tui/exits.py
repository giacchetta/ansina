"""The one place `ansina-tui`'s exit-code table lives.

Every exit anywhere in this codebase goes through `ExitCode` — never a bare
`raise typer.Exit(2)` — so this table (and `README.md`'s copy of it) stays the single
source of truth, and `tests/unit/test_exits.py` can assert every member is exercised.

The first six codes are the ones named in issue #31. `NOT_READY`/`NOT_HEALTHY` extend
that table: a daemon that answered but reported a bad state is neither "ok" (0) nor a
failed request (1) — distinguishing them is what makes `ansina-tui status`'s exit code
usable as a container health/readiness probe. `/healthz` is always checked before
`/readyz` (see `commands/status.py`), so an unhealthy daemon exits `NOT_HEALTHY`, never
`NOT_READY`.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    REQUEST_FAILED = 1
    USAGE = 2
    NOT_AUTHENTICATED = 3
    FORBIDDEN = 4
    HOST_UNREACHABLE = 5
    NOT_READY = 6
    NOT_HEALTHY = 7
