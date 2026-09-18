"""The startup-time route-coverage audit. See issue #25.

Walks every route `create_app()` will actually serve and refuses to boot — same "fail
loudly before uvicorn binds a port" pattern as `HeartUnavailableError` — if any
non-public route lacks exactly one `ansina.api.authorization.require(...)`
declaration. The same walk doubles as the `resources` catalog's source, replacing
issue #24's hand-written `BOOTSTRAP_RESOURCES` seed: a route's own declaration is the
only way a resource enters the catalog, so the catalog can never drift from what's
actually enforced.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from fastapi.routing import APIRoute, iter_route_contexts

from ansina.api.auth import PUBLIC_PATHS
from ansina.api.authorization import DECLARATION_ATTR, ResourceDeclaration
from ansina.auth.models import Verb
from ansina.auth.policy import ResourceSpec
from ansina.errors import AuthError

_VERB_VALUES: frozenset[str] = frozenset(verb.value for verb in Verb)

if TYPE_CHECKING:
    from fastapi import FastAPI


class RouteCoverageError(AuthError):
    """A non-public route has no `require(...)` declaration, more than one, isn't an
    `APIRoute` at all (and so structurally can't carry one), or two routes declare the
    same resource name with conflicting descriptions.
    """

    code: ClassVar[str] = "ansina.auth.route_coverage_invalid"


def _declarations_for(route: APIRoute) -> list[ResourceDeclaration]:
    """Every `require(...)`-tagged dependency directly on `route` — route-level
    (`dependencies=[Depends(require(...))]`) and handler-parameter-level both land in
    `route.dependant.dependencies`, so one scan covers both declaration shapes.
    """
    declarations = []
    for dependency in route.dependant.dependencies:
        declaration = getattr(dependency.call, DECLARATION_ATTR, None)
        if isinstance(declaration, ResourceDeclaration):
            declarations.append(declaration)
    return declarations


def audit_route_coverage(app: FastAPI) -> tuple[ResourceSpec, ...]:
    """Verify every non-public route in `app` declares exactly one `require(...)`,
    raising `RouteCoverageError` (listing every offender) if not. Returns the
    deduplicated `ResourceSpec`s the surviving declarations describe, sorted by name —
    `create_app`'s lifespan hands these to `auth.reconciler.sync_resources` in place of
    `auth.policy.BOOTSTRAP_RESOURCES`.
    """
    problems: list[str] = []
    descriptions_by_name: dict[str, str] = {}
    verbs_by_name: dict[str, set[Verb]] = {}

    for context in iter_route_contexts(app.routes):
        path = context.path
        if path is None or path in PUBLIC_PATHS:
            continue

        route = context.route
        if not isinstance(route, APIRoute):
            problems.append(f"{path}: not an APIRoute — cannot declare require(...)")
            continue

        # `APIRoute.methods` is always populated for a real route — `set[str] | None`
        # only accommodates `BaseRoute` subclasses (e.g. `Mount`) that don't apply here.
        methods = sorted(route.methods or ())
        declarations = _declarations_for(route)
        if not declarations:
            problems.append(f"{methods} {path}: missing require(...)")
            continue
        if len(declarations) > 1:
            names = sorted({d.name for d in declarations})
            problems.append(
                f"{methods} {path}: multiple require(...) declarations {names}"
            )
            continue

        declaration = declarations[0]
        # Issue #38: every surviving route contributes its own methods to its
        # resource's served-verb set, unioned across every route declaring that
        # resource (e.g. `auth.users` is served by seven routes and must end up with
        # all five verbs) — a method outside `Verb` (e.g. `OPTIONS`, explicitly
        # declared via `api_route(methods=[...])`) is filtered out, since `require()`
        # itself 403s an unmapped verb and it's never something a role could be
        # granted.
        verbs_by_name.setdefault(declaration.name, set()).update(
            Verb(method) for method in route.methods or () if method in _VERB_VALUES
        )

        existing_description = descriptions_by_name.get(declaration.name)
        if (
            existing_description is not None
            and existing_description
            and declaration.description
            and existing_description != declaration.description
        ):
            problems.append(
                f"resource {declaration.name!r} declared with conflicting "
                f"descriptions: {existing_description!r} vs "
                f"{declaration.description!r}"
            )
            continue
        if existing_description is None or (
            not existing_description and declaration.description
        ):
            descriptions_by_name[declaration.name] = declaration.description

    if problems:
        raise RouteCoverageError(
            "route coverage audit failed:\n  " + "\n  ".join(problems)
        )

    specs = (
        ResourceSpec(name, description, frozenset(verbs_by_name[name]))
        for name, description in descriptions_by_name.items()
    )
    return tuple(sorted(specs, key=lambda spec: spec.name))
