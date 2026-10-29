# OpenCode: the confined "Hands" foundation

`[opencode] enabled = true` is M9's starting point — Ansina hosting
[OpenCode](https://opencode.ai) (`anomalyco/opencode`) as an external, subprocess-
driven, workspace-confined executor rather than building one. Issue #74 shipped the
foundation: workspace provisioning, generated per-run config, an allow-list child
environment, and a logged-skip preflight — with no spawn at all beyond `preflight()`'s
own two readiness checks (`--version`, `debug config`). Issue #75's
`ansina.opencode.runner.OpencodeCliRunner` is the first thing that actually spawns a
real `opencode` child to do work: a one-shot `opencode run --format json ...`
subprocess per call — confined to the workspace via `cwd`, since `opencode run` has
no directory flag at all (issue #81) — streaming its line-delimited JSON events into a
callback as they arrive, enforcing a wall-clock timeout (terminate→kill) and a
concurrency/backlog bound, and never raising once that spawn has been attempted —
every failure (a bad spawn, a non-zero exit, a timeout, an output-size cap) resolves
into a typed `RunOutcome` instead. `build_opencode_service(settings)` is the one
factory: `None` on a failed preflight (unchanged from #74), a real, ready-to-call
`OpencodeCliRunner` on a clean pass. **Still wired into no route or tick-loop caller**
as of #75 — see "Verifying it" below for how to drive a real run by hand.
`[opencode] enabled = false` (the default) is a byte-for-byte no-op: no directory is
created, no file is written, nothing is logged, and no subprocess is ever spawned.

**Ansina targets OpenCode v2**, not v1 — the two are materially different CLIs (see
"Installing it" below for why that matters even at install time, before any Ansina
code runs).

## Installing it

Neither CI leg installs `opencode` (same posture as Vector — see `docs/dev-mode.md`),
and the real binary isn't needed for anything in this repository to boot, test, or
pass `make check`. On a Mac Mini or dev machine that will actually run an executor:

```bash
make opencode-install ARGS='--version 2.1.1'   # pick a real, current v2 release
```

This wraps the official installer (`curl -fsSL https://opencode.ai/v2/install | bash`)
via `scripts/opencode-install.sh` — never typed ad hoc, the same "complexity lives in
one script behind one `make` target" discipline every other real-network/real-binary
tool in this codebase follows. It refuses to run with no `--version`/`-v` (or
`--binary`/`-b`) given: **no OpenCode release has been validated against Ansina yet**
(`[opencode] required_version_prefix` defaults to `""`, meaning nothing is pinned), so
this script will not silently install "latest" and let an unreviewed upgrade happen by
accident. Two things worth knowing, verified against the real v2 install script rather
than assumed:

- `https://opencode.ai/install` (no `/v2/`) redirects to the **v1** branch's own
  installer — a different CLI, including a different `--version` output shape (v1
  prints a bare `"1.19.3"`; v2 prints `"opencode v2.1.1"`). Always use the `/v2/`
  endpoint, which is what `opencode-install.sh` calls.
- The downloaded artifact is a single self-contained binary built via `bun build
  --compile`, which bundles the Bun runtime *inside* the executable. **No separate
  Bun or Node.js install is needed** on the target machine, despite OpenCode itself
  being written in TypeScript.

The script prints the real `opencode --version` output once the install finishes —
copy that straight into `ANSINA_OPENCODE__REQUIRED_VERSION_PREFIX` once you've
actually run and validated against it (the same "pin only what you've proven,"
Vector-0.45.x-lesson discipline this codebase applies everywhere else).

## Why `enabled = true` also requires `[telemetry] enabled = true`

Unlike every other `[section] enabled` flag in this codebase, turning OpenCode on
requires `[telemetry] enabled = true` too — refused at boot with a `ConfigError`,
not a logged skip, if it isn't. Every other "degrade gracefully" subsystem here
(`[telemetry.s3]`, `[dev]`) only ever ships something that was already captured: a
missing bucket or a dead sidecar leaves a bench/soak report or a telemetry file
sitting unshipped, recoverable later. An unobserved OpenCode run is different in
kind — a follow-up issue emits every tool call, permission denial, and outcome as
telemetry or nothing at all, so a run that happens with telemetry off never existed
anywhere Ansina can account for it. That's the one thing this milestone refuses to
allow for an executor with real `bash`/`edit` reach.

## The confinement model

- **A dedicated workspace** (`[opencode] workspace_dir`, default
  `~/.local/state/ansina/opencode`) — refused at boot if it is, is inside, or
  contains this ansina checkout itself (walking up from `workspace_dir` for a
  `pyproject.toml` naming `"ansina"`, and checking whether this running code's own
  checkout sits inside `workspace_dir` the other way around).
- **An isolated `HOME`** (`[opencode] isolate_home = true`, the default) — redirects
  the child's `HOME` into a subdirectory of `workspace_dir`. This is a security
  finding, not tidiness: see "The config-merge finding" below.
- **A deny-by-default permission block** (`[opencode.permission]`) — `"*": "deny"` is
  always the floor; `edit`/`bash` are Ansina's own typed modes (`workspace`/
  `allowlist`), translated into OpenCode's native glob-map shape by
  `ansina.opencode.config`. `"ask"` is unrepresentable anywhere in this section by
  construction — a headless `--auto` run has nobody to answer a prompt, so an `"ask"`
  value would hang a real run until its own timeout.
- **An explicit env allow-list** (`ansina.opencode.env.build_opencode_env`) — only
  `PATH`/`TMPDIR` are inherited unfiltered; everything else (the provider API key,
  operator-named `inherit_env_names` entries) is named explicitly. Any
  `ANSINA_`-prefixed or secret-shaped name (`AWS_*`, `*_API_KEY`, `*SECRET*`,
  `*TOKEN*`) is refused outright at config-load time — `inherit_env_names` can never
  be used to smuggle a real secret into the child's environment under cover of a
  legitimate-looking name.

## The config-merge finding (and its residual risk)

OpenCode's own documented config precedence **merges** every source together rather
than replacing one with another: remote → global (`~/.config/opencode/opencode.json`)
→ `OPENCODE_CONFIG` → project (`opencode.json`) → `.opencode/` dirs →
`OPENCODE_CONFIG_CONTENT` → managed config files → macOS managed preferences (later
wins). Project config does rank above the global file — but because sources *merge*
rather than override wholesale, any permission key an operator set in their own
global config that Ansina's generated `opencode.json` doesn't also name survives into
the effective, resolved configuration. Redirecting the child's `HOME` removes the
global file from the merge chain entirely; `OPENCODE_CONFIG_CONTENT` (set to the
identical rendered permission block) is defense in depth, since it ranks above
project config too.

**Residual risk, named rather than solved**: managed config files and macOS managed
preferences outrank even `OPENCODE_CONFIG_CONTENT`, and redirecting `HOME` does not
remove either from the merge chain — neither lives at a path this module can read and
account for directly. `ansina.opencode.preflight`'s own final check re-reads the
*effective*, fully-resolved permissions (`opencode debug config`) and refuses to
proceed if anything looser than configured — or a literal `"ask"` anywhere — crept in
from a layer above Ansina's own. This is the check that actually catches that case;
everything else here only makes the *common* case (an ordinary global config file)
unreachable.

## Verifying it

Preflight-only, with no real binary (works on any machine, including CI):

```bash
uv run python -c "
from ansina.config import load_settings
from ansina.opencode import build_opencode_service

settings = load_settings()
print(build_opencode_service(settings))
"
```

With `[opencode] enabled = false` (the default), this prints `None` and creates no
directory — it does log one `WARNING` line naming the skip reason, the same way
`ansina.dev.vector.preflight()` already logs a specific reason for *every* skip,
including "disabled." That's calling the preflight function directly, though; nothing
in the daemon's own boot sequence calls it yet (no route or tick-loop caller exists),
so an ordinary `uv run ansina` with the default config logs nothing about OpenCode at
all. With `enabled = true` and `[telemetry] enabled = true`, this snippet preflights
for real: binary resolution, an optional pinned-version check, workspace provisioning,
the generated `opencode.json`'s own round-trip, and the effective-permission
assertion above — returning `None` (with a specific logged skip reason) on any failed
check, or (issue #75) a real, ready-to-call `OpencodeCliRunner` on a clean pass.
Neither CI leg installs `opencode` (same reasoning as Vector — see
`docs/dev-mode.md`), so both always exercise the "binary not found" skip path for real.

Driving a real run, once a pinned `opencode` binary is installed (see "Installing it"
above) and `[opencode] enabled = true`/`[telemetry] enabled = true` are configured:

```bash
uv run python -c "
import asyncio

from ansina.config import load_settings
from ansina.opencode import RunRequest, build_opencode_service

settings = load_settings()
service = build_opencode_service(settings)
if service is None:
    raise SystemExit('preflight failed — see the logged WARNING for why')

outcome = asyncio.run(
    service.run(RunRequest(prompt='say hello'), lambda event: print(event))
)
print(outcome)
"
```

Every classified event prints as it arrives (`RunEvent.type`/`raw_type`/`payload` —
`payload` already truncated to `[opencode] max_event_bytes` and redacted); the final
`RunOutcome` reports `status` (`completed`/`failed`/`timeout`), the real exit code, and
counts of emitted/skipped events. `RunEventType`'s own lookup table is intentionally
empty as of #75 (see `ansina.opencode.events`'s module docstring) — every event
normalizes to `RunEventType.OTHER` today, with `raw_type` carrying the real,
as-emitted string regardless; a captured sample from a real run like this one is what
a future change would use to populate that table with confirmed, not guessed, OpenCode
event-type literals.

## Run telemetry: `kind=opencode/`

Issue #76 adds `ansina.opencode.telemetry.OpencodeTelemetryWriter` — the third
telemetry *shape* alongside M7's interval sampler and log mirror (see
`docs/dev-mode.md`): one JSON line per run-lifecycle event (`submitted`,
`started`, each in-run event, a terminal `completed`/`failed`/`timeout`/
`cancelled`, and an `intervention` event — reserved at #76, populated by issue
#79's `OpencodeInterventionHandler`), through the same generic
`SpooledRotatingWriter` under its own `"opencode"` file family,
shipped by Vector's third source/sink pair (`deploy/vector.toml`) to
`host=<id>/kind=opencode/dt=<date>/`. This is a deliberate, load-bearing
sequencing decision, not an afterthought: a run must not be submittable over
HTTP before its own trace is already shipping — see `docs/ml/corpus-contract.md`'s
"Opencode run event" family for the full field reference and its `seq`-based
gap-detection property.

**Wired in as of issue #77**: `build_opencode_telemetry_writer(settings)` is the
telemetry half `ansina.opencode.service.build_opencode_run_service` constructs
alongside `build_opencode_service`'s own runner — see "Submitting a run over
HTTP" below for the route that drives both.

## Submitting a run over HTTP

Issue #77 is what actually exposes OpenCode as an inbound surface: `POST
/opencode/runs`, `GET /opencode/runs`, `GET /opencode/runs/{id}`, `POST
/opencode/runs/{id}/cancel` (resource `opencode.runs`), plus `GET
/opencode/status`, `POST /opencode/intake/pause`, `POST /opencode/intake/resume`
(resource `opencode.intake`) — all gated `require(...)` like every other route in
this codebase, and all answer 503 `problem+json` (`ansina.opencode.disabled`)
when `app.state.opencode` is `None` (`[opencode] enabled = false`, or a failed
preflight), checked only *after* authorization has already passed. `POST
/opencode/runs`/`.../cancel`/`.../intake/pause`/`.../intake/resume` are
`sensitive=True` — launching, or killing, an autonomous coding agent demands a
live sudo grant for every role but `Admin`, the same discipline `auth.*` mutations
already use for a different reason.

`POST /opencode/runs` returns `202` with the newly created, `queued` run — the
actual execution happens in a tracked background task, never inside the request
itself. **Every run produces its `submitted` telemetry line before the route
returns**, not merely "eventually" — `ansina.opencode.service.OpencodeRunService
.submit()` writes it synchronously before handing the run back, a tested
property (`tests/e2e/test_server.py`'s own fake-executor test reads the spooled
file back immediately after the `202`, before polling anything). `opencode_runs`
is this codebase's first *mutable* status table — every other one
(`heart_journal`, `login_attempts`, …) is deliberately append-only; see
`storage/migrations/0012_opencode_runs.sql`'s own header comment for why a run's
real lifecycle (`queued` → `running` → one of `succeeded`/`failed`/`timeout`/
`cancelled`) earns the exception, with the *append-only* trace of that same
lifecycle living in the `kind=opencode/` telemetry stream instead. A row left
`queued`/`running` by a crashed or killed daemon is swept to `failed` on the next
boot (`error_detail = "daemon restarted"`), so a restart can never poison a
later active-run count.

`GET /opencode/runs` pages newest-first with `limit`/`since`, byte-for-byte the
`GET /heart/journal` shape (`has_more` via one extra row, a malformed `since` →
400 `ansina.request.invalid`). `POST /opencode/intake/pause` is the operator-
facing half of the one intake kill switch issue #79's own bounded
`OpencodeAction.PAUSE_INTAKE` intervention also calls (`OpencodeRunService
.pause_intake()`, the exact same method) — `POST /opencode/runs` refuses with
409 `ansina.opencode.intake_paused` while it's set, independently of whether
`[opencode] enabled` itself is `true`. See "The Heart's bounded interventions"
below for the full mechanism.

Hand-run verification, since neither CI leg installs a real `opencode` binary:

```bash
make opencode-install ARGS='--version 2.1.1'   # once, if not already installed
make opencode-smoke                             # boots ansina, submits a real run, verifies telemetry
```

`make remote-opencode`/`-tail`/`-attach` run the identical check on a remote
host over ssh (see `.envrc`'s `ANSINA_REMOTE_HOST`/`ANSINA_REMOTE_PATH`, the same
two variables `make remote-heart`/`make remote-dev-mode` already use) — the same
force-sync-then-tmux shape `scripts/remote-dev-mode.sh` established, just
wrapping `make opencode-smoke` instead.

## The Heart's bounded interventions

Issue #79 gives the Heart its first ability to act on OpenCode, within a fixed,
enumerable set — never free-form, never model-authored text reaching a
subprocess argv/prompt/path. Gated by `[heart.tick] opencode_interventions`
(default `false`, a byte-for-byte no-op — `build_tick_loop` never constructs the
handler at all), composed alongside `JournalDecisionHandler`/
`BrainEscalationHandler`/`FaultInjector` in `heart.tick.loop.build_tick_loop`.

`ansina.opencode.interventions.OpencodeAction` is the closed set:

- **`retry`** — resubmits a `failed`/`timeout` run's own prompt as a fresh run
  (no session continuation), up to `[heart.tick] opencode_max_retries_per_run`
  attempts per run.
- **`nudge`** — resubmits a fixed, code-generated continuation prompt against a
  stalled run's own `--session`, since `AgentRunner` is one-shot per call with no
  interactive stdin to send more input to a still-running process.
- **`compact`** — ships a real, verified `unsupported` outcome as of this issue.
  Verified against a real, installed OpenCode v2.1.4 binary: a compact entry
  point does exist (`POST /api/session/{id}/compact`, "Compact session" in the
  binary's own `/openapi.json`), but there is no CLI subcommand or flag for it
  anywhere (`opencode --help`, every subcommand's own `--help`, and
  `opencode session --help` were all checked directly) — it is reachable only
  over the HTTP API `opencode serve` exposes, not through the one-shot
  `opencode run` subprocess model `OpencodeCliRunner` uses. A documented no-op,
  not a silent gap: wiring the real endpoint needs a second, server-based
  `AgentRunner` adapter, which that port's own module docstring already
  anticipates as a pure, future append ("a future `opencode serve` + httpx
  adapter") — out of scope here.
- **`pause_intake`** — calls the exact same `OpencodeRunService.pause_intake()`
  method `POST /opencode/intake/pause` does.
- **`escalate_to_brain`** — reuses `BrainEscalationHandler`'s own
  `BrainProvider.stream()` call/journal-column pattern, gated *additionally* by
  `[heart.tick] escalate_to_brain` (the older, M7 flag) — the Brain is never
  reachable through this new path while that flag stays off.

*Which* action fires, if any, is chosen by `DeterministicInterventionPolicy` — a
documented priority ladder over real `OpencodeStateProbe` signals (the same ones
issue #78's `OpencodeStateSource` already reports), bounded by three budgets
(`opencode_max_retries_per_run`, `opencode_intervention_cooldown_ticks`,
`opencode_max_interventions_per_hour`) so no feedback loop can amplify. The
Heart's own model output is consumed only as "act / don't act"
(`TickDecision.ACT`) — never as the action or the target run id.

Every attempt — applied, declined, unsupported, or errored — is journaled as a
second `heart_journal` row (sharing the triggering tick's `tick_number`,
`decision=act`; see `0013_heart_journal_opencode_intervention.sql`) and emitted
as the `intervention` line in the `kind=opencode/` telemetry stream (reserved by
issue #76, populated here) — see `docs/ml/corpus-contract.md`'s "Opencode run
event" family for the full field reference.

## Config reference

`ansina.example.toml`'s `[opencode]` and `[opencode.permission]` blocks document every
setting inline, including why `enabled = true` requires `[telemetry] enabled = true`
and how to pin `required_version_prefix` against a specific OpenCode v2 release (a
raw-prefix match against the trimmed `--version` output, e.g. `"opencode v2."` —
never a v1 release, which this project doesn't target and whose own `--version`
output shape differs, as "Installing it" above explains). Issue #75 adds the keys
`OpencodeCliRunner` itself reads: `run_timeout_seconds`/`shutdown_timeout_seconds`
(the per-run wall-clock cap and its terminate→kill grace window),
`max_concurrent_runs`/`queue_max_depth` (how many children may run at once, and how
large a backlog of admitted-but-not-yet-running calls is allowed before a further one
is refused with `OpencodeBusyError`), `max_prompt_chars`/`max_event_bytes`/
`max_run_output_bytes` (three independent size caps — the request itself, one
captured JSON line, and the whole run's own output), and `model`/`agent`/
`auto_approve` (per-run `--model`/`--agent`/`--auto` defaults, overridable per call via
`RunRequest`). Issue #77 adds the three keys `opencode_runs`' own retention and the
intake kill switch's boot-time state read from: `max_runs_retained`/
`run_retention_days` (the identical two-bound sweep shape `[heart.journal]`
`max_entries`/`retention_days` already use) and `intake_paused_on_boot` (whether
`OpencodeRunService` starts with intake paused — independent of `enabled` itself).
