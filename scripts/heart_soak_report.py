#!/usr/bin/env python3
"""Renders a multi-hour Heart soak into a committed-shape markdown report — issue
#56's Mac Mini acceptance evidence: no unbounded RSS growth, no tick-duration or
scheduling drift, every circuit-breaker event called out, and a journal/log
cross-check re-run at soak scale.

Not part of the `ansina` package (no pytest/mypy/coverage obligation, same as every
other `scripts/*` operational tool) — plain stdlib, invoked by `scripts/heart-soak.sh`
(`make remote-heart-soak-fetch`) after `run.log`/`samples.jsonl`/`journal.json` are
scp'd back from the Mac Mini. Shares `scripts/heart_log.py`'s JSON-log-line parser
with `heart_journal_smoke_verify.py` rather than duplicating it.

Output lands in a *gitignored* directory (`docs/heart/soak/`), auto-suffixed (`-2`,
`-3`, ...) on a same-day collision so a re-render never clobbers a prior run — the same
"never overwrite a report" discipline `scripts/remote-heart.sh` established for
`docs/heart/bench/`. Reports are not committed pending issue #59's S3-compatible
upload; `docs/heart/soak.md` (committed) documents why.

Usage: python scripts/heart_soak_report.py <run.log> <samples.jsonl> <journal.json> \
    <dest_dir>
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from heart_log import load_json_log_lines

_BRANCH_RE = re.compile(r"^branch: (?P<branch>.+)$", re.MULTILINE)
_COMMIT_RE = re.compile(
    r"^soaking (?P<sha>[0-9a-f]+) \((?P<subject>.*)\)$", re.MULTILINE
)
_CONFIG_RE = re.compile(r"^soak config: (?P<rest>.+)$", re.MULTILINE)

# A tick gap beyond this multiple of the expected interval+jitter/2 is called out by
# name in the drift section — chosen as "clearly more than jitter could explain", not
# a formal statistical test.
_NOTABLE_GAP_MULTIPLE = 1.5
_MAX_NOTABLE_GAPS_SHOWN = 20


def _parse_run_metadata(run_log_text: str) -> dict[str, str]:
    """Pulls the plain-text lines `scripts/heart-soak-run.sh` echoes ahead of the
    JSON-logging daemon boot (branch, commit, and a single `soak config: k=v k=v ...`
    line) — never JSON itself, the same "most lines are plain text" situation every
    other `run.log` consumer in this codebase already handles.
    """
    metadata: dict[str, str] = {}
    if (match := _BRANCH_RE.search(run_log_text)) is not None:
        metadata["branch"] = match.group("branch")
    if (match := _COMMIT_RE.search(run_log_text)) is not None:
        metadata["commit"] = match.group("sha")
        metadata["commit_subject"] = match.group("subject")
    if (match := _CONFIG_RE.search(run_log_text)) is not None:
        for pair in match.group("rest").split():
            if "=" in pair:
                key, _, value = pair.partition("=")
                metadata[key] = value
    return metadata


def _load_samples(samples_path: Path) -> list[dict[str, Any]]:
    if not samples_path.exists():
        return []
    samples: list[dict[str, Any]] = []
    for line in samples_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            samples.append(record)
    return samples


def _load_journal_entries(journal_path: Path) -> list[dict[str, Any]]:
    """Oldest-first (the API itself returns newest-first) — a readable report reads
    top to bottom in the order ticks actually happened.
    """
    if not journal_path.exists():
        return []
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = payload.get("entries", [])
    return list(reversed(entries))


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _slope_per_x(xs: list[float], ys: list[float]) -> float:
    """Least-squares slope of `ys` against `xs` — plain stdlib, no numpy dependency
    for one number.
    """
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return 0.0
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return numerator / denominator


def _fmt_gib(kib: float) -> str:
    return f"{kib / (1024 * 1024):.3f} GiB"


def _run_date(
    samples: list[dict[str, Any]], tick_log_lines: list[dict[str, Any]]
) -> str:
    for sample in samples:
        t = sample.get("t")
        if isinstance(t, int | float):
            return datetime.fromtimestamp(t, tz=UTC).date().isoformat()
    for record in tick_log_lines:
        ts = _parse_ts(record.get("timestamp"))
        if ts is not None:
            return ts.date().isoformat()
    return datetime.now(tz=UTC).date().isoformat()


def _unique_stem(dest_dir: Path, stem: str) -> str:
    """Never overwrite a prior render — same `-2`/`-3`/... auto-suffix discipline
    `scripts/remote-heart.sh` uses for `docs/heart/bench/`.
    """
    md_exists = (dest_dir / f"{stem}.md").exists()
    json_exists = (dest_dir / f"{stem}.json").exists()
    if not md_exists and not json_exists:
        return stem
    n = 2
    while (dest_dir / f"{stem}-{n}.md").exists() or (
        dest_dir / f"{stem}-{n}.json"
    ).exists():
        n += 1
    return f"{stem}-{n}"


def _header(
    metadata: dict[str, str], samples: list[dict[str, Any]], run_date: str
) -> str:
    observed_hours = (samples[-1].get("elapsed_s", 0) / 3600) if samples else 0.0
    commit = metadata.get("commit", "?")
    commit_subject = metadata.get("commit_subject", "?")
    return "\n".join(
        [
            f"# Heart soak: {run_date}",
            "",
            f"- Commit: {commit} ({commit_subject})",
            f"- Branch: {metadata.get('branch', '?')}",
            f"- Model: {metadata.get('model_repo', '?')}",
            f"- Prompt variant: {metadata.get('prompt_variant', '?')}",
            f"- interval_seconds={metadata.get('interval_seconds', '?')} "
            f"jitter_seconds={metadata.get('jitter_seconds', '?')}",
            f"- Requested duration: {metadata.get('hours', '?')}h, "
            f"sample interval: {metadata.get('sample_interval_seconds', '?')}s",
            f"- Observed duration: {observed_hours:.2f}h ({len(samples)} samples)",
            "",
        ]
    )


def _rss_section(samples: list[dict[str, Any]]) -> str:
    valid = [
        s for s in samples if isinstance(s.get("rss_kib"), int) and s["rss_kib"] > 0
    ]
    if not valid:
        return "## RSS (resident set size)\n\nNo RSS samples were recorded.\n"

    rss_values = [float(s["rss_kib"]) for s in valid]
    elapsed_hours = [s["elapsed_s"] / 3600 for s in valid]
    slope = _slope_per_x(elapsed_hours, rss_values)

    quarter = max(1, len(rss_values) // 4)
    first_quarter_mean = sum(rss_values[:quarter]) / quarter
    last_quarter_mean = sum(rss_values[-quarter:]) / quarter

    hourly: dict[int, list[float]] = {}
    for s in valid:
        bucket = int(s["elapsed_s"] // 3600)
        hourly.setdefault(bucket, []).append(float(s["rss_kib"]))

    lines = [
        "## RSS (resident set size)",
        "",
        "| Stat | Value |",
        "|---|---|",
        f"| First | {_fmt_gib(rss_values[0])} |",
        f"| Min | {_fmt_gib(min(rss_values))} |",
        f"| Median | {_fmt_gib(_percentile(rss_values, 0.5))} |",
        f"| Max | {_fmt_gib(max(rss_values))} |",
        f"| Last | {_fmt_gib(rss_values[-1])} |",
        f"| Slope (least-squares) | {slope:+.1f} KiB/hour |",
        f"| First-quarter mean | {_fmt_gib(first_quarter_mean)} |",
        f"| Last-quarter mean | {_fmt_gib(last_quarter_mean)} |",
        f"| Samples | {len(valid)} |",
        "",
        "| Hour | Samples | Mean RSS | Max RSS |",
        "|---|---|---|---|",
    ]
    for bucket in sorted(hourly):
        values = hourly[bucket]
        lines.append(
            f"| {bucket} | {len(values)} | {_fmt_gib(sum(values) / len(values))} | "
            f"{_fmt_gib(max(values))} |"
        )
    lines.append("")
    return "\n".join(lines)


def _duration_section(tick_log_lines: list[dict[str, Any]]) -> str:
    durations: list[float] = []
    for record in tick_log_lines:
        value = record.get("extra", {}).get("duration_seconds")
        if isinstance(value, int | float):
            durations.append(float(value))
    if not durations:
        return "## Tick duration\n\nNo tick-completed log lines were found.\n"

    lines = [
        "## Tick duration",
        "",
        "| Stat | Value |",
        "|---|---|",
        f"| p50 | {_percentile(durations, 0.5):.3f}s |",
        f"| p95 | {_percentile(durations, 0.95):.3f}s |",
        f"| max | {max(durations):.3f}s |",
        f"| Ticks | {len(durations)} |",
        "",
    ]

    timestamped = [
        (ts, record)
        for record in tick_log_lines
        if (ts := _parse_ts(record.get("timestamp"))) is not None
    ]
    if timestamped:
        t0 = min(ts for ts, _ in timestamped)
        hourly: dict[int, list[float]] = {}
        for ts, record in timestamped:
            value = record.get("extra", {}).get("duration_seconds")
            if not isinstance(value, int | float):
                continue
            bucket = int((ts - t0).total_seconds() // 3600)
            hourly.setdefault(bucket, []).append(float(value))
        lines += ["| Hour | Ticks | p50 | p95 | max |", "|---|---|---|---|---|"]
        for bucket in sorted(hourly):
            values = hourly[bucket]
            lines.append(
                f"| {bucket} | {len(values)} | {_percentile(values, 0.5):.3f}s | "
                f"{_percentile(values, 0.95):.3f}s | {max(values):.3f}s |"
            )
        lines.append("")
    return "\n".join(lines)


def _drift_section(
    tick_log_lines: list[dict[str, Any]], interval_seconds: float, jitter_seconds: float
) -> str:
    timestamps = sorted(
        ts
        for record in tick_log_lines
        if (ts := _parse_ts(record.get("timestamp"))) is not None
    )
    if len(timestamps) < 2:
        return (
            "## Scheduling drift\n\n"
            "Fewer than two tick timestamps — nothing to compare.\n"
        )

    expected = interval_seconds + jitter_seconds / 2
    gaps = [
        (timestamps[i + 1] - timestamps[i]).total_seconds()
        for i in range(len(timestamps) - 1)
    ]
    threshold = expected * _NOTABLE_GAP_MULTIPLE
    notable = [(i, gap) for i, gap in enumerate(gaps) if gap > threshold]

    lines = [
        "## Scheduling drift",
        "",
        f"Expected gap between ticks: ~{expected:.1f}s "
        f"(interval_seconds={interval_seconds:g} + "
        f"jitter_seconds/2={jitter_seconds / 2:.1f})",
        "",
        "| Stat | Value |",
        "|---|---|",
        f"| Mean gap | {sum(gaps) / len(gaps):.1f}s |",
        f"| p95 gap | {_percentile(gaps, 0.95):.1f}s |",
        f"| Max gap | {max(gaps):.1f}s |",
        f"| Gaps > {_NOTABLE_GAP_MULTIPLE:g}x expected | "
        f"{len(notable)} of {len(gaps)} |",
        "",
    ]
    if notable:
        lines.append("Notable gaps (tick index -> gap):")
        for i, gap in notable[:_MAX_NOTABLE_GAPS_SHOWN]:
            lines.append(f"- tick {i + 1}->{i + 2}: {gap:.1f}s")
        if len(notable) > _MAX_NOTABLE_GAPS_SHOWN:
            lines.append(f"- ... and {len(notable) - _MAX_NOTABLE_GAPS_SHOWN} more")
        lines.append("")
    return "\n".join(lines)


def _decision_section(journal_entries: list[dict[str, Any]]) -> str:
    if not journal_entries:
        return "## Decision distribution\n\nNo journal entries available.\n"
    counts: dict[str, int] = {}
    for entry in journal_entries:
        decision = entry.get("decision", "unknown")
        counts[decision] = counts.get(decision, 0) + 1
    total = sum(counts.values())

    lines = [
        "## Decision distribution",
        "",
        "| Decision | Count | % |",
        "|---|---|---|",
    ]
    for decision in ("idle", "act", "escalate"):
        count = counts.get(decision, 0)
        pct = (count / total * 100) if total else 0.0
        lines.append(f"| {decision} | {count} | {pct:.1f}% |")
    for decision, count in counts.items():
        if decision in ("idle", "act", "escalate"):
            continue
        pct = (count / total * 100) if total else 0.0
        lines.append(f"| {decision} | {count} | {pct:.1f}% |")
    lines.append(f"| **total** | **{total}** | |")
    lines.append("")
    return "\n".join(lines)


def _pause_episodes(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapses consecutive `paused=true` samples sharing a reason into one episode
    (start/end/reason) — a sample every `ANSINA_SOAK_SAMPLE_INTERVAL` while paused for
    hours would otherwise be hundreds of identical rows.
    """
    episodes: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for sample in samples:
        if sample.get("paused") is True:
            reason = sample.get("paused_reason") or "(manual pause — no reason)"
            if current is None or current["reason"] != reason:
                if current is not None:
                    episodes.append(current)
                current = {
                    "start": sample.get("elapsed_s"),
                    "end": sample.get("elapsed_s"),
                    "reason": reason,
                }
            else:
                current["end"] = sample.get("elapsed_s")
        elif current is not None:
            episodes.append(current)
            current = None
    if current is not None:
        episodes.append(current)
    return episodes


def _breaker_section(
    all_log_lines: list[dict[str, Any]], samples: list[dict[str, Any]]
) -> str:
    trips = [
        record
        for record in all_log_lines
        if record.get("message") == "heart tick: circuit breaker tripped"
    ]
    episodes = _pause_episodes(samples)

    if not trips and not episodes:
        return (
            "## Circuit breaker\n\n"
            "No circuit-breaker trips and no paused samples observed during the soak.\n"
        )

    lines = ["## Circuit breaker", ""]
    if trips:
        lines.append(f"{len(trips)} trip log line(s):")
        lines.append("")
        lines.append("| Timestamp | Reason | auto_pause_enabled |")
        lines.append("|---|---|---|")
        for record in trips:
            extra = record.get("extra", {})
            lines.append(
                f"| {record.get('timestamp', '?')} | {extra.get('reason', '?')} | "
                f"{extra.get('auto_pause_enabled', '?')} |"
            )
        lines.append("")
    else:
        lines.append(
            "No trip log lines were found, but the sampler observed `paused=true` at "
            "least once — likely a manual pause (`POST /heart/tick/pause`) during the "
            "soak window rather than a breaker trip:"
        )
        lines.append("")

    if episodes:
        lines.append(f"{len(episodes)} paused episode(s) observed by the sampler:")
        lines.append("")
        lines.append("| start (elapsed_s) | end (elapsed_s) | reason |")
        lines.append("|---|---|---|")
        for episode in episodes:
            lines.append(
                f"| {episode['start']} | {episode['end']} | {episode['reason']} |"
            )
        lines.append("")
    return "\n".join(lines)


def _journal_crosscheck_section(
    journal_entries: list[dict[str, Any]], tick_log_lines: list[dict[str, Any]]
) -> str:
    log_by_tick: dict[int, dict[str, Any]] = {}
    for record in tick_log_lines:
        extra = record.get("extra", {})
        tick = extra.get("tick")
        if isinstance(tick, int):
            log_by_tick[tick] = extra

    mismatches: list[str] = []
    covered = 0
    for row in journal_entries:
        tick = row.get("tick_number")
        log_entry = log_by_tick.get(tick)
        if log_entry is None:
            continue  # Outside the fetched page / log window — not a mismatch.
        covered += 1
        if row.get("decision") != log_entry.get("decision"):
            mismatches.append(
                f"tick {tick}: journal decision {row.get('decision')!r} != "
                f"log decision {log_entry.get('decision')!r}"
            )
        if row.get("prompt_tokens") != log_entry.get("prompt_tokens"):
            mismatches.append(
                f"tick {tick}: journal prompt_tokens {row.get('prompt_tokens')!r} != "
                f"log prompt_tokens {log_entry.get('prompt_tokens')!r}"
            )
        log_duration = log_entry.get("duration_seconds")
        row_duration = row.get("duration_seconds")
        if (
            not isinstance(log_duration, int | float)
            or not isinstance(row_duration, int | float)
            or abs(row_duration - log_duration) > 1e-6
        ):
            mismatches.append(
                f"tick {tick}: journal duration_seconds {row_duration!r} != "
                f"log duration_seconds {log_duration!r}"
            )

    lines = [
        "## Journal cross-check",
        "",
        "`GET /heart/journal` returns at most 500 rows per page (`_MAX_LIMIT`, "
        "`api/routes/heart_journal.py`) — for a multi-hour soak at a 30s cadence "
        "(~960 ticks over 8h) this covers only the most recent page's worth, not the "
        "whole run.",
        "",
        f"- Journal rows fetched: {len(journal_entries)}",
        f'- Matched against a `"heart tick completed"` log line: {covered}',
        f"- Mismatches: {len(mismatches)}",
        "",
    ]
    if mismatches:
        lines.append("Mismatches found:")
        lines.extend(f"- {m}" for m in mismatches)
        lines.append("")
    elif covered:
        lines.append(
            "Every covered journal row matches its daemon log line exactly (the same "
            "check `scripts/heart_journal_smoke_verify.py` runs, re-run here at soak "
            "scale)."
        )
        lines.append("")

    non_idle = [row for row in journal_entries if row.get("decision") != "idle"]
    lines.append(f"## Non-idle journal entries ({len(non_idle)})")
    lines.append("")
    if non_idle:
        lines.append("| tick | decision | note |")
        lines.append("|---|---|---|")
        for row in non_idle:
            note = str(row.get("note") or "").replace("|", "\\|")
            tick = row.get("tick_number")
            decision = row.get("decision")
            lines.append(f"| {tick} | {decision} | {note} |")
    else:
        lines.append("None — every journal row in the fetched page decided `idle`.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    run_log_path = Path(sys.argv[1])
    samples_path = Path(sys.argv[2])
    journal_path = Path(sys.argv[3])
    dest_dir = Path(sys.argv[4])

    run_log_text = run_log_path.read_text(encoding="utf-8")
    metadata = _parse_run_metadata(run_log_text)

    all_log_lines = load_json_log_lines(run_log_path)
    tick_log_lines = [
        record
        for record in all_log_lines
        if record.get("message") == "heart tick completed"
    ]
    samples = _load_samples(samples_path)
    journal_entries = _load_journal_entries(journal_path)

    interval_seconds = float(metadata.get("interval_seconds", 30.0))
    jitter_seconds = float(metadata.get("jitter_seconds", 3.0))

    run_date = _run_date(samples, tick_log_lines)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = _unique_stem(dest_dir, f"soak-{run_date}")

    report_md = "\n".join(
        [
            _header(metadata, samples, run_date),
            _rss_section(samples),
            _duration_section(tick_log_lines),
            _drift_section(tick_log_lines, interval_seconds, jitter_seconds),
            _decision_section(journal_entries),
            _breaker_section(all_log_lines, samples),
            _journal_crosscheck_section(journal_entries, tick_log_lines),
        ]
    )

    md_path = dest_dir / f"{stem}.md"
    md_path.write_text(report_md, encoding="utf-8")

    summary = {
        "metadata": metadata,
        "sample_count": len(samples),
        "tick_log_line_count": len(tick_log_lines),
        "journal_entry_count": len(journal_entries),
    }
    json_path = dest_dir / f"{stem}.json"
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
