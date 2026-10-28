"""`/opencode/status`, `/opencode/intake/pause`, `/opencode/intake/resume` — the
intake kill switch. See issue #77.

A sibling module to `api/routes/opencode_runs.py`, not an addition to it, mirroring
`api/routes/heart_journal.py`'s own "a different resource deserves its own module"
precedent: these three routes share one resource (`opencode.intake`), distinct from
`opencode.runs` — `GET /opencode/status` is non-sensitive (reporting state), the two
pause/resume mutations are `sensitive=True`, the same split `opencode_runs.py` makes
on its own resource.

`POST /opencode/intake/pause`/`resume` call the exact same
`OpencodeRunService.pause_intake()`/`.resume_intake()` methods a later issue's
bounded Heart intervention will call — this route is the operator-facing half of
that one kill switch, not a separate mechanism.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.params import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ansina.api.authorization import require
from ansina.api.problems import CODE_OPENCODE_DISABLED, problem_response

if TYPE_CHECKING:
    from ansina.opencode.service import OpencodeRunService

router = APIRouter(prefix="/opencode")

_DISABLED_DETAIL = (
    "opencode is disabled ([opencode] enabled = false, or its preflight failed) — "
    "there is no run service to report on or pause/resume"
)

_RESOURCE = "opencode.intake"
_DESCRIPTION = "Whether OpenCode run submission is currently accepted."


def _require_intake(*, sensitive: bool) -> Depends:
    """A fresh `require(...)` dependency per route — mirrors
    `opencode_runs.py`'s own `_require_runs()`."""
    return Depends(require(_RESOURCE, description=_DESCRIPTION, sensitive=sensitive))


class IntakeStatus(BaseModel):
    enabled: bool
    intake_paused: bool
    active_runs: int


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


@router.get(
    "/status",
    response_model=None,
    responses={
        200: {"model": IntakeStatus},
        503: {"description": "OpenCode is disabled — no run service exists."},
    },
    dependencies=[_require_intake(sensitive=False)],
)
async def get_intake_status(request: Request) -> IntakeStatus | JSONResponse:
    service = _get_service(request)
    if service is None:
        return _disabled_response()
    active_runs = await anyio.to_thread.run_sync(service.repository.count_active)
    return IntakeStatus(
        enabled=True, intake_paused=service.intake_paused, active_runs=active_runs
    )


@router.post(
    "/intake/pause",
    response_model=None,
    responses={
        200: {"model": IntakeStatus},
        503: {"description": "OpenCode is disabled — no run service exists."},
    },
    dependencies=[_require_intake(sensitive=True)],
)
async def pause_intake(request: Request) -> IntakeStatus | JSONResponse:
    service = _get_service(request)
    if service is None:
        return _disabled_response()
    service.pause_intake()
    active_runs = await anyio.to_thread.run_sync(service.repository.count_active)
    return IntakeStatus(enabled=True, intake_paused=True, active_runs=active_runs)


@router.post(
    "/intake/resume",
    response_model=None,
    responses={
        200: {"model": IntakeStatus},
        503: {"description": "OpenCode is disabled — no run service exists."},
    },
    dependencies=[_require_intake(sensitive=True)],
)
async def resume_intake(request: Request) -> IntakeStatus | JSONResponse:
    service = _get_service(request)
    if service is None:
        return _disabled_response()
    service.resume_intake()
    active_runs = await anyio.to_thread.run_sync(service.repository.count_active)
    return IntakeStatus(enabled=True, intake_paused=False, active_runs=active_runs)
