# Heart bench: mlx-community/gemma-4-e2b-it-4bit / strict

- Generated: 2026-09-30T12:37:05.320431+00:00
- Host: macOS-26.6.2-arm64-arm-64bit-Mach-O
- mlx-lm: 0.31.3
- Chat template applied: True
- Max output tokens: 512
- Fixtures: 36

## Gate: FAIL

| Check | Result |
|---|---|
| accuracy >= 0.90 | ❌ fail |
| zero false act/escalate on obviously_idle fixtures | ✅ pass |
| zero parse-fallback rate | ✅ pass |
| p95 latency <= 6.00s (20% of 30.0s interval) | ✅ pass |

## Metrics

| Metric | Value |
|---|---|
| Overall accuracy | 80.56% |
| Parse-fallback rate | 0.00% |
| False act/escalate on obviously-idle | 0 |
| p50 latency | 0.209s |
| p95 latency | 0.224s |
| Prompt tokens (min/median/max) | 88 / 98 / 154 |
| Peak RSS | 3.57 GiB |

## Per-class recall

| Class | Recall | Count |
|---|---|---|
| idle | 91.67% | 12 |
| act | 58.33% | 12 |
| escalate | 91.67% | 12 |

## Recall by tag

| Tag | Recall | Count |
|---|---|---|
| obviously_idle | 100.00% | 6 |
| self_state | 50.00% | 12 |
| self_state_fault | 37.50% | 8 |

## Per-fixture results

| id | expected | actual | correct | latency (s) | tokens |
|---|---|---|---|---|---|
| idle-01 | idle | idle | ✅ | 0.232 | 88 |
| idle-02 | idle | idle | ✅ | 0.193 | 94 |
| idle-03 | idle | idle | ✅ | 0.192 | 90 |
| idle-04 | idle | idle | ✅ | 0.193 | 93 |
| idle-05 | idle | idle | ✅ | 0.184 | 88 |
| idle-06 | idle | idle | ✅ | 0.196 | 95 |
| idle-07 | idle | idle | ✅ | 0.197 | 92 |
| idle-08 | idle | idle | ✅ | 0.195 | 90 |
| act-01 | act | act | ✅ | 0.194 | 97 |
| act-02 | act | escalate | ❌ | 0.210 | 93 |
| act-03 | act | act | ✅ | 0.200 | 100 |
| act-04 | act | act | ✅ | 0.204 | 98 |
| act-05 | act | act | ✅ | 0.199 | 97 |
| act-06 | act | act | ✅ | 0.199 | 92 |
| act-07 | act | act | ✅ | 0.199 | 93 |
| act-08 | act | act | ✅ | 0.197 | 95 |
| escalate-01 | escalate | escalate | ✅ | 0.215 | 100 |
| escalate-02 | escalate | escalate | ✅ | 0.217 | 106 |
| escalate-03 | escalate | escalate | ✅ | 0.215 | 96 |
| escalate-04 | escalate | escalate | ✅ | 0.211 | 97 |
| escalate-05 | escalate | escalate | ✅ | 0.212 | 92 |
| escalate-06 | escalate | escalate | ✅ | 0.211 | 98 |
| escalate-07 | escalate | escalate | ✅ | 0.211 | 102 |
| escalate-08 | escalate | escalate | ✅ | 0.209 | 97 |
| self-idle-01 | idle | idle | ✅ | 0.207 | 127 |
| self-idle-02 | idle | idle | ✅ | 0.210 | 139 |
| self-idle-03 | idle | idle | ✅ | 0.206 | 142 |
| self-idle-04 | idle | act | ❌ | 0.206 | 139 |
| self-act-01 | act | escalate | ❌ | 0.294 | 137 |
| self-act-02 | act | escalate | ❌ | 0.224 | 136 |
| self-act-03 | act | escalate | ❌ | 0.220 | 150 |
| self-act-04 | act | idle | ❌ | 0.206 | 151 |
| self-esc-01 | escalate | escalate | ✅ | 0.220 | 137 |
| self-esc-02 | escalate | act | ❌ | 0.216 | 154 |
| self-esc-03 | escalate | escalate | ✅ | 0.222 | 151 |
| self-esc-04 | escalate | escalate | ✅ | 0.224 | 151 |
