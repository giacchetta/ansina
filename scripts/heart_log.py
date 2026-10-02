"""Shared helpers for parsing the daemon's structured JSON log lines out of a tee'd
run.log — used by `heart_journal_smoke_verify.py` (issue #55) and
`heart_soak_report.py` (issue #56). Not part of the `ansina` package (no pytest/mypy/
coverage obligation, same as every other `scripts/*` operational tool) — plain stdlib,
imported by both callers via the ordinary "sys.path[0] is the invoking script's own
directory" rule (`python scripts/<caller>.py` puts `scripts/` on `sys.path`, so `import
heart_log` resolves here with no package machinery needed).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json_log_lines(path: Path) -> list[dict[str, Any]]:
    """Every line in `path` that parses as a JSON object, in file order. Most lines in
    a `tee`'d run.log are plain text (shell echoes, `make`/`uv` output) rather than
    JSON — those are silently skipped, not an error.
    """
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def load_tick_log_lines(path: Path) -> list[dict[str, Any]]:
    """Every `"heart tick completed"` JSON log line in `path`, parsed whole — the full
    record (`timestamp`/`level`/`message`/`extra`/...), not just its `extra` payload:
    issue #56's soak report also needs each line's own `timestamp` for its scheduling-
    drift check, which `extra` alone doesn't carry.
    """
    return [
        record
        for record in load_json_log_lines(path)
        if record.get("message") == "heart tick completed"
    ]
