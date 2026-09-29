# Heart bench: mlx-community/gemma-4-e2b-it-4bit / strict

- Generated: 2026-09-29T13:59:42.388920+00:00
- Host: macOS-26.6.2-arm64-arm-64bit-Mach-O
- mlx-lm: 0.31.3
- Chat template applied: True
- Max output tokens: 512
- Fixtures: 24

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
| Overall accuracy | 95.83% |
| Parse-fallback rate | 0.00% |
| False act/escalate on obviously-idle | 0 |
| p50 latency | 0.202s |
| p95 latency | 0.260s |
| Prompt tokens (min/median/max) | 88 / 95 / 106 |
| Peak RSS | 5.92 GiB |

## Per-class recall

| Class | Recall | Count |
|---|---|---|
| idle | 100.00% | 8 |
| act | 87.50% | 8 |
| escalate | 100.00% | 8 |

## Per-fixture results

| id | expected | actual | correct | latency (s) | tokens |
|---|---|---|---|---|---|
| idle-01 | idle | idle | ✅ | 1.148 | 88 |
| idle-02 | idle | idle | ✅ | 0.193 | 94 |
| idle-03 | idle | idle | ✅ | 0.194 | 90 |
| idle-04 | idle | idle | ✅ | 0.197 | 93 |
| idle-05 | idle | idle | ✅ | 0.185 | 88 |
| idle-06 | idle | idle | ✅ | 0.200 | 95 |
| idle-07 | idle | idle | ✅ | 0.195 | 92 |
| idle-08 | idle | idle | ✅ | 0.197 | 90 |
| act-01 | act | act | ✅ | 0.260 | 97 |
| act-02 | act | escalate | ❌ | 0.218 | 93 |
| act-03 | act | act | ✅ | 0.195 | 100 |
| act-04 | act | act | ✅ | 0.198 | 98 |
| act-05 | act | act | ✅ | 0.196 | 97 |
| act-06 | act | act | ✅ | 0.196 | 92 |
| act-07 | act | act | ✅ | 0.202 | 93 |
| act-08 | act | act | ✅ | 0.201 | 95 |
| escalate-01 | escalate | escalate | ✅ | 0.210 | 100 |
| escalate-02 | escalate | escalate | ✅ | 0.211 | 106 |
| escalate-03 | escalate | escalate | ✅ | 0.211 | 96 |
| escalate-04 | escalate | escalate | ✅ | 0.215 | 97 |
| escalate-05 | escalate | escalate | ✅ | 0.210 | 92 |
| escalate-06 | escalate | escalate | ✅ | 0.211 | 98 |
| escalate-07 | escalate | escalate | ✅ | 0.213 | 102 |
| escalate-08 | escalate | escalate | ✅ | 0.211 | 97 |
