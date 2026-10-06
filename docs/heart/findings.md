# M6 findings: Heartbeat — First Real Beat

Issue #56's write-up: what the Heart actually did across #53 (bench harness + model
ladder + prompt variants), #54 (daemon self-state + circuit breaker), #55
(`heart_journal`), and the multi-hour soak this issue itself runs. This is the
milestone's "learn" step and M7's sole input — not a status report, a set of
evidence-based verdicts.

**Status:** every section below is now final. The model/prompt-variant/Backlog #15
sections are drawn from the committed bench evidence across #53/#54. The soak,
escalate-rate, and escalate→Brain sections are drawn from the 8-hour unattended run
completed on the Mac Mini M4 on 2026-10-02 (issue #60) — the rendered report lives at
`docs/heart/soak/soak-2026-10-02.md` (gitignored, local to the machine that fetched it,
per `docs/heart/soak.md`); the raw inputs it was rendered from (`run.log`,
`samples.jsonl`, `journal.json`) are kept alongside it under
`docs/heart/soak/2026-10-02-raw/`.

## Chosen model: `mlx-community/gemma-4-e2b-it-4bit`

Issue #53's bench ladder, smallest-first, each rung benched for real on the Mac Mini
M4 against `heart.tick.prompts.DEFAULT_PROMPT_VARIANT` ("strict", see below) and the
gate (accuracy >= 0.90, zero false act/escalate on the `obviously_idle` fixture
subset, zero parse-fallback rate, p95 latency <= 20% of `interval_seconds`):

| Rung | Model | Accuracy | Gate |
|---|---|---|---|
| 1 | `Qwen3.5-2B-MLX-4bit` | 75.0% | FAIL |
| 2 | `Qwen3.5-4B-MLX-4bit` | 83.3% | FAIL |
| 3 | `gemma-4-e2b-it-4bit` | 95.8% | **PASS** |

The ladder stopped at rung 3 per #53's own "stop at the first rung that clears" rule
— rungs 4-5 (`gemma-4-e4b-it-4bit`, `gemma-4-e4b-it-8bit`) were never benched. A
planning-time correction is worth recording: the originally-proposed
`gemma-4-e4b-8bit` turned out to be the *base*, not instruction-tuned, checkpoint
(`-it-` missing from the repo id) at 7.94B stored parameters, not the ~4B "E4B"
effective size suggested — the instruction-tuned repos live under `-it-4bit`/
`-it-8bit` instead, which is what the ladder above actually benched.

On the widened 36-fixture set (issue #54 added 12 `self_state`-tagged fixtures),
`gemma-4-e2b-it-4bit` measures **97.2% accuracy**, reproduced across three
independent runs on two separate days (2026-09-30, 2026-10-01 x2) with the identical
single miss each time: `self-act-02` (`act` called `escalate` — the safe direction,
not a false-idle). Peak RSS measured 3.57 GiB across all three runs, comfortably
inside the Mac Mini M4's 16 GB unified memory — see
`docs/heart/mac-mini-m4.toml` for the full validated profile.

## Chosen prompt variant: `"strict"`

Two adapter fixes had to land before any prompt variant could be meaningfully
compared, both measured on the Mac Mini rather than assumed:

1. **No chat template** (the pre-#53 adapter behavior): 0% accuracy, 100%
   parse-fallback — the model continued the raw prompt as free text instead of
   answering it.
2. **Chat template applied, reasoning left on**: still 0% accuracy — 18/24 fixtures
   never reached a final answer within `max_output_tokens`, and every one that did
   still blew the latency gate (p50 ~6.2s against a ~6.0s ceiling).

With both the chat template and `enable_thinking=False` applied, a 3-variant bake-off
on the smallest rung (`Qwen3.5-2B-MLX-4bit`) picked `"strict"`:

| Variant | Accuracy |
|---|---|
| `baseline` | 62.5% |
| `fewshot` | 50.0% |
| `strict` | **75.0%** |

`"strict"` then cleared the gate at rung 3 (95.8%, see above) and became
`heart.tick.prompts.DEFAULT_PROMPT_VARIANT` and `HeartSettings.model_repo`'s paired
default.

Issue #54's own real-hardware run (the 36-fixture, `self_state`-tagged set) found a
second, genuine prompt weakness, not an adapter bug: `_STRICT`'s original act/
escalate rule had no severity heuristic — it left the model no way to weigh severity
once #54 gave it real, nameable fault conditions to judge, and it broke toward
`escalate` on several single, routine issues. Before the fix: 80.56% accuracy (gate
FAIL), driven by weak `self_state` (50.0%) and `self_state_fault` (37.5%) recall —
see `docs/heart/soak.md`'s sibling evidence,
`2026-09-30-gemma-4-e2b-it-4bit-strict-preimprovement.{md,json}` (gitignored as of
#58, and as of #59 published to the report bucket at
`kind=bench/dt=2026-09-30/2026-09-30-gemma-4-e2b-it-4bit-strict-preimprovement.
{md,json}` once `make heart-bench-publish` has run; the numbers are reproduced here
regardless, since neither the local file nor the bucket is guaranteed present in
every clone). The fix kept `"strict"`'s original capability-based
clause and added an explicit counting rule: one routine problem is `act`, two or more
simultaneous problems (or one that keeps recurring despite retries) is `escalate`.
Re-measured at 97.2% accuracy, gate PASS, reproduced three times (see "Chosen model"
above).

## Parse-fallback, note quality, and the Backlog #15 verdict

**Parse-fallback rate is 0.00%** on every gate-passing run since the chat-template +
`enable_thinking=False` fixes landed — three independent 36-fixture runs, zero
unparseable replies in any of them. `heart.tick.decision.try_parse_decision`'s free-
text parsing (one word — idle/act/escalate — stripped of any trailing `</think>`
content) has not failed once under real-hardware measurement since the adapter fixes
landed.

The journal `note` (`heart.tick.journal_handler._build_note`) is not a model-output
quality question at all under the current design: it is **code-generated** from the
triggering snapshot items (the highest-priority band of `TickPrompt.items`, rendered
`"<source> reported: <text>"`), deliberately never the model's own raw reply — see
issue #55's own scope line. There is no model-authored text to judge the quality of.

**Verdict: No, not justified by current evidence.** Backlog #15's own hypothesis is
about a different set of duties entirely — session titles, intent/slot extraction,
tool-argument drafting under grammar-constrained decoding, explicitly out of the tick
loop's scope (#11's non-goal) — and M6 has not tested any of them; nothing here
speaks to #15's own accept/reject metric (schema-valid output rate, agreement with
the Brain). What M6 *does* speak to is the narrower question issue #56 actually asks:
does the tick-decision duty's own parse-fallback rate or note-authorship shape now
make a case for constrained decoding on *this* duty. It does not — 0% parse-fallback
with plain free-text parsing is itself evidence against urgency here, and the note is
already policy-constrained by construction (code-generated, not model-authored), so
there is nothing for constrained decoding to improve on this duty specifically. #15
stays parked.

## The 8-hour unattended soak (2026-10-02)

Issue #56 built the soak tooling and started the run; issue #60 fetched, rendered, and
evaluates it. Run envelope (from `docs/heart/soak/soak-2026-10-02.md`/`.json`):
commit `bc1163c` on branch `m6-heartbeat`, `mlx-community/gemma-4-e2b-it-4bit`,
prompt variant `strict`, `interval_seconds=30`/`jitter_seconds=3.0`, 8.01 h observed
(480 samples @ 60s), **960 real ticks**. Per `docs/heart/soak.md`'s own "what this is
not" note: the harness boots the real daemon with `ANSINA_SECURITY__ENABLED=false`
against a scratch SQLite database on loopback — a measurement rig, not a deployment
recipe (`docs/heart/mac-mini-m4.toml` is that).

| Section | Measured | Verdict (per `docs/heart/soak.md`'s own pass/fail criteria) |
|---|---|---|
| RSS | first 2.921 → last 2.958 GiB; slope **+1278.4 KiB/hour**; Q1 mean 2.950 vs Q4 mean 2.957 GiB | **PASS** — +1278 KiB/h against a ~2.92 GiB resident baseline extrapolates to ~30 MiB/day; flat, not a climb |
| Tick duration | overall p50 0.326s / p95 0.334s / max 0.341s; per-hour p95 0.334/0.333/0.332/0.334/0.335/0.333/0.335/0.334s (hours 0–7) | **PASS** — hour 7's p95 (0.334s) is indistinguishable from hour 0's (0.334s); no hour trends slower |
| Scheduling drift | expected ~31.5s; mean 30.0s, p95 32.0s, max 33.0s | **PASS** — 0 of 959 gaps exceeded 1.5x expected |
| Circuit breaker | 0 trips, 0 paused samples | **PASS** — stated explicitly: no pause episode occurred during the run |
| Journal cross-check (re-run at soak scale, issue #60) | 960 `"heart tick completed"` log lines; 500 `GET /heart/journal` rows fetched; 0 mismatches | **PASS** — every fetched row matches its log line exactly (`scripts/heart_journal_smoke_verify.py`, re-run against `docs/heart/soak/2026-10-02-raw/`) |

**Page-cap caveat, restated per #56's own note:** `GET /heart/journal`'s 500-row
`_MAX_LIMIT` (`api/routes/heart_journal.py`) means the journal-backed cross-check above
covers only the most recent ~4 hours of this 8-hour run, not all 960 ticks. The full-run
decision denominator below instead comes directly from the daemon's own `run.log`
(`"heart tick completed"` lines), which has no such cap.

## Measured escalate rate and genuine-vs-noise review

| Decision | Journal page (500 rows) | % | Full run (`run.log`, 960 ticks) | % | Genuine | Noise |
|---|---|---|---|---|---|---|
| idle | 500 | 100.0% | 960 | 100.0% | — | — |
| act | 0 | 0.0% | 0 | 0.0% | 0 | 0 |
| escalate | 0 | 0.0% | 0 | 0.0% | 0 | 0 |

This section's three open questions, answered:

- **Did any `escalate` fire during a healthy steady state?** No — zero across all 960
  ticks, both in the fetched journal page and the full `run.log` count. The
  genuine-vs-noise review is therefore empty by construction: there are no non-idle
  rows to classify. `docs/heart/soak/soak-2026-10-02.md`'s own "Non-idle journal
  entries (0)" section agrees.
- **Does the rate match the fixture-measured `self_state_fault` recall (87.5%)?** The
  comparison doesn't apply in the direction the question anticipated:
  `self_state_fault` recall measures behavior on *injected* fault fixtures, and this
  soak injected none — every tick's `DaemonStateSource` reported a healthy daemon
  (readiness green, database healthy, zero consecutive failures/overruns) for the
  entire run, confirmed by zero circuit-breaker trips and exactly one `WARNING` line in
  1,965 log lines (the expected `security.enabled = false` dev-mode banner logged once
  at boot). The comparable fixture figure for a healthy run is the `obviously_idle`
  subset's own gate clause (100% recall, zero false `act`/`escalate`); 960/960 `idle`
  is consistent with it at roughly 27x the fixture count, with no divergence in the
  false-positive direction. The true-positive (`self_state_fault`) direction remains
  untested by this soak — see the verdict's honest limit below.
- **Any recurring-condition repeats suggesting a dedup gap?** None observable — with
  zero non-idle decisions there are no repeats to inspect. Recorded as *unmeasured*,
  not as proven absent.

## Verdict: wiring `escalate` → `BrainProvider.stream()`

**Yes — safe to wire as-is; no dedup/cooldown rung first.** This is the "yes" branch
the placeholder itself named for this evidence shape: a healthy multi-hour run with an
escalate rate at zero, no false escalates, no noisy repeats.

Reasoning:

- Zero false `escalate` over 960 real ticks / 8.01 h on the actual target hardware, in
  exactly the healthy steady state the original placeholder's "yes" condition
  described.
- A hard upper bound on call volume exists independently of the model's own behavior:
  `TickLoop` is overlap-guarded (an in-flight flag prevents concurrent ticks) and one
  tick yields at most one decision, so the 30s cadence caps `BrainProvider.stream()` at
  ≤120 calls/hour even in a pathological all-`escalate` run. The measured rate here is
  0.
- The failure mode a dedup/cooldown rung would most plausibly guard against — a fault
  recurring tick after tick, each one independently re-escalating — already has a
  backstop in #54's circuit breaker: `max_consecutive_failures` auto-pauses the loop
  entirely (`pause_reason` naming the trip), which bounds the worst case regardless of
  whether a cooldown exists on top.
- `BrainProvider.stream()` (issue #12) already exists and is wired into `create_app`'s
  lifespan independently of the Heart — the *port* was never the open question; whether
  a tick's `escalate` should call it unconditionally, every time, was, and this soak's
  zero-false-positive result over 8 hours answers that for the signal as currently
  shaped.

**Honest limit, stated plainly, not hidden:** 960/960 ticks were `idle` — this soak
exercised only the idle path. The verdict above rests on *zero false positives over 8
hours*, not on any observed `escalate` behaving well; there is still no real
`act`/`escalate` ground truth in this corpus. The only evidence that `escalate` fires
*correctly* when a genuine fault exists remains #54's synthetic 36-fixture
`self_state_fault` set (87.5% recall, one miss in the safe direction) — a fixture
snapshot, not a live run. This is also the caveat the corpus-contract/ML-readiness
work (issue #63) needs for its own assessment. Hand-off for issue #64: wire it, and
keep dedup/cooldown as a named follow-up to revisit once real non-idle production data
exists — not a prerequisite to wiring.

## Open questions for M7

1. **RESOLVED (issue #60).** Does `escalate` need a dedup/cooldown rung before it can
   safely reach `BrainProvider.stream()`, or is the current one-shot signal already
   clean enough? **No dedup/cooldown rung needed before wiring** — the 2026-10-02 soak
   measured a zero escalate rate (0 false positives) over 960 real ticks / 8.01 h, with
   a hard upper bound on call volume already enforced by the tick cadence and #54's
   circuit breaker regardless. See "Verdict: wiring `escalate` →
   `BrainProvider.stream()`" above for the full reasoning and its honest limit (only
   the idle path was exercised; no real `act`/`escalate` ground truth exists yet).
2. (New, issue #60) No soak to date has exercised a real `act` or `escalate` decision
   — all evidence that the decision fires *correctly* under a genuine fault is
   fixture-based (#54's 36-fixture set), not observed in a live multi-hour run. Once
   #64 wires `escalate` → `BrainProvider.stream()`, a future soak (or production
   telemetry, once #61/#62 land) that actually observes non-idle decisions would close
   this gap and let the genuine-vs-noise review above be re-run against real data.
3. `GET /heart/tick` does not currently expose the circuit breaker's own counters
   (`failures_total`/`consecutive_failures`/`consecutive_overruns`) — the soak report
   reads them indirectly via `paused`/`paused_reason` plus the daemon's own log.
   Worth a small API addition if an operator ever needs to watch these without log
   access, though nothing in M6 required it.
4. `RecentJournalSource`'s short-term-memory feed (issue #55) has not yet been
   evaluated for whether it measurably changes decision quality one way or the other
   on the Mac Mini — #55's own scope note flagged this as a possible follow-up rather
   than something this milestone re-opens.
5. The portable, non-Apple-Silicon `HeartRuntime` adapter remains deferred (no
   non-Apple hardware with a viable GPU was available during M1 or since) — still
   worth flagging as open since M6's whole bench/soak evidence base is MLX-only and
   would need to be re-established for any future adapter.
