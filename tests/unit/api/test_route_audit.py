"""Unit tests for `ansina.api.route_audit.audit_route_coverage`."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI

from ansina.api.authorization import require
from ansina.api.route_audit import RouteCoverageError, audit_route_coverage


def _bare_app() -> FastAPI:
    """A `FastAPI` app with no default `/openapi.json`/`/docs`/`/redoc` — mirrors
    `create_app`'s own construction, so these tests exercise the audit against the
    shape the real app actually has. `test_a_non_apiroute_route_fails_the_audit` is
    the one deliberate exception.
    """
    return FastAPI(openapi_url=None, docs_url=None, redoc_url=None)


def test_a_route_with_require_is_catalogued() -> None:
    app = _bare_app()
    router = APIRouter()

    @router.get(
        "/thing", dependencies=[Depends(require("thing.read", description="reads"))]
    )
    async def get_thing() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    specs = audit_route_coverage(app)

    assert [(s.name, s.description) for s in specs] == [("thing.read", "reads")]


def test_public_paths_never_need_a_declaration() -> None:
    app = _bare_app()
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)

    assert audit_route_coverage(app) == ()


def test_a_route_with_no_require_fails_the_audit() -> None:
    app = _bare_app()
    router = APIRouter()

    @router.get("/scratch")
    async def scratch() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    with pytest.raises(RouteCoverageError, match=r"/scratch"):
        audit_route_coverage(app)


def test_two_require_declarations_on_one_route_fails_the_audit() -> None:
    app = _bare_app()
    router = APIRouter()

    @router.get(
        "/double",
        dependencies=[
            Depends(require("thing.a")),
            Depends(require("thing.b")),
        ],
    )
    async def double() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    with pytest.raises(RouteCoverageError, match="multiple require"):
        audit_route_coverage(app)


def test_conflicting_descriptions_for_the_same_resource_fails_the_audit() -> None:
    app = _bare_app()
    router = APIRouter()

    @router.get(
        "/one", dependencies=[Depends(require("thing.shared", description="first"))]
    )
    async def one() -> dict[str, bool]:
        return {"ok": True}

    @router.get(
        "/two", dependencies=[Depends(require("thing.shared", description="second"))]
    )
    async def two() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    with pytest.raises(RouteCoverageError, match="conflicting"):
        audit_route_coverage(app)


def test_matching_descriptions_for_the_same_resource_across_routes_is_fine() -> None:
    app = _bare_app()
    router = APIRouter()

    @router.get(
        "/one", dependencies=[Depends(require("thing.shared", description="same"))]
    )
    async def one() -> dict[str, bool]:
        return {"ok": True}

    @router.post(
        "/two", dependencies=[Depends(require("thing.shared", description="same"))]
    )
    async def two() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    specs = audit_route_coverage(app)

    assert [(s.name, s.description) for s in specs] == [("thing.shared", "same")]


def test_an_empty_description_never_overwrites_an_existing_one() -> None:
    app = _bare_app()
    router = APIRouter()

    @router.get(
        "/one", dependencies=[Depends(require("thing.shared", description="real"))]
    )
    async def one() -> dict[str, bool]:
        return {"ok": True}

    @router.post("/two", dependencies=[Depends(require("thing.shared"))])
    async def two() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    specs = audit_route_coverage(app)

    assert [(s.name, s.description) for s in specs] == [("thing.shared", "real")]


def test_a_non_apiroute_route_fails_the_audit() -> None:
    """FastAPI's own default `/openapi.json`/`/docs`/`/redoc` are plain Starlette
    routes — structurally unable to carry a `require(...)` dependency. `create_app`
    disables them (`openapi_url=None` etc.) precisely so this branch is never hit in
    the real app; this test proves the audit would still catch one if it were.
    """
    app = FastAPI(openapi_url="/openapi.json")

    with pytest.raises(RouteCoverageError, match="not an APIRoute"):
        audit_route_coverage(app)


def test_specs_are_sorted_by_name() -> None:
    app = _bare_app()
    router = APIRouter()

    @router.get("/z", dependencies=[Depends(require("zzz.last"))])
    async def z() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/a", dependencies=[Depends(require("aaa.first"))])
    async def a() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    specs = audit_route_coverage(app)

    assert [s.name for s in specs] == ["aaa.first", "zzz.last"]
