"""Per-request correlation id, propagated via `contextvars`.

The dependency-injection analogue from a Jest/Node background: this is what
`AsyncLocalStorage` gives you for free — a value implicitly threaded through everything
called during a request without passing it as an explicit parameter everywhere.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("_request_id", default=None)


def get_request_id() -> str | None:
    """The current request/correlation id, or `None` outside a `request_id_scope`."""
    return _request_id.get()


@contextmanager
def request_id_scope(request_id: str | None = None) -> Iterator[str]:
    """Bind a request id for the lifetime of the `with` block.

    Generates a fresh id (`uuid4().hex`) when none is given — e.g. issue #4's ASGI
    middleware passes an inbound header's value here if present, or lets one be minted.
    Nesting is supported: the previous value (possibly `None`) is restored on exit, the
    same set/reset-token discipline as `_config_file_override` in `config/settings.py`.
    """
    resolved = request_id if request_id is not None else uuid.uuid4().hex
    token = _request_id.set(resolved)
    try:
        yield resolved
    finally:
        _request_id.reset(token)
