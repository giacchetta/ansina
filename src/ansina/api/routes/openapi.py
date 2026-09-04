"""`GET /openapi.json` — the OpenAPI contract document, served as a gated route of our
own instead of FastAPI's default. See issue #25.

`create_app` passes `openapi_url=None, docs_url=None, redoc_url=None` to the `FastAPI`
constructor: those defaults are plain Starlette routes that can't carry a
`require(...)` dependency, so the route-coverage audit could never see them, and
`/docs`/`/redoc` are non-functional with auth enabled anyway (no security scheme is
declared for Swagger UI's "Authorize" button to use — auth here is middleware-level,
not a per-route `fastapi.security` dependency). The JSON contract itself is the useful
artifact; any external OpenAPI viewer can point at a fetched copy of it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ansina.api.authorization import require

router = APIRouter(include_in_schema=False)

_RESOURCE = "system.openapi"
_DESCRIPTION = "GET /openapi.json — the OpenAPI contract document."


@router.get(
    "/openapi.json",
    dependencies=[Depends(require(_RESOURCE, description=_DESCRIPTION))],
)
async def openapi_schema(request: Request) -> JSONResponse:
    return JSONResponse(request.app.openapi())
