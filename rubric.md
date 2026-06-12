# Token Lens — suggestion evaluation rubric (v1)

Score each suggestion 0–10 as the sum of:

| Criterion | Range | What it measures |
|-----------|-------|------------------|
| Usefulness | 0–3 | Would acting on this measurably cut tokens for THIS user, given the aggregates? |
| Specificity | 0–2 | Names the exact server/skill/setting, with quantified evidence from the input. |
| Savings credibility | 0–2 | The estimate is derived from observed data, not vibes; ≤ the category's own share. |
| Capability-risk honesty | 0–2 | Names what degrades and when/how to roll back. |
| Actionability | 0–1 | The plan executes via copy-paste with no missing steps. |

Suggestions scoring below the configured `score_threshold` (default 6) are
stored hidden, not shown.

Self-evolution guardrails (enforced in code, not prose): at most 7 criteria,
total scale fixed at 10, `score_threshold` is never self-modifiable, prior
scores are never rewritten, every amendment lands as a new version with a
rationale and an EVOLUTION.md entry.
