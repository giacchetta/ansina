"""Unit tests for `ansina.api.route_audit.audit_route_coverage`."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI

from ansina.api.authorization import require
from ansina.api.route_audit import RouteCoverageError, audit_route_coverage
from ansina.auth.models import Verb


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
    # Issue #38: a GET-only route's resource is catalogued with only GET, not every
    # `Verb` — the fidelity `GET /auth/permissions` builds its response from.
    assert specs[0].verbs == {Verb.GET}


def test_verbs_union_across_every_route_declaring_the_same_resource() -> None:
    app = _bare_app()
    router = APIRouter()

    @router.get("/thing", dependencies=[Depends(require("thing.crud"))])
    async def get_thing() -> dict[str, bool]:
        return {"ok": True}

    @router.post("/thing", dependencies=[Depends(require("thing.crud"))])
    async def post_thing() -> dict[str, bool]:
        return {"ok": True}

    @router.delete("/thing/{id}", dependencies=[Depends(require("thing.crud"))])
    async def delete_thing(id: str) -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    specs = audit_route_coverage(app)

    assert [s.verbs for s in specs] == [{Verb.GET, Verb.POST, Verb.DELETE}]


def test_a_non_verb_method_is_filtered_out_of_the_served_verb_set() -> None:
    """A route can declare a method outside `Verb` (e.g. `OPTIONS`, via `api_route`'s
    own `methods=`) — it must never enter the served-verb set, since `require()`
    itself 403s an unmapped verb and it's not something a role could ever be granted.
    """
    app = _bare_app()
    router = APIRouter()

    @router.api_route(
        "/thing",
        methods=["GET", "OPTIONS"],
        dependencies=[Depends(require("thing.read"))],
    )
    async def get_thing() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    specs = audit_route_coverage(app)

    assert specs[0].verbs == {Verb.GET}


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
