"""Liveness, readiness, and version probes — the only routes M0 ships (issue #4).

No business logic lives here or ever will: this router is infrastructure, not a domain
endpoint. `GET /healthz` intentionally does not consult `Readiness` — a process that can
answer HTTP at all is alive, even if a downstream dependency (SQLite, later the Brain)
isn't ready yet; conflating the two would make an orchestrator restart a pod that just
needs to wait.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ansina import __version__
from ansina.api.problems import CODE_NOT_READY, problem_response
from ansina.api.readiness import Readiness

router = APIRouter()


class HealthStatus(BaseModel):
    status: str


class ReadyStatus(BaseModel):
    status: str
    checks: dict[str, bool]


class VersionInfo(BaseModel):
    name: str
    version: str


@router.get("/healthz", response_model=HealthStatus)
async def healthz() -> HealthStatus:
    """Liveness only — 200 unconditionally, no dependency on readiness."""
    return HealthStatus(status="ok")


@router.get(
    "/readyz",
    response_model=None,
    responses={
        200: {"model": ReadyStatus},
        503: {"description": "One or more readiness checks are failing."},
    },
)
async def readyz(request: Request) -> ReadyStatus | JSONResponse:
    # `Readiness` is per-app state (built in `create_app`), not a global — pulled from
    # `request.app.state` rather than fighting FastAPI's `Depends` typing for a value
    # that isn't itself a dependency-injectable singleton.
    readiness: Readiness = request.app.state.readiness
    checks = readiness.snapshot()
    if all(checks.values()):
        return ReadyStatus(status="ready", checks=checks)
    return problem_response(
        status=503,
        code=CODE_NOT_READY,
        title="Not Ready",
        detail="One or more readiness checks are failing.",
        extra={"checks": checks},
    )


@router.get("/version", response_model=VersionInfo)
async def version() -> VersionInfo:
    return VersionInfo(name="ansina", version=__version__)
