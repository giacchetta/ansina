# M6 findings: Heartbeat — First Real Beat

Issue #56's write-up: what the Heart actually did across #53 (bench harness + model
ladder + prompt variants), #54 (daemon self-state + circuit breaker), #55
(`heart_journal`), and the multi-hour soak this issue itself runs. This is the
milestone's "learn" step and M7's sole input — not a status report, a set of
evidence-based verdicts.

**Status:** the model/prompt-variant/Backlog #15 sections below are final, drawn from
the committed bench evidence across #53/#54. The escalate-rate and escalate→Brain
sections are placeholders pending the soak itself (`docs/heart/soak.md`) — see the
note at the top of each.

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
`docs/heart/bench/2026-09-30-gemma-4-e2b-it-4bit-strict-preimprovement.{md,json}`
(gitignored as of #58, kept on this machine; the numbers are reproduced here since
the file itself isn't committed). The fix kept `"strict"`'s original capability-based
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

## Measured escalate rate and genuine-vs-noise review

**Pending the soak** — see `docs/heart/soak.md` for the procedure. This section is
filled from the rendered `docs/heart/soak/soak-<date>.md` report's "Decision
distribution" and "Non-idle journal entries" sections once a multi-hour run
completes. Placeholder table to fill:

| Decision | Count | % | Genuine (manual review) | Noise (manual review) |
|---|---|---|---|---|
| idle | — | — | — | — |
| act | — | — | — | — |
| escalate | — | — | — | — |

Open questions this section needs to answer once filled:

- Did any `escalate` fire during a healthy steady state (no injected fault, no
  observed daemon/database issue)? If so, on what triggering condition (per the
  journal `note`)?
- Does the soak's escalate rate roughly match the fixture-measured `self_state_fault`
  recall (87.5%), or diverge — and if it diverges, in which direction?
- Were any `escalate` decisions a repeat of the identical condition within the soak
  window (the "recurring" half of `"strict"`'s counting rule), suggesting a
  dedup/cooldown gap rather than a correct decision firing repeatedly?

## Verdict: wiring `escalate` → `BrainProvider.stream()`

**Pending the soak** — this section's verdict depends directly on the escalate-rate
review above. Placeholder: **yes / no / not-yet** (circle one once filled), with the
reasoning that led there. Considerations already in view ahead of the soak's own
numbers:

- `BrainProvider.stream()` (issue #12) already exists and is wired into
  `create_app`'s lifespan independently of the Heart — the *port* is not the open
  question, whether a tick's `escalate` should call it unconditionally, every time, is.
- No dedup/cooldown mechanism exists yet (explicitly out of this milestone's scope,
  per the issue text) — if the soak shows `escalate` firing repeatedly for the
  identical recurring condition, wiring it straight to the Brain today would mean one
  real-world fault produces N Brain calls, not one. That would argue for **not-yet**:
  a dedup/cooldown rung first, then wiring.
- If the soak's escalate rate over a healthy multi-hour run is at or near zero (no
  false escalates, no noisy repeats), that argues for **yes**: the signal is clean
  enough that today's shape (fire on every `escalate` decision) is already safe to
  wire.

## Open questions for M7

1. (From the escalate-rate review above) Does `escalate` need a dedup/cooldown rung
   before it can safely reach `BrainProvider.stream()`, or is the current one-shot
   signal already clean enough?
2. `GET /heart/tick` does not currently expose the circuit breaker's own counters
   (`failures_total`/`consecutive_failures`/`consecutive_overruns`) — the soak report
   reads them indirectly via `paused`/`paused_reason` plus the daemon's own log.
   Worth a small API addition if an operator ever needs to watch these without log
   access, though nothing in M6 required it.
3. `RecentJournalSource`'s short-term-memory feed (issue #55) has not yet been
   evaluated for whether it measurably changes decision quality one way or the other
   on the Mac Mini — #55's own scope note flagged this as a possible follow-up rather
   than something this milestone re-opens.
4. The portable, non-Apple-Silicon `HeartRuntime` adapter remains deferred (no
   non-Apple hardware with a viable GPU was available during M1 or since) — still
   worth flagging as open since M6's whole bench/soak evidence base is MLX-only and
   would need to be re-established for any future adapter.
