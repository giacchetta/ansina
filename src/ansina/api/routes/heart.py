"""Operator control surface for the autonomic tick loop. See issue #11.

Ansina's first non-health routes: deny-by-default already covers them (they're not in
`api/auth.py`'s `PUBLIC_PATHS`), so every one of these requires the bearer token once
one is configured. This is the kill switch the issue asks for — an operator can halt or
restart the tick loop without a process restart.

All three share one `heart.tick` resource (issue #25's `require(...)`): a `Read`-role
caller gets `GET /heart/tick` but 403s on the two `POST` routes; `Write`/`Maintain`/
`Admin` get all three.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.params import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ansina.api.authorization import require
from ansina.api.problems import CODE_HEART_DISABLED, problem_response
from ansina.heart.tick import TickController, TickDecision

router = APIRouter(prefix="/heart")

_DISABLED_DETAIL = (
    "the Heart is disabled ([heart] enabled = false) — there is no tick loop to report "
    "on or control"
)

_RESOURCE = "heart.tick"
_DESCRIPTION = "The autonomic tick loop's status and pause/resume."


def _require_tick() -> Depends:
    """A fresh `require(...)` dependency per route — one call site per route, like
    every other router in this codebase, rather than one `Depends` instance shared
    across the three routes below. Constructs `fastapi.params.Depends` directly rather
    than through the `fastapi.Depends` factory used everywhere else in the codebase —
    that factory is typed to return `Any` (for annotation convenience at a route
    decorator's `Depends(...)` call site), which defeats this function's own explicit
    return type under `mypy --strict`.
    """
    return Depends(require(_RESOURCE, description=_DESCRIPTION))


class TickStatus(BaseModel):
    running: bool
    paused: bool
    ticks: int
    last_decision: TickDecision | None = None
    last_tick_at: str | None = None
    last_duration_seconds: float | None = None


class PauseResult(BaseModel):
    paused: bool


def _get_tick_loop(request: Request) -> TickController | None:
    tick_loop: TickController | None = request.app.state.tick_loop
    return tick_loop


def _disabled_response() -> JSONResponse:
    return problem_response(
        status=503,
        code=CODE_HEART_DISABLED,
        title="Heart Disabled",
        detail=_DISABLED_DETAIL,
    )


@router.get(
    "/tick",
    response_model=None,
    responses={
        200: {"model": TickStatus},
        503: {"description": "The Heart is disabled — no tick loop exists."},
    },
    dependencies=[_require_tick()],
)
async def get_tick_status(request: Request) -> TickStatus | JSONResponse:
    tick_loop = _get_tick_loop(request)
    if tick_loop is None:
        return _disabled_response()
    return TickStatus(
        running=tick_loop.is_healthy(),
        paused=tick_loop.paused,
        ticks=tick_loop.ticks_run,
        last_decision=tick_loop.last_decision,
        last_tick_at=tick_loop.last_tick_at,
        last_duration_seconds=tick_loop.last_duration_seconds,
    )


@router.post(
    "/tick/pause",
    response_model=None,
    responses={
        200: {"model": PauseResult},
        503: {"description": "The Heart is disabled — no tick loop exists."},
    },
    dependencies=[_require_tick()],
)
async def pause_tick_loop(request: Request) -> PauseResult | JSONResponse:
    tick_loop = _get_tick_loop(request)
    if tick_loop is None:
        return _disabled_response()
    tick_loop.pause()
    return PauseResult(paused=True)


@router.post(
    "/tick/resume",
    response_model=None,
    responses={
        200: {"model": PauseResult},
        503: {"description": "The Heart is disabled — no tick loop exists."},
    },
    dependencies=[_require_tick()],
)
async def resume_tick_loop(request: Request) -> PauseResult | JSONResponse:
    tick_loop = _get_tick_loop(request)
    if tick_loop is None:
        return _disabled_response()
    tick_loop.resume()
    return PauseResult(paused=False)
