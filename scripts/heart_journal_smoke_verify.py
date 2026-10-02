#!/usr/bin/env python3
"""Cross-checks a fetched `GET /heart/journal` page against the daemon's own
`"heart tick completed"` log lines from the same run — the comparison
`scripts/heart-journal-smoke.sh` needs for issue #55's Mac Mini acceptance check.

Not part of the `ansina` package (no pytest/mypy/coverage obligation, same as every
other `scripts/*.sh` operational tool) — plain stdlib, invoked locally by the bash
driver after `journal.json`/`run.log` are scp'd back from the Mac Mini. Log-line
parsing itself lives in `scripts/heart_log.py` (issue #56 pulled it out once a second
script, `heart_soak_report.py`, needed the identical shape).

Usage: python scripts/heart_journal_smoke_verify.py <run.log> <journal.json> [min_ticks]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from heart_log import load_tick_log_lines

_FLOAT_TOLERANCE = 1e-6


def main() -> int:
    if len(sys.argv) not in (3, 4):
        print(__doc__, file=sys.stderr)
        return 2
    run_log = Path(sys.argv[1])
    journal_path = Path(sys.argv[2])
    min_ticks = int(sys.argv[3]) if len(sys.argv) == 4 else 1

    log_entries = [record.get("extra", {}) for record in load_tick_log_lines(run_log)]
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal_entries = journal["entries"]

    print(f"Log lines  ('heart tick completed'): {len(log_entries)}")
    print(f"Journal rows (GET /heart/journal):    {len(journal_entries)}")

    failures: list[str] = []

    if len(log_entries) < min_ticks:
        failures.append(
            f"only {len(log_entries)} 'heart tick completed' log line(s), "
            f"expected at least {min_ticks}"
        )
    if len(journal_entries) < min_ticks:
        failures.append(
            f"only {len(journal_entries)} journal row(s), expected at least {min_ticks}"
        )

    log_by_tick = {entry["tick"]: entry for entry in log_entries if "tick" in entry}
    for row in journal_entries:
        tick = row["tick_number"]
        row_failures: list[str] = []
        log_entry = log_by_tick.get(tick)
        if log_entry is None:
            row_failures.append(f"tick {tick}: journal row has no matching log line")
        else:
            if row["decision"] != log_entry.get("decision"):
                row_failures.append(
                    f"tick {tick}: journal decision {row['decision']!r} != "
                    f"log decision {log_entry.get('decision')!r}"
                )
            if row["prompt_tokens"] != log_entry.get("prompt_tokens"):
                row_failures.append(
                    f"tick {tick}: journal prompt_tokens {row['prompt_tokens']!r} != "
                    f"log prompt_tokens {log_entry.get('prompt_tokens')!r}"
                )
            log_duration = log_entry.get("duration_seconds")
            if (
                not isinstance(log_duration, int | float)
                or abs(row["duration_seconds"] - log_duration) > _FLOAT_TOLERANCE
            ):
                row_failures.append(
                    f"tick {tick}: journal duration_seconds "
                    f"{row['duration_seconds']!r} != log duration_seconds "
                    f"{log_duration!r}"
                )

        status = "MISMATCH" if row_failures else "matches log"
        print(
            f"  tick {tick}: decision={row['decision']!r} "
            f"duration_seconds={row['duration_seconds']!r} "
            f"prompt_tokens={row['prompt_tokens']!r}  ({status})"
        )
        failures.extend(row_failures)

    sys.stdout.flush()
    if failures:
        print("\nFAIL:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("\nPASS: every journal row matches its daemon log line.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
