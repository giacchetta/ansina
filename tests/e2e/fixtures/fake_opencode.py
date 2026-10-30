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
- `run <prompt> [flags...]` -> a short, canned sequence of line-delimited JSON
  events on stdout, then exits 0 — real output a real `OpencodeCliRunner.run()`
  parses through `ansina.opencode.events.parse_run_event` exactly as it would the
  real binary's.

The `run` branch validates its own flags against an allow-list of what the real
v2 CLI actually accepts (`--format`/`--session`/`--model`/`--agent`/`--continue`/
`-c`/`--fork`/`--auto`) and rejects anything else the same way the real binary
does — `ERROR: Unrecognized flag: <flag> in command opencode run`, exit 1. This
is issue #81's own CI-level guard: `OpencodeCliRunner._build_argv()` passed a
`--dir` flag that does not exist on the real binary for two issues straight
(#75, #79's `RETRY`/`NUDGE`) before a human caught it with a real install, in
part because this fake used to accept any flag at all. Rejecting unknown flags
here means the next argv regression fails on both CI legs, not only on real
hardware.

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

# What the real `opencode run` subcommand actually accepts, per issue #81's own
# flag audit against a real v2.1.4 binary's `--help` output — `--dir` is
# deliberately absent; it does not exist on the real CLI.
_RUN_FLAGS_WITH_VALUE = ("--format", "--session", "--model", "-m", "--agent")
_RUN_FLAGS_BARE = ("--continue", "-c", "--fork", "--auto")


def _run_argv_is_valid(run_args: list[str]) -> str | None:
    """Walks the arguments after `run`, mirroring the real CLI's own parser:
    the first non-flag element is the prompt, a bare flag consumes itself, a
    value flag consumes the next element too. Returns the first unrecognized
    flag, or `None` if every element was accounted for.
    """
    i = 0
    seen_prompt = False
    while i < len(run_args):
        arg = run_args[i]
        if arg in _RUN_FLAGS_WITH_VALUE:
            i += 2
        elif arg in _RUN_FLAGS_BARE:
            i += 1
        elif arg.startswith("-"):
            return arg
        elif not seen_prompt:
            seen_prompt = True
            i += 1
        else:
            return arg
    return None


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(_VERSION)
        return 0

    if len(argv) >= 2 and argv[0] == "debug" and argv[1] == "config":
        print(json.dumps({"permission": {}}))
        return 0

    if argv and argv[0] == "run":
        bad_flag = _run_argv_is_valid(argv[1:])
        if bad_flag is not None:
            print(
                f"ERROR\n  Unrecognized flag: {bad_flag} in command opencode run",
                file=sys.stderr,
            )
            return 1
        for event in _CANNED_EVENTS:
            print(json.dumps(event), flush=True)
        return 0

    print(f"fake_opencode.py: unrecognized argv {argv!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
