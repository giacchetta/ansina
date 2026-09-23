"""Cross-Origin Resource Sharing for browser-hosted third-party clients. See issue #51.

Without this, a browser on another origin cannot call Ansina's REST API at all — the
failure is reported by the browser, never by Ansina, so it is invisible in the daemon's
own logs. `[security.cors] enabled = false` (the default,
`config.settings.CorsSettings`) means `add_cors_middleware` adds nothing at all; the
daemon + `ansina-tui` deployment (which is not a browser and never sends an `Origin`
header) is byte-for-byte unchanged.

Wraps `starlette.middleware.cors.CORSMiddleware` — ships with FastAPI, no new
dependency (`.agents/guardrails/forbidden-actions.md`'s "no unapproved dependencies").
`create_app` registers it *last*, so it runs *outermost*, ahead of both
`RequestIdMiddleware` and `BearerAuthMiddleware` — see `api/app.py`'s own middleware
ordering comment for why a preflight must never reach either of those.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.cors import CORSMiddleware

from ansina.auth.models import Verb

if TYPE_CHECKING:
    from fastapi import FastAPI

    from ansina.config import Settings

# Every verb `role_permissions` can grant, plus OPTIONS (the preflight method itself,
# never a grantable verb — `auth.policy.permitted_verbs`/`Resource.verbs` both exclude
# it, see issue #38's own reasoning). Derived from `Verb` so this can never drift from
# what a route can actually be gated on.
ALLOW_METHODS: tuple[str, ...] = (*(verb.value for verb in Verb), "OPTIONS")

# Starlette unions this with its own `SAFELISTED_HEADERS` (Accept, Accept-Language,
# Content-Language, Content-Type) automatically — only Ansina-specific headers need
# listing here. `Authorization`/`X-Sudo-Token` are how a caller authenticates/elevates;
# `Content-Type` is listed explicitly since a JSON request body needs it past the
# safelist's own plain-form-encoded default.
ALLOW_HEADERS: tuple[str, ...] = ("Authorization", "X-Sudo-Token", "Content-Type")

# Response headers a browser hides from JS unless explicitly exposed here.
# `X-Request-ID` is what lets a browser client log-correlate the way `tui/`'s
# `ApiClient` already does; `Retry-After` is what makes issue #49's login throttle and
# issue #26's sudo lockout readable from JS at all — both would otherwise be silently
# unreadable despite being present on the wire.
EXPOSE_HEADERS: tuple[str, ...] = ("X-Request-ID", "Retry-After")


def add_cors_middleware(app: FastAPI, settings: Settings) -> bool:
    """Adds `CORSMiddleware` to `app` when `[security.cors] enabled = true`, and
    nothing at all otherwise. Returns whether it was added, so `create_app` can assert
    against the resulting stack shape without re-reading `settings` itself.

    `allow_credentials=False` always — Ansina authenticates with an `Authorization`
    header, never a cookie, so a credentialed CORS mode would add CSRF surface for no
    benefit (`config.settings.CorsSettings`'s own docstring).
    """
    cors = settings.security.cors
    if not cors.enabled:
        return False
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors.allowed_origins),
        allow_methods=list(ALLOW_METHODS),
        allow_headers=list(ALLOW_HEADERS),
        allow_credentials=False,
        expose_headers=list(EXPOSE_HEADERS),
        max_age=cors.max_age_seconds,
    )
    return True
