#!/usr/bin/env python3
"""A fake `opencode` binary — an executable Python stub standing in for the real
OpenCode CLI in `tests/e2e/test_server.py`'s own fake-executor test (issue #77).

Neither CI leg installs the real `opencode` binary (see `docs/opencode.md`), so
this is what lets the e2e suite exercise a real subprocess spawn, a real
`--version`/`debug config` preflight pass, and a real line-delimited JSON event
stream — with **zero production-code special-casing**: `ansina.opencode.preflight`/
`ansina.opencode.runner.OpencodeCliRunner` never know they're talking to this
script rather than the real binary. Its directory is prepended to `PATH` by the
test that uses it, the same way a real `opencode` install would be resolved via
`shutil.which`.

Answers exactly the three invocation shapes `ansina.opencode` actually makes:

- `--version` -> a fixed version string on stdout, matching the prefix the test
  configures `[opencode] required_version_prefix` to.
- `debug config` -> a minimal JSON object with an empty `permission` block, so
  `ansina.opencode.preflight._resolve_effective_permission`'s own best-effort scan
  finds nothing looser than configured (an empty block can't be looser than
  anything) and the preflight's final check passes cleanly.
- `run <prompt> --format json --dir <workspace> ...` -> a short, canned sequence
  of line-delimited JSON events on stdout, then exits 0 — real output a real
  `OpencodeCliRunner.run()` parses through `ansina.opencode.events.
  parse_run_event` exactly as it would the real binary's.

Not `pytest`-collected (no `test_*`/`*_test` name) — but, unlike `scripts/*.py`
(outside `src`/`tests`, so outside mypy's own `files` setting), it *is* under
`tests/`, so `mypy --strict` does check it; kept clean rather than excluded.
"""

from __future__ import annotations

import json
import sys

_VERSION = "opencode v2.0.0-fake"

_CANNED_EVENTS = (
    {"type": "message", "role": "assistant", "text": "Hello from the fake."},
    {"type": "tool_call", "tool": "bash", "command": "echo hi"},
)


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(_VERSION)
        return 0

    if len(argv) >= 2 and argv[0] == "debug" and argv[1] == "config":
        print(json.dumps({"permission": {}}))
        return 0

    if argv and argv[0] == "run":
        for event in _CANNED_EVENTS:
            print(json.dumps(event), flush=True)
        return 0

    print(f"fake_opencode.py: unrecognized argv {argv!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
