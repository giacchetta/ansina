"""`/opencode/runs` — submit, list, inspect, and cancel OpenCode-hosted runs. See
issue #77.

Follows `api/routes/heart.py` + `api/routes/heart_journal.py` exactly: one fresh
`require(...)` dependency per route via a private `_require_runs()` helper, the
`_DISABLED_DETAIL` + 503 `problem+json` shape when `app.state.opencode is None`,
checked only **after** authorization has already passed (a caller without a grant
gets 403 without ever learning whether OpenCode is even enabled) — the same
ordering `heart.py` uses for `heart.tick`.

`GET /opencode/runs`'s pagination is byte-for-byte `heart_journal.py`'s own shape
(`limit`/`since`, `has_more` via one extra row, a malformed `since` -> 400
`ansina.request.invalid`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio.to_thread
from fastapi import APIRouter, Query, Request
from fastapi.params import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ansina.api.authorization import require
from ansina.api.problems import (
    CODE_NOT_FOUND,
    CODE_OPENCODE_DISABLED,
    CODE_REQUEST_INVALID,
    problem_response,
)
from ansina.auth.clock import parse_iso
from ansina.errors import OpencodeRunFailedError
from ansina.opencode.events import RunRequest
from ansina.opencode.repository import OpencodeRun

if TYPE_CHECKING:
    from ansina.opencode.service import OpencodeRunService

router = APIRouter(prefix="/opencode")

_DISABLED_DETAIL = (
    "opencode is disabled ([opencode] enabled = false, or its preflight failed) — "
    "there is no run service to submit to or report on"
)

_RESOURCE = "opencode.runs"
_DESCRIPTION = "OpenCode-hosted executor runs: submit, list, inspect, cancel."

_MAX_LIMIT = 500
_DEFAULT_LIMIT = 50


def _require_runs(*, sensitive: bool) -> Depends:
    """A fresh `require(...)` dependency per route, mirroring `routes/heart.py`'s
    own `_require_tick()` — one call site per route, never a shared `Depends`
    instance. `sensitive` varies per call the same way `users.py`/`groups.py` split
    it per verb on one resource: the two `GET`s are `sensitive=False`, submission and
    cancellation are `sensitive=True` (launching, or killing, an autonomous coding
    agent demands a live sudo grant for every role but `Admin`).
    """
    return Depends(require(_RESOURCE, description=_DESCRIPTION, sensitive=sensitive))


class RunSubmission(BaseModel):
    """The request body for `POST /opencode/runs` — mirrors `RunRequest`'s own
    fields one-for-one; `workspace_dir` is never part of it (issue #74's own
    confinement — always `[opencode] workspace_dir`, never per-request).
    """

    prompt: str
    session_id: str | None = None
    continue_last: bool = False
    fork: bool = False
    model: str | None = None
    agent: str | None = None
    auto_approve: bool | None = None

    def to_run_request(self) -> RunRequest:
        return RunRequest(
            prompt=self.prompt,
            session_id=self.session_id,
            continue_last=self.continue_last,
            fork=self.fork,
            model=self.model,
            agent=self.agent,
            auto_approve=self.auto_approve,
        )


class OpencodeRunOut(BaseModel):
    id: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    status: str
    session_id: str | None
    prompt: str
    model: str | None
    agent: str | None
    exit_code: int | None
    error_detail: str
    event_count: int
    tool_call_count: int
    permission_denied_count: int
    parse_error_count: int
    prompt_tokens: int | None
    completion_tokens: int | None
    duration_seconds: float | None
    retry_of: str | None
    intervention_count: int

    @classmethod
    def from_model(cls, run: OpencodeRun) -> OpencodeRunOut:
        return cls(
            id=run.id,
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            status=run.status,
            session_id=run.session_id,
            prompt=run.prompt,
            model=run.model,
            agent=run.agent,
            exit_code=run.exit_code,
            error_detail=run.error_detail,
            event_count=run.event_count,
            tool_call_count=run.tool_call_count,
            permission_denied_count=run.permission_denied_count,
            parse_error_count=run.parse_error_count,
            prompt_tokens=run.prompt_tokens,
            completion_tokens=run.completion_tokens,
            duration_seconds=run.duration_seconds,
            retry_of=run.retry_of,
            intervention_count=run.intervention_count,
        )


class RunPage(BaseModel):
    """Newest-first — byte-for-byte `heart_journal.py`'s `JournalPage` shape."""

    runs: list[OpencodeRunOut]
    count: int
    limit: int
    has_more: bool


def _get_service(request: Request) -> OpencodeRunService | None:
    service: OpencodeRunService | None = request.app.state.opencode
    return service


def _disabled_response() -> JSONResponse:
    return problem_response(
        status=503,
        code=CODE_OPENCODE_DISABLED,
        title="OpenCode Disabled",
        detail=_DISABLED_DETAIL,
    )


def _not_found_response(run_id: str) -> JSONResponse:
    return problem_response(
        status=404,
        code=CODE_NOT_FOUND,
        title="Run Not Found",
        detail=f"no opencode run with id {run_id!r}",
    )


def _invalid_request_response(detail: str) -> JSONResponse:
    return problem_response(
        status=400,
        code=CODE_REQUEST_INVALID,
        title="Invalid Run Request",
        detail=detail,
    )


def _invalid_since_response(since: str) -> JSONResponse:
    return problem_response(
        status=400,
        code=CODE_REQUEST_INVALID,
        title="Invalid Query Parameter",
        detail=f"'since' is not a valid ISO 8601 timestamp: {since!r}",
    )


@router.post(
    "/runs",
    response_model=None,
    status_code=202,
    responses={
        202: {"model": OpencodeRunOut},
        400: {"description": "The prompt exceeds [opencode] max_prompt_chars."},
        409: {"description": "Intake is paused."},
        429: {"description": "The run backlog is full."},
        503: {"description": "OpenCode is disabled — no run service exists."},
    },
    dependencies=[_require_runs(sensitive=True)],
)
async def submit_run(
    request: Request, payload: RunSubmission
) -> OpencodeRunOut | JSONResponse:
    service = _get_service(request)
    if service is None:
        return _disabled_response()
    try:
        run_request = payload.to_run_request()
    except ValueError as exc:
        # `RunRequest.__post_init__`'s own session_id/continue_last/fork
        # mutual-exclusion checks — a malformed combination is a 400, the same
        # family as an oversized prompt below, never a bare 500.
        return _invalid_request_response(str(exc))
    try:
        run = await service.submit(run_request)
    except OpencodeRunFailedError as exc:
        return _invalid_request_response(str(exc))
    return OpencodeRunOut.from_model(run)


def _list_runs(service: OpencodeRunService, limit: int, since: str | None) -> RunPage:
    # `limit + 1`: an extra row beyond what's returned tells us whether there's more
    # to page through, without a second round-trip — `heart_journal.py`'s own trick.
    runs = service.repository.list_recent(limit=limit + 1, since=since)
    has_more = len(runs) > limit
    page = runs[:limit]
    return RunPage(
        runs=[OpencodeRunOut.from_model(run) for run in page],
        count=len(page),
        limit=limit,
        has_more=has_more,
    )


@router.get(
    "/runs",
    response_model=None,
    responses={
        200: {"model": RunPage},
        400: {"description": "'since' is not a valid ISO 8601 timestamp."},
        503: {"description": "OpenCode is disabled — no run service exists."},
    },
    dependencies=[_require_runs(sensitive=False)],
)
async def list_runs(
    request: Request,
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    since: str | None = Query(default=None),
) -> RunPage | JSONResponse:
    service = _get_service(request)
    if service is None:
        return _disabled_response()
    if since is not None:
        try:
            parse_iso(since)
        except ValueError:
            return _invalid_since_response(since)
    return await anyio.to_thread.run_sync(_list_runs, service, limit, since)


@router.get(
    "/runs/{run_id}",
    response_model=None,
    responses={
        200: {"model": OpencodeRunOut},
        404: {"description": "No run with this id."},
        503: {"description": "OpenCode is disabled — no run service exists."},
    },
    dependencies=[_require_runs(sensitive=False)],
)
async def get_run(request: Request, run_id: str) -> OpencodeRunOut | JSONResponse:
    service = _get_service(request)
    if service is None:
        return _disabled_response()
    run = await anyio.to_thread.run_sync(service.repository.get, run_id)
    if run is None:
        return _not_found_response(run_id)
    return OpencodeRunOut.from_model(run)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=None,
    responses={
        200: {"model": OpencodeRunOut},
        404: {"description": "No active run with this id."},
        503: {"description": "OpenCode is disabled — no run service exists."},
    },
    dependencies=[_require_runs(sensitive=True)],
)
async def cancel_run(request: Request, run_id: str) -> OpencodeRunOut | JSONResponse:
    service = _get_service(request)
    if service is None:
        return _disabled_response()
    cancelled = await service.cancel(run_id)
    run = await anyio.to_thread.run_sync(service.repository.get, run_id)
    if not cancelled and run is None:
        return _not_found_response(run_id)
    if not cancelled:
        # The run exists but was already terminal — `cancel()` found no in-flight
        # task to cancel, so nothing was changed. Still a 404: there is no *active*
        # run with this id to cancel, the AC's own wording.
        return _not_found_response(run_id)
    assert run is not None  # a just-cancelled run always still has its own row
    return OpencodeRunOut.from_model(run)
