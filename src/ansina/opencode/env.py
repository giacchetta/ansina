"""Builds the OpenCode child's own environment — an explicit allow-list, never
the daemon's own `os.environ` wholesale. See issue #74.

Mirrors `ansina.dev.vector.build_vector_env`'s own discipline, with one
deliberate difference: `HOME` is **not** inherited (`build_vector_env`'s own
`_INHERITED_ENV_NAMES` includes it) — it is instead redirected to
`ansina.opencode.workspace.isolated_home` whenever `[opencode] isolate_home`
is `True` (the default). This is a security finding, not a style preference:
OpenCode's own documented config precedence *merges* files together rather
than replacing them, with the global `~/.config/opencode/opencode.json`
merging in *under* project config — so an ambient operator-level config on
the host could otherwise silently loosen this module's own deny-by-default
block for any permission key our own generated `opencode.json` doesn't
explicitly name. Redirecting `HOME` removes that one file from the merge
chain entirely.

`OPENCODE_CONFIG_CONTENT` is set to the identical rendered permission block
the generated `opencode.json` file carries, as defense in depth: it sits
*above* project config in OpenCode's own documented merge order, so even a
project-config file an operator hand-edited after the fact can't loosen what
this module demands.

**Residual risk, named rather than solved here**: OpenCode's documented
precedence places managed config files and macOS managed preferences *above*
even `OPENCODE_CONFIG_CONTENT` — redirecting `HOME` does not remove either
from the merge chain, and neither lives at a path this module could read and
account for directly. `ansina.opencode.preflight`'s own final check (re-
reading the *effective*, fully-resolved permissions via `opencode debug
config`) is what actually catches that case; this module's job is only to
make the common case (an ordinary global config file) unreachable.
"""

from __future__ import annotations

import json
import os

from ansina.config.settings import Settings
from ansina.opencode.config import render_opencode_config
from ansina.opencode.workspace import isolated_home

ENV_HOME = "HOME"
ENV_OPENCODE_CONFIG_CONTENT = "OPENCODE_CONFIG_CONTENT"

# Inherited from the daemon's own process environment, unfiltered — everything
# else in `build_opencode_env` is an explicit allow-list, the same
# `ansina.dev.vector.build_vector_env`-style discipline. `HOME` is
# deliberately absent here (see module docstring); `PATH` for resolving the
# `opencode` binary itself, `TMPDIR` for its own scratch use.
_INHERITED_ENV_NAMES = ("PATH", "TMPDIR")


def build_opencode_env(
    settings: Settings, *, base_env: dict[str, str] | None = None
) -> dict[str, str]:
    """`base_env` defaults to the real process environment; the unit suite
    passes a synthetic one so a test can assert exactly what is and isn't
    forwarded with no dependency on this machine's own environment.
    """
    base = os.environ if base_env is None else base_env
    opencode = settings.opencode
    env: dict[str, str] = {
        name: base[name] for name in _INHERITED_ENV_NAMES if name in base
    }
    if opencode.isolate_home:
        env[ENV_HOME] = str(isolated_home(settings))
    env[ENV_OPENCODE_CONFIG_CONTENT] = json.dumps(render_opencode_config(settings))
    if opencode.provider_api_key_env and opencode.provider_api_key is not None:
        env[opencode.provider_api_key_env] = (
            opencode.provider_api_key.get_secret_value()
        )
    for name in opencode.inherit_env_names:
        if name in base:
            env[name] = base[name]
    return env
