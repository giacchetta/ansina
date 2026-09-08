"""Parse the daemon's RFC 9457 `application/problem+json` error bodies.

Mirrors the shape `ansina.api.problems.Problem` produces (`ansina/src/ansina/api/
problems.py`), but this module is deliberately standalone: `tui/` never imports
`ansina` (see `README.md` — the whole point of this project's isolation), so the
recognized-code table below is a hand-copied, commented mapping, not a shared import.
If the daemon's codes drift, this table needs a manual update — the alternative (a
shared package) would reintroduce the cross-project dependency issue #31 exists to
rule out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ansina_tui.exits import ExitCode

# code -> human message. Sourced from:
#   ansina.unauthorized          api/problems.py CODE_UNAUTHORIZED
#   ansina.forbidden              auth/authorization.py ForbiddenError.code
#   ansina.auth.sudo_required     auth/authorization.py SudoRequiredError.code
#   ansina.auth.sudo_locked_out   auth/sudo.py SudoLockedOutError.code
#   ansina.heart.disabled         api/problems.py CODE_HEART_DISABLED
#   ansina.not_ready              api/problems.py CODE_NOT_READY
_KNOWN_MESSAGES: dict[str, str] = {
    "ansina.unauthorized": "Not authenticated. Run `auth login` or set ANSINA_TOKEN.",
    "ansina.forbidden": "Your role doesn't grant this action.",
    "ansina.auth.sudo_required": "Sudo required. Run `ansina-tui auth sudo` first.",
    "ansina.auth.sudo_locked_out": "Sudo locked out after too many failed attempts.",
    "ansina.heart.disabled": "The Heart is disabled on this daemon.",
    "ansina.not_ready": "The daemon is not ready yet.",
}


@dataclass(frozen=True, slots=True)
class ApiProblem:
    """One `application/problem+json` body, tolerantly parsed."""

    type: str | None
    title: str | None
    status: int | None
    detail: str | None
    code: str | None
    request_id: str | None
    extra: dict[str, Any]

    @property
    def message(self) -> str:
        """A human-readable line: the known message for `code` if we have one, else
        `detail`, else `title`, else a last-resort fallback naming `code`."""
        if self.code and self.code in _KNOWN_MESSAGES:
            return _KNOWN_MESSAGES[self.code]
        if self.detail:
            return self.detail
        if self.title:
            return self.title
        return f"Request failed ({self.code or 'unknown error'})."


# Members every `Problem` (see `api/problems.py`) always carries.
_KNOWN_FIELDS = {"type", "title", "status", "detail", "code", "request_id"}


def parse_problem(body: object) -> ApiProblem | None:
    """Parse a response body as `ApiProblem`, or `None` if it isn't a problem+json
    object at all (e.g. an empty body, or a non-JSON/non-dict payload)."""
    if not isinstance(body, dict):
        return None
    extra = {k: v for k, v in body.items() if k not in _KNOWN_FIELDS}
    return ApiProblem(
        type=_str_or_none(body.get("type")),
        title=_str_or_none(body.get("title")),
        status=body.get("status") if isinstance(body.get("status"), int) else None,
        detail=_str_or_none(body.get("detail")),
        code=_str_or_none(body.get("code")),
        request_id=_str_or_none(body.get("request_id")),
        extra=extra,
    )


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def exit_code_for(status_code: int) -> ExitCode:
    """Map an HTTP status to the exit code a CLI command should use when it can't
    otherwise interpret the failure (mutation/`api` commands, not `status`, which has
    its own health/readiness-aware mapping)."""
    if status_code == 401:
        return ExitCode.NOT_AUTHENTICATED
    if status_code == 403:
        return ExitCode.FORBIDDEN
    return ExitCode.REQUEST_FAILED
