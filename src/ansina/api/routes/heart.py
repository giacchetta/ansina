"""Operator control surface for the autonomic tick loop. See issue #11.

Ansina's first non-health routes: deny-by-default already covers them (they're not in
`api/auth.py`'s `PUBLIC_PATHS`), so every one of these requires the bearer token once
one is configured. This is the kill switch the issue asks for — an operator can halt or
restart the tick loop without a process restart.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ansina.api.problems import CODE_HEART_DISABLED, problem_response
from ansina.heart.tick import TickController, TickDecision

router = APIRouter(prefix="/heart")

_DISABLED_DETAIL = (
    "the Heart is disabled ([heart] enabled = false) — there is no tick loop to report "
    "on or control"
)


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
)
async def resume_tick_loop(request: Request) -> PauseResult | JSONResponse:
    tick_loop = _get_tick_loop(request)
    if tick_loop is None:
        return _disabled_response()
    tick_loop.resume()
    return PauseResult(paused=False)
