"""OpenCode as Ansina's confined, subprocess-driven executor ("Hands").
Issue #74 shipped the foundation with no executor yet: confined workspace
provisioning, generated per-run config, an allow-list child environment, and
a logged-skip preflight. Issue #75's `ansina.opencode.runner.
OpencodeCliRunner` is the first thing that spawns a real `opencode` child to
do work — `build_opencode_service` below returns one, already constructed
against the preflight-resolved binary, on a clean preflight pass;
`preflight()` itself still only ever invokes the binary to answer a yes/no
readiness question (`--version`, `debug config`), never to run a prompt.

This is also where the "the daemon itself never shells out" invariant
(`heart.eval.provenance`'s own module docstring, already narrowed to
`ansina.dev`-at-boot — see `ansina.process.handles`'s own module docstring)
is narrowed a second, differently-shaped way, named here rather than left
implicit: issue #75 widens it to *runtime*, *repeated*, request- and
tick-triggered subprocess use (`OpencodeCliRunner.run()`), confined to this
package only. The two narrowings don't conflict — `ansina.dev` stays a
one-time, boot-only exception; `ansina.opencode` is a second, independent
exception with its own (wider) shape.
"""

from ansina.opencode.config import render_opencode_config, write_opencode_config
from ansina.opencode.env import build_opencode_env
from ansina.opencode.events import (
    RunEvent,
    RunEventType,
    RunOutcome,
    RunRequest,
    RunStatus,
)
from ansina.opencode.preflight import build_opencode_service
from ansina.opencode.repository import OpencodeRun, OpencodeRunRepository
from ansina.opencode.runner import AgentRunner, OpencodeCliRunner
from ansina.opencode.service import (
    OpencodeRunService,
    OpencodeRunServiceFactory,
    build_opencode_run_service,
)
from ansina.opencode.telemetry import (
    OpencodeTelemetryWriter,
    build_opencode_telemetry_writer,
)
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
# `runner`/`events`/`telemetry` carry no such risk — none of those submodules'
# own names collide with anything this file re-exports from them, and `telemetry
# .py` (issue #76) has no `ansina.heart` dependency chain to worry about either
# (unlike `ansina.telemetry.sampler`, which is why *that* package's own
# `__init__` leaves `sampler.py` out — see its module docstring). `repository.py`/
# `service.py` (issue #77) carry the identical no-cycle, no-name-collision shape —
# `ansina.opencode.service` imports `ansina.heart.tick.loop`'s own `TickController`
# at no point (unlike `ansina.telemetry.sampler`), so re-exporting it eagerly here
# is safe.
__all__ = [
    "AgentRunner",
    "OpencodeCliRunner",
    "OpencodeRun",
    "OpencodeRunRepository",
    "OpencodeRunService",
    "OpencodeRunServiceFactory",
    "OpencodeTelemetryWriter",
    "RunEvent",
    "RunEventType",
    "RunOutcome",
    "RunRequest",
    "RunStatus",
    "build_opencode_env",
    "build_opencode_run_service",
    "build_opencode_service",
    "build_opencode_telemetry_writer",
    "ensure_workspace",
    "isolated_home",
    "render_opencode_config",
    "write_opencode_config",
]
