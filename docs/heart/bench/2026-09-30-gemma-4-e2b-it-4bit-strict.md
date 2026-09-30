# Heart bench: mlx-community/gemma-4-e2b-it-4bit / strict

- Generated: 2026-09-30T13:12:34.951910+00:00
- Host: macOS-26.6.2-arm64-arm-64bit-Mach-O
- mlx-lm: 0.31.3
- Chat template applied: True
- Max output tokens: 512
- Fixtures: 36

## Gate: PASS

| Check | Result |
|---|---|
| accuracy >= 0.90 | ✅ pass |
| zero false act/escalate on obviously_idle fixtures | ✅ pass |
| zero parse-fallback rate | ✅ pass |
| p95 latency <= 6.00s (20% of 30.0s interval) | ✅ pass |

## Metrics

| Metric | Value |
|---|---|
| Overall accuracy | 97.22% |
| Parse-fallback rate | 0.00% |
| False act/escalate on obviously-idle | 0 |
| p50 latency | 0.219s |
| p95 latency | 0.235s |
| Prompt tokens (min/median/max) | 128 / 138 / 186 |
| Peak RSS | 3.57 GiB |

## Per-class recall

| Class | Recall | Count |
|---|---|---|
| idle | 100.00% | 12 |
| act | 90.00% | 10 |
| escalate | 100.00% | 14 |

## Recall by tag

| Tag | Recall | Count |
|---|---|---|
| obviously_idle | 100.00% | 6 |
| self_state | 91.67% | 12 |
| self_state_fault | 87.50% | 8 |

## Per-fixture results

| id | expected | actual | correct | latency (s) | tokens |
|---|---|---|---|---|---|
| idle-01 | idle | idle | ✅ | 0.250 | 128 |
| idle-02 | idle | idle | ✅ | 0.209 | 134 |
| idle-03 | idle | idle | ✅ | 0.205 | 130 |
| idle-04 | idle | idle | ✅ | 0.210 | 133 |
| idle-05 | idle | idle | ✅ | 0.205 | 128 |
| idle-06 | idle | idle | ✅ | 0.206 | 135 |
| idle-07 | idle | idle | ✅ | 0.207 | 132 |
| idle-08 | idle | idle | ✅ | 0.207 | 130 |
| act-01 | act | act | ✅ | 0.209 | 137 |
| act-02 | act | act | ✅ | 0.205 | 133 |
| act-03 | act | act | ✅ | 0.214 | 140 |
| act-04 | act | act | ✅ | 0.207 | 138 |
| act-05 | act | act | ✅ | 0.205 | 137 |
| act-06 | act | act | ✅ | 0.208 | 132 |
| act-07 | act | act | ✅ | 0.205 | 133 |
| act-08 | act | act | ✅ | 0.205 | 135 |
| escalate-01 | escalate | escalate | ✅ | 0.222 | 140 |
| escalate-02 | escalate | escalate | ✅ | 0.224 | 146 |
| escalate-03 | escalate | escalate | ✅ | 0.219 | 136 |
| escalate-04 | escalate | escalate | ✅ | 0.220 | 137 |
| escalate-05 | escalate | escalate | ✅ | 0.220 | 132 |
| escalate-06 | escalate | escalate | ✅ | 0.222 | 138 |
| escalate-07 | escalate | escalate | ✅ | 0.224 | 142 |
| escalate-08 | escalate | escalate | ✅ | 0.220 | 137 |
| self-idle-01 | idle | idle | ✅ | 0.218 | 167 |
| self-idle-02 | idle | idle | ✅ | 0.218 | 179 |
| self-idle-03 | idle | idle | ✅ | 0.222 | 182 |
| self-idle-04 | idle | idle | ✅ | 0.221 | 179 |
| self-act-01 | act | act | ✅ | 0.218 | 179 |
| self-act-02 | act | escalate | ❌ | 0.234 | 179 |
| self-esc-01 | escalate | escalate | ✅ | 0.233 | 175 |
| self-esc-02 | escalate | escalate | ✅ | 0.235 | 183 |
| self-esc-03 | escalate | escalate | ✅ | 0.232 | 180 |
| self-esc-04 | escalate | escalate | ✅ | 0.232 | 176 |
| self-esc-05 | escalate | escalate | ✅ | 0.233 | 177 |
| self-esc-06 | escalate | escalate | ✅ | 0.250 | 186 |
