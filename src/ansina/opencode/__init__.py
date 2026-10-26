"""OpenCode as Ansina's confined, subprocess-driven executor ("Hands") — the
foundation this issue (#74) ships, with no executor yet: confined workspace
provisioning, generated per-run config, an allow-list child environment, and
a logged-skip preflight. Nothing in this package spawns a real `opencode`
child that does work — issue #75's `OpencodeCliRunner` is the first thing
that does; `preflight()` here only ever invokes the binary to answer a
yes/no readiness question (`--version`, `debug config`), never to run a
prompt.

This is also where the "the daemon itself never shells out" invariant
(`heart.eval.provenance`'s own module docstring, already narrowed to
`ansina.dev`-at-boot — see `ansina.process.handles`'s own module docstring)
is narrowed a second, differently-shaped way, named here rather than left
implicit: issue #75 widens it to *runtime*, *repeated*, request- and
tick-triggered subprocess use, confined to this package only. The two
narrowings don't conflict — `ansina.dev` stays a one-time, boot-only
exception; `ansina.opencode` is a second, independent exception with its own
(wider) shape.
"""

from ansina.opencode.config import render_opencode_config, write_opencode_config
from ansina.opencode.env import build_opencode_env
from ansina.opencode.preflight import build_opencode_service
from ansina.opencode.workspace import ensure_workspace, isolated_home

# Deliberately does *not* re-export `preflight`/`OpencodePreflightResult` from
# `ansina.opencode.preflight` — a name collision waiting to happen, since that
# submodule's own public function is also named `preflight`. Binding it here
# (`from ansina.opencode.preflight import preflight`) would rebind this
# package's own `preflight` *attribute* from the submodule to the function,
# breaking any later `import ansina.opencode.preflight` anywhere else in the
# codebase (confirmed empirically — Python's dotted-import machinery resolves
# `import a.b.c` through attribute access on `a.b`, not a direct `sys.modules`
# lookup, so the shadowing is real, not merely cosmetic). `ansina.dev`'s own
# `__init__.py` already avoids this for the identical reason: it re-exports
# `build_vector_sidecar` from `vector.py`, never that module's own `preflight`/
# `PreflightResult`. A caller that needs either imports them from
# `ansina.opencode.preflight` directly, the same way
# `tests/unit/dev/test_vector.py` imports `ansina.dev.vector`'s own `preflight`.
__all__ = [
    "build_opencode_env",
    "build_opencode_service",
    "ensure_workspace",
    "isolated_home",
    "render_opencode_config",
    "write_opencode_config",
]
