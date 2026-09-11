"""Renders a failed `ApiResponse` the same way across every command: a human message
on stderr (the recognized message for the problem's `code` from `problems.py`, or a
per-call override), the request id when `--verbose`, and the daemon's own
`retry_after_seconds` figure when it sent one (issue #26's sudo lockout, 429) — then
returns the `ExitCode` the command should exit with.

Lives above both `client.py` and `output.py` rather than inside `problems.py` itself:
`problems.py` is imported *by* `client.py`, so a helper that also imports `output.py`
(which `client.py` never touches) would either duplicate rendering per command or
create a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping

from ansina_tui.client import ApiResponse
from ansina_tui.exits import ExitCode
from ansina_tui.output import Emitter
from ansina_tui.problems import ApiProblem, exit_code_for


def report(
    emitter: Emitter,
    response: ApiResponse,
    *,
    messages: Mapping[str, str] | None = None,
) -> ExitCode:
    """Render `response` (assumed non-2xx) and return the exit code to use.

    `messages` overrides `problems.py`'s recognized-code table for specific codes —
    `commands/auth/sudo.py` uses it because `ansina.unauthorized` means something
    different at `POST /auth/sudo` (a wrong step-up password) than everywhere else
    (no/invalid bearer token): "Run `auth login`" would be actively wrong there.
    """
    problem = response.problem
    code = problem.code if problem else None
    override = (messages or {}).get(code) if code else None
    message = override or (
        problem.message if problem else f"Request failed (HTTP {response.status_code})."
    )
    emitter.error(message)

    request_id = (problem.request_id if problem else None) or response.request_id
    if request_id:
        emitter.debug(f"request id: {request_id}")

    retry_after = _retry_after(problem)
    if retry_after is not None:
        emitter.error(f"Retry after {retry_after:.0f}s.")

    return exit_code_for(response.status_code)


def _retry_after(problem: ApiProblem | None) -> float | None:
    """`SudoLockedOutError`'s `retry_after_seconds` extension member
    (`ansina.api.exception_handlers.ansina_error_handler`) — the same figure the
    daemon also puts in a real `Retry-After` header, for a client that doesn't parse
    JSON; this one does, so it reads the body's copy."""
    if problem is None:
        return None
    value = problem.extra.get("retry_after_seconds")
    return value if isinstance(value, int | float) else None
