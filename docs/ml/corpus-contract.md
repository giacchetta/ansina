# Heart corpus contract — `schema_version: 1`

Issue [#63](https://github.com/giacchetta/ansina/issues/63). This document is the
single source of truth for the shape of every artifact Ansina writes to its
S3-compatible report bucket (`[telemetry.s3]`, issue #59). The machine-readable
mirror of this exact document is `corpus-schema-v1.json` in this same directory,
published byte-for-byte to `_contract/corpus-schema-v1.json` in the bucket (see
"Self-discovery" below) — the two are reviewed together in the same PR whenever
either changes, so there is never a second, independently-maintained copy to drift
out of sync.

## Lifecycle: Ansina is a producer, nothing more

Ansina writes to this bucket. It does not read from it, does not know who (if
anyone) reads from it, and names no consumer anywhere in its own source, docs, or
config. Whatever external process eventually analyzes this corpus is entirely out of
scope here, by design — the boundary this document draws is one-way.

From this issue onward, the expected direction of *change* is also one-way, but in
the opposite sense: once a consumer exists and has something to say about this
contract (a field it needs that isn't here, a key layout gap it hit, a stability
problem), that arrives as a **new GitHub issue filed against this repo** — never as
a code change this repo reaches out to pull in, and never as this repo polling or
otherwise depending on anything about a consumer's existence or state. This
document itself may be rewritten by such an issue; it is not frozen, only
versioned.

## Stability promise

- A **new artifact family**, or a new optional/nullable field on an existing one, is
  a **minor**, non-breaking addition — consumers reading defensively (ignoring
  unknown fields, treating optional ones as optional) are unaffected. It does not
  bump `schema_version`.
- **Removing or renaming a field**, or changing a field's type or semantics (e.g.
  `rss_kib` changing from an instantaneous to a cumulative reading), is a
  **breaking** change. It bumps `schema_version` and publishes a new
  `_contract/corpus-schema-v<N+1>.json` alongside the old one — the old file is
  never deleted or overwritten, so anything still reading the old version keeps
  working against it.
- `schema_version` is one number for the whole document, not per family: a breaking
  change to *any* family's shape bumps the whole document's version, since a
  consumer discovering the contract (see below) needs one number to check, not one
  per family.

## Self-discovery: `_contract/corpus-schema-v<N>.json`

Every version of this contract that has ever shipped is published, unmodified, to
`_contract/corpus-schema-v<N>.json` in the bucket (uploaded by
`ansina.ml.contract.publish_contract_schema` via issue #59's `ReportStorage` port,
as part of every `make heart-bench-publish` run — see that module's own docstring).
A reader needs no access to this repository's source to learn the current schema:
list `_contract/`, read the highest `<N>`, done. The *currently committed* `<N>` is
whatever `corpus-schema-v<N>.json` is named in this directory — `v1` as of this
writing.

## Bucket key layout

Hive-partitioned (`key=value/` segments), so a query engine can read it with no ETL
step. Four families actually land in the bucket; three key prefixes, since bench and
soak share no prefix collision risk with telemetry/runlog:

```
s3://<bucket>/<prefix>/kind=bench/dt=<date>/<stem>.{md,json}
s3://<bucket>/<prefix>/kind=soak/dt=<date>/<run_id>/{samples.jsonl,run.log,
    journal.json,soak-<date>.md,soak-<date>.json}
s3://<bucket>/<prefix>/kind=telemetry/dt=<date>/<vector-assigned-filename>
s3://<bucket>/<prefix>/kind=runlog/dt=<date>/<vector-assigned-filename>
s3://<bucket>/<prefix>/_contract/corpus-schema-v<N>.json
```

`<prefix>` is `[telemetry.s3] key_prefix`, applied/stripped entirely inside
`ReportStorage`/Vector's own sink config — nothing above is prefix-aware.

**`kind=telemetry/`/`kind=runlog/` are produced by Vector (issue #62), not by any
Python code in this repo**, per `deploy/vector.toml`'s two `aws_s3` sink
definitions. The filename under `dt=<date>/` is whatever Vector's own `aws_s3` sink
assigns it (not a name this repo controls or documents) — contrast with
`kind=bench/`/`kind=soak/`, where `ansina.heart.eval.storage`'s own `bench_key`/
`soak_key` name the exact filename.

### Known v1 limitation: no host attribution

Neither `kind=telemetry/` nor `kind=runlog/` carries any partition or field
identifying *which machine* produced a given object, and no field on the telemetry
sample (below) carries a hostname either. Two hosts shipping to the same bucket
(e.g. a dev laptop and the Mac Mini M4 deployment target) are today
indistinguishable in the corpus. This is a real, named gap — not fixed by this
issue, per its own scope (`heart/eval/storage.py`'s bucket-layout docstring already
anticipated `_contract/` as this issue's own concern, not a producer-code fix). Two
concrete fixes exist, named here as the path rather than built:

1. Add a `host=<id>/` segment to `deploy/vector.toml`'s two `key_prefix` values,
   injected via `ansina.dev.vector.build_vector_env`.
2. Add a `host` field to `ansina.telemetry.sampler.TelemetrySample`.

Either is exactly the kind of change that should arrive as a future issue against
this repo once an actual consumer needs to disambiguate two hosts — per the
Lifecycle section above.

## Per-family schema

### 1. Bench report (`kind=bench/`)

One JSON object per bench run (`ansina.heart.eval.report.report_to_json`), the
authoritative shape; the paired `.md` is a human-readable rendering of the same
data, not independently schema'd.

| Field | Type | Notes |
|---|---|---|
| `model_repo` | string | HF repo id benched |
| `prompt_variant` | string | one of `heart.tick.prompts.PROMPT_VARIANTS` |
| `chat_template` | bool | whether the tokenizer's chat template was applied |
| `generated_at` | string (ISO 8601, UTC) | |
| `host` | string | `platform.platform()` |
| `commit` | string \| null | `-dirty`-suffixed if the worktree had uncommitted changes; `null` if undeterminable |
| `branch` | string \| null | `null` if undeterminable |
| `mlx_lm_version` | string \| null | `null` if `mlx-lm` isn't installed |
| `max_output_tokens` | int | |
| `fixture_count` | int | |
| `metrics.accuracy` | float (0–1) | |
| `metrics.recall_by_class` | object (`"idle"`/`"act"`/`"escalate"` → float) | |
| `metrics.class_counts` | object (same keys → int) | |
| `metrics.recall_by_tag` | object (fixture tag → float) | |
| `metrics.tag_counts` | object (same keys → int) | |
| `metrics.parse_fallback_rate` | float (0–1) | |
| `metrics.false_act_or_escalate_on_obvious_idle` | int | |
| `metrics.latency_p50_seconds` / `latency_p95_seconds` | float | |
| `metrics.prompt_tokens_min` / `_median` / `_max` | int / float / int | |
| `metrics.peak_rss_bytes` | int | |
| `gate.passed` | bool | |
| `gate.checks` | object (check name → bool) | |
| `gate.latency_threshold_seconds` | float | |
| `results[].id` | string | fixture id |
| `results[].expected` / `.actual` | string \| null | `"idle"`/`"act"`/`"escalate"`; `actual` is `null` on a parse fallback |
| `results[].correct` / `.parse_fallback` | bool | |
| `results[].raw_output` | string | the model's raw reply — **unbounded length**, one run measured 52 KB for a single fixture |
| `results[].latency_seconds` | float | |
| `results[].prompt_tokens` | int | |
| `results[].tags` | string[] | sorted |

### 2. Soak run (`kind=soak/`)

Five files per run, per `docs/heart/soak.md`. The daemon boots with
`ANSINA_SECURITY__ENABLED=false` against a scratch database — these are
measurement-rig artifacts, not production traffic.

- **`samples.jsonl`** — one JSON object per line, appended every
  `ANSINA_SOAK_SAMPLE_INTERVAL` seconds (default 60s) by `scripts/heart-soak-run.sh`:

  | Field | Type | Notes |
  |---|---|---|
  | `t` | float (unix timestamp) | |
  | `elapsed_s` | int | seconds since soak start |
  | `rss_kib` | int | **instantaneous** reading, via `ps -o rss=` against the real MLX/python process — see the telemetry-sample divergence note below |
  | `ticks` | int | |
  | `paused` | bool | |
  | `paused_reason` | string \| null | |
  | `last_decision` | string \| null | `"idle"`/`"act"`/`"escalate"` |
  | `last_duration_seconds` | float \| null | |

  **No circuit-breaker counters** — this script reads `GET /heart/tick`, which
  deliberately doesn't expose `failures_total`/`consecutive_failures`/
  `consecutive_overruns` (see the telemetry sample below, which does).

- **`run.log`** — the daemon's own structured log output, tee'd to a file. Same
  line shape as "mirrored log line" (family 4) — see that section rather than a
  duplicate description here.
- **`journal.json`** — the verbatim response body of `GET /heart/journal?limit=500`
  at the end of the run (`JournalPage`, see family 2's own route below — this *is*
  that same shape, just one captured page of it).
- **`soak-<date>.md`** — the rendered report (`scripts/heart_soak_report.py`):
  prose sections (RSS, tick duration, scheduling drift, decision distribution,
  circuit breaker, journal cross-check, non-idle entries) — not independently
  schema'd.
- **`soak-<date>.json`** — a small metadata summary alongside the `.md` (not the
  full report): `{"metadata": {...string keys parsed from run.log, e.g.
  "interval_seconds"/"jitter_seconds"/"branch"/"commit"}, "sample_count": int,
  "tick_log_line_count": int, "journal_entry_count": int}`.

### 3. Telemetry sample (`kind=telemetry/`)

One JSON object per line, written by `ansina.telemetry.sampler.TelemetrySample`
every `[telemetry] sample_interval_seconds` (default 60s) whenever `[telemetry]
enabled = true`, shipped to the bucket by the Vector sidecar (issue #62).

| Field | Type | Notes |
|---|---|---|
| `t` | float (unix timestamp) | |
| `elapsed_s` | int | seconds since the sampler started |
| `rss_kib` | int \| null | **peak** (`ru_maxrss`), not instantaneous — see divergence note below. `null` only if the read itself failed |
| `ticks` | int \| null | `null` whenever `[heart]`/`[heart.tick]` are disabled |
| `paused` | bool \| null | |
| `paused_reason` | string \| null | |
| `last_decision` | string \| null | `"idle"`/`"act"`/`"escalate"` |
| `last_duration_seconds` | float \| null | |
| `failures_total` | int \| null | cumulative since tick loop start |
| `consecutive_failures` | int \| null | the circuit breaker's own live counter |
| `consecutive_overruns` | int \| null | the circuit breaker's own live counter |

**This is a different, superset shape from soak's `samples.jsonl` above — do not
treat the two as the same schema despite the near-identical field names**:

- This family adds `failures_total`/`consecutive_failures`/`consecutive_overruns`
  (not present in soak's `samples.jsonl` at all), since `GET /heart/tick` — what the
  soak script reads — doesn't expose them, while this sampler reads the tick loop's
  own `TickController` in-process.
- `rss_kib` here is **peak** RSS (`resource.getrusage(RUSAGE_SELF).ru_maxrss`,
  monotonically non-decreasing for the process's whole lifetime), not the soak
  script's **instantaneous** `ps`-based reading. The two are not bit-comparable
  sample-for-sample — a flattening peak-RSS curve and a flat instantaneous-RSS curve
  both read as "no leak," but a single sample from each source cannot be diffed
  against the other. See `ansina.telemetry.sampler`'s own module docstring for the
  full reasoning.
- Every field here is nullable whenever `[heart]`/`[heart.tick]` are disabled
  (sampling runs independently of the Heart) — soak's `samples.jsonl` has no such
  case, since the soak harness always boots with the Heart enabled.

### 4. Mirrored log line (`kind=runlog/`)

One JSON object per line — `ansina.logging.formatter.JsonFormatter`'s output,
mirrored through `ansina.telemetry.log_mirror.TelemetryLogHandler` to a rotated
file and shipped by Vector. **Identical in shape to the daemon's own primary log
stream** (the same formatter instance backs both handlers) and to `run.log`'s own
lines in the soak family above.

| Field | Type | Notes |
|---|---|---|
| `timestamp` | string (ISO 8601, UTC) | |
| `level` | string | `"INFO"`/`"WARNING"`/`"ERROR"`/... |
| `logger` | string | the logger name, e.g. `"ansina.heart.tick.loop"` |
| `message` | string | redacted |
| `request_id` | string | present only inside an HTTP request's correlation scope |
| `extra` | object | present only when the log call passed `extra={...}`; every string leaf (recursively) is redacted |
| `exception` | string | present only on an exception log, redacted |
| `stack` | string | present only when stack info was captured, redacted |

## `heart/eval`'s continued authority

`ansina.heart.eval` remains the **authoritative in-repo harness** for the
tick-decision duty, full stop. It renders every fixture through the actually-shipped
`heart.tick.snapshot.build_prompt` and `heart.tick.decision.try_parse_decision` —
the exact code path a real tick runs. Any external evaluation framework sitting on
top of this corpus would necessarily re-implement both of those, reintroducing
precisely the kind of drift this codebase structurally avoids everywhere else (the
same reasoning `auth.policy`'s `resources.verbs` deriving from `route.methods`,
rather than being hand-maintained, already embodies). Nothing recommended below is a
replacement for `heart/eval` — each operates only on *finished* results this harness
already produced.

## Analysis-layer tooling

Whether an existing OSS framework should sit on top of this corpus, or something
purpose-built, was evaluated — every tool this issue's own scope named was assessed
on its merits. **The evaluation's recommendation and reasoning are maintained
privately, not published in this document.** `ansina` is published as an open,
public, MIT-licensed repository — anyone may run the daemon itself for free — but
the *analysis* layer built on top of the corpus it produces is where recovering the
cost of building and operating this is possible at all; publishing that evaluation
here would hand it, for free, to anyone reading this repo, competitor or not. What
stays public here, and must, is the data contract itself (above) — any analysis
layer, in-house or OSS, needs to read the same schema regardless of which one is
actually chosen.

This redaction is itself a decision worth recording plainly rather than leaving
unexplained: a future editor of this document should not "restore" the missing
survey table by re-deriving one from the committed git history or from the GitHub
issue that requested it — the omission here is deliberate, not an oversight.

## ML-readiness caveat

As of this milestone, the whole corpus is:

- **One 8-hour soak run** (issue #60): 960 ticks, **100% `idle`** — zero `act`, zero
  `escalate`, zero circuit-breaker trips.
- **~24 bench reports** (issues #53/#54's model-ladder and prompt-variant bake-offs):
  a few hundred labelled fixture rows total.

That supports:

- **Regression detection** — did a model/prompt-variant change measurably move
  accuracy or recall.
- **Prompt/model A-B comparison at scale** — what issue #53's ladder did by hand,
  done systematically once there's enough history.
- **Operational anomaly detection** — RSS/latency/scheduling drift against the
  soak's own established baseline.

It does **not** support learning a better tick-decision policy: there is no
non-idle ground truth anywhere in the live (non-fixture) corpus. Two concrete things
change that, neither built by this issue — named as the path forward, not built:

1. **Fault-injection scenarios** — the same shape issue #54's `self_state_fault`-
   tagged fixtures already use, producing labelled non-idle samples at scale rather
   than relying on a real fault to occur during a live run.
2. **Once issue #64 lands** (`escalate` → `BrainProvider.stream()`), the Brain's own
   response to an escalated tick becomes a supervisory label for that tick's
   decision — real, not fixture, non-idle ground truth.
