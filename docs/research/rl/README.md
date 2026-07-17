# Reinforcement Learning Research

These documents preserve the sequence of RL investigations. They do not
describe an approved production policy.

| Document | Status | Purpose |
| --- | --- | --- |
| `RL_PROFITABILITY_RECOVERY_PLAN.md` | Current gate definition | Requires causal, cost-adjusted alpha before PPO resumes. |
| `RL_PROFITABILITY_RECOVERY_RESULTS.md` | Current finding | Records that the tested feature set did not pass the alpha gate. |
| `RL_STRATEGY_V2_REPORT.md` | Historical rejection | Rejects the 2026-07-12 candidate set. |
| `RL_PPO_NEXT_OPEN_V2_REPORT.md` | Superseded | Retained because its timing audit explains invalid old evaluations. |

Machine-generated walk-forward, stress, and seed reports must stay in the
external experiment store. A durable document may summarize them only when it
states the data snapshot, code revision, costs, evaluation windows, and final
promotion decision.
