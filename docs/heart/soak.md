# Heartbeat soak procedure

Issue #56's "learn" step: run the tick loop unattended, for hours, on the real target
hardware (Mac Mini M4), and measure what #53/#54/#55 only ever proved on a run of
minutes — whether process RSS stays bounded, whether tick duration or scheduling
cadence drift over time, and what the model actually decides in a healthy steady
state over hundreds of real ticks. This document is the procedure; a run's own output
is a rendered report under `docs/heart/soak/` (gitignored — see below), not this file.

## What this is not

The harness this procedure drives boots the real daemon with
`ANSINA_SECURITY__ENABLED=false` against a scratch SQLite database on loopback — a
measurement rig, not a deployment recipe. For how to actually run Ansina with the
Heart enabled, see `docs/heart/mac-mini-m4.toml`.

## Prerequisites

- `.envrc` (gitignored) on this machine, with the same two variables
  `scripts/remote-heart.sh`/`scripts/heart-journal-smoke.sh` already require:

  ```bash
  export ANSINA_REMOTE_HOST=<user@host>
  export ANSINA_REMOTE_PATH=<path to the ansina checkout on that host>
  ```

- A **pushed** branch — `scripts/heart-soak.sh start` refuses a dirty worktree or an
  unpushed HEAD, the same discipline `remote-heart.sh`/`heart-journal-smoke.sh`
  already enforce, since the Mac Mini soaks `origin/<branch>`, never an unpushed local
  commit.
- `uv sync --extra mlx` already run on the Mac Mini at least once (`make
  heart-bench-sync` does this automatically at the start of every soak run, but the
  first sync downloads the model and takes longer than subsequent ones).

## Running a soak

```bash
make remote-heart-soak-start              # launches, returns immediately
make remote-heart-soak-status             # is it still running? how far in?
make remote-heart-soak-tail               # tail the live log
make remote-heart-soak-attach             # attach to the tmux session interactively
make remote-heart-soak-fetch              # once finished: fetch + render the report
make remote-heart-soak-stop               # end it early, if needed
```

`start` is the one call that force-syncs the remote checkout and launches
`scripts/heart-soak-run.sh` in its own tmux session (`ansina-soak`) — then returns.
Unlike `make remote-heart`/`make remote-heart-journal-smoke`, it does **not** block
waiting for the run to finish: an 8-hour soak cannot sit inside one blocking `make`
call, let alone one terminal session. `status`/`fetch` are separate, later
invocations — run them from a different session, a different day, even a different
laptop, as long as `.envrc` points at the same Mac Mini. The soak itself runs detached
on the Mac Mini the whole time, indifferent to whether this machine stays on.

Default duration is 8 hours (`ANSINA_SOAK_HOURS`, also accepted as the first argument
to `start`, e.g. `make remote-heart-soak-start ARGS=4` for a 4-hour run). A short
run (e.g. `ARGS=0.05`, ~3 minutes) is useful for validating the tooling end to end
before committing to an overnight run.

## What is sampled, and how often

`scripts/heart-soak-run.sh` boots the real daemon (`[heart] enabled = true`, the
Mac-validated profile's `model_repo`/`interval_seconds`/`jitter_seconds` — see
`docs/heart/mac-mini-m4.toml`) and, every `ANSINA_SOAK_SAMPLE_INTERVAL` seconds
(default 60s), appends one JSON line to `samples.jsonl`:

```json
{"t": 1759400000.1, "elapsed_s": 3600, "rss_kib": 3741184, "ticks": 118,
 "paused": false, "paused_reason": null, "last_decision": "idle",
 "last_duration_seconds": 0.21}
```

`rss_kib` comes from `ps -o rss=` against the real MLX/python process (resolved via
`pgrep -P` off the `uv run` wrapper, falling back to the wrapper PID itself); every
other field is read straight from `GET /heart/tick`. At the end of the run, the
daemon's own structured `run.log` (every `"heart tick completed"` line, and any
`"heart tick: circuit breaker tripped"` line) and the most recent page of
`GET /heart/journal` (`journal.json`, capped at 500 rows — see below) are captured
alongside `samples.jsonl`.

## Reading a rendered report

`make remote-heart-soak-fetch` renders `docs/heart/soak/soak-<date>.md` (auto-
suffixed `-2`, `-3`, ... on a same-day collision — never overwritten) via
`scripts/heart_soak_report.py`. Its sections, in order:

- **RSS** — first/min/median/max/last, a per-hour table, a least-squares slope in
  KiB/hour, and a first-quarter vs. last-quarter mean comparison. **Pass**: the slope
  is small and the quarter means are close — growth that looks roughly flat across
  the run, not a steady climb. A slope of a few hundred KiB/hour across an 8-hour run
  is noise (e.g. allocator fragmentation); anything that would extrapolate to
  gigabytes over a week of real uptime is not.
- **Tick duration** — p50/p95/max overall and per hour. **Pass**: per-hour p95 stays
  roughly flat across the run (no hour trending markedly slower than hour 0).
- **Scheduling drift** — the gap between consecutive completed ticks against the
  expected `interval_seconds + jitter_seconds/2`, plus every gap more than 1.5x that
  expectation named explicitly. **Pass**: no notable gaps, or any that appear are
  explained (e.g. a deliberate manual pause during the run).
- **Decision distribution** — idle/act/escalate counts and percentages over the run.
- **Circuit breaker** — every trip log line and every paused episode the sampler
  observed, each with its reason; "none occurred" is printed explicitly when true, so
  the issue's own AC ("any pause events are called out explicitly") is answered
  either way, not just by omission.
- **Journal cross-check** — journal rows vs. log lines, re-running
  `scripts/heart_journal_smoke_verify.py`'s own agreement check at soak scale. Note
  the stated caveat: `GET /heart/journal`'s own page cap (500 rows) covers only the
  most recent ~4 hours of an 8-hour run at the default cadence, not the whole thing.
- **Non-idle journal entries** — every `act`/`escalate` row in the fetched page, with
  its code-generated note verbatim — the raw material a genuine-vs-noise review (the
  PR that lands a given soak run) draws from.

## Running a soak with fault injection (issue #72)

Every soak run above — and every day of live telemetry — is 100% `idle`: nothing in
the harness or the live deployment profile ever makes a real fault happen, so there
is no non-idle ground truth anywhere in the corpus to score a tick-decision policy
against. `ansina.heart.tick.fault_injection` closes that gap by forcing real,
scheduled faults (a readiness check failing, consecutive tick failures, consecutive
tick overruns) during an otherwise-ordinary soak, so the resulting act/escalate
decisions have ground truth to compare against — see that module's own docstring for
the mechanics (it never fabricates snapshot text; every fault is forced for real).

```bash
export ANSINA_SOAK_FAULT_SCHEDULE=docs/heart/fault-schedules/example.json
make remote-heart-soak-start ARGS=0.05   # a short validation run first
```

`docs/heart/fault-schedules/example.json` (committed) is a ready-to-use schedule
recreating `heart/eval/data/tick_fixtures.jsonl`'s own `self_state_fault`-tagged
scenarios — a single failure, a persistent failure, a stuck readiness check, an
overloaded backend, and the "two simultaneous axes" combination — against the
Mac-validated profile's `interval_seconds`/`overrun_ratio`/`max_consecutive_failures`
(`docs/heart/mac-mini-m4.toml`). Copy and edit it (or write a new one from scratch,
following `ansina.heart.tick.fault_injection.FaultWindow`'s own field shape) for a
longer or differently-shaped run; `[heart.tick] auto_pause_enabled = false` is worth
setting too (`ANSINA_SOAK_TICK_INTERVAL`'s sibling env vars, or edit the schedule's
`duration_ticks`) if a window is deliberately long enough to trip the circuit
breaker and you don't want that to end the run early.

`cmd_start` scp's the schedule to the Mac Mini and `heart-soak-run.sh` boots the
daemon against it; `cmd_fetch` pulls the schedule back down alongside the usual
`run.log`/`samples.jsonl`/`journal.json`, and `heart_soak_report.py` adds a **Fault
injection** section scoring every fetched `GET /heart/journal` row that falls within
the schedule's own tick range against its `expected_decision` — correct, under-
reacted (a miss), or over-reacted (a false alarm), the same `idle < act < escalate`
severity order `ansina.heart.tick.fault_injection` itself uses when two different-kind
windows overlap. With no `ANSINA_SOAK_FAULT_SCHEDULE` set, this section is simply
absent — every soak run before this one, unaffected.

Because `GET /heart/journal` returns at most 500 rows per page, point the schedule's
own tick numbers at the *end* of a long run (or keep the run short) so the window
you want scored is still inside the fetched page — the same caveat the journal
cross-check section above already documents.

## If the soak finds a real bug

Fix it and re-run, the same standing rule #54 and #55 both set on this milestone (see
`AGENTS.md`'s M6 entries) — a soak's whole purpose is to catch what a short run
can't, and deferring a real finding back into a vague TODO defeats that purpose.

## Where the report goes

`docs/heart/soak/` is gitignored, not committed — the same decision #58 made for
`docs/heart/bench/`, for the same reason: a soak's raw `samples.jsonl` plus `run.log`
can run to hundreds of KiB to low MiB per run, and a corpus that only ever grows
belongs in an object store, not git history. **Issue #59** gives it exactly that: set
`[telemetry.s3]` in `ansina.toml` (bucket/endpoint/region) plus the two
`ANSINA_TELEMETRY__S3__*` credentials in `.envrc`, then run `make heart-bench-publish`
to upload every soak artifact set here (and every bench report in `docs/heart/bench/`)
not already in the bucket — a verified no-op on a second run. With `[telemetry.s3]`
left disabled (the default), `make remote-heart-soak-fetch` writes here for local
analysis only, and the numbers that matter are called out in the PR that lands this
work either way — Ansina itself does not maintain a standing findings/evidence
write-up; that analysis, if any, is the consumer corpus's own job (see
`docs/ml/corpus-contract.md`), delivered back as a GitHub issue when it has
something actionable to say, not as a file this repo carries forward.
