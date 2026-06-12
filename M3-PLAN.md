# M3 plan — v0.3.0 (post-QA polish)

Self-contained spec; any agent can execute cold. Items surfaced during the
2026-06-12 live QA of v0.2.0 (see HANDOFF.md). Nothing here blocks daily use.

## T1 — Canonical fingerprints across kinds (trust fix; do first)

**Problem:** the same target produces two cards with two fingerprints —
detector `mcp_disable:gbrain` vs LLM `llm:mcp:gbrain`. Dismiss/done
inheritance is keyed by fingerprint, so dismissing one does NOT retire the
other: the user's dismissal silently fails to stick across kinds.

**Fix:** one target-derived grammar, generator-agnostic:

| Target | Canonical fingerprint |
|---|---|
| MCP server `<s>` | `mcp:<s>` |
| cache/prefix behavior | `config:cache-prefix` |
| system prompt + skills size | `config:system-prompt` |
| turn count behavior | `behavior:turn-cap` |
| tool-result carriage | `behavior:tool-results` |
| LLM free-form target `<t>` | `<t>` normalized (`[^a-z0-9:_-]` → `-`), no `llm:` prefix |

- `detectors.py`: emit canonical fingerprints.
- `engine.py`: drop the `llm:` prefix; use the normalized `target` directly.
- **Schema migration v2** rewrites existing rows (GLOB, not LIKE — `_` is a
  LIKE wildcard). Display dedup (latest row per fingerprint) then collapses
  the duplicate pair automatically, and dismiss cascades across kinds.
- Display semantics: when detector and LLM regenerate the same fingerprint,
  the latest run's row shows. Acceptable: detector evidence regenerates each
  watermark, and inheritance carries user actions either way.

**Verify:** migration test (old fingerprints rewritten); cross-kind
inheritance test (dismiss detector row → LLM regeneration inherits).

## T2 — Self-correcting token estimator

**Problem:** chars/4 over-counts real prompts ~2× (live `calib_scale` ≈
0.48). Totals stay exact (calibration), but bucket *shares* skew when
categories have different true chars/token ratios, and `/health`'s ±25%
drift alert will permanently fire once a median builds — alarm fatigue.

**Fix:** learn the correction from our own calibration history. Per model,
keep a running ratio in `meta_kv` (`est_ratio:<model>`): on session
finalize, `ratio *= clamp(median(calib_scale of last N complete calls for
that model))`, clamped to [0.2, 5]. The recorder multiplies decomposed
estimates by the ratio before storing, so future `calib_scale` → ~1.0 and
the drift alert measures residual error only. Multiplicative update is
required — `calib_scale` is computed against *corrected* estimates, so
assigning (not multiplying) would erase the correction once it converges.
No new dependencies (tiktoken stays out).

**Verify:** unit tests — ratio learns from seeded history; corrected
pre-call buckets ≈ billed; convergence (second update ≈ no-op); clamping.

## T3 — By-model for backfilled windows

**Problem:** charts show 29.9M imported tokens while the by-model table says
"no recorded calls" — it reads recorder `api_calls` only; backfill writes
rollups only. Confuses exactly the first-run user D8 courted.

**Fix:** `/by-model` falls back to core `state.db` (read-only) grouped by
`sessions.model` within the window when no recorder rows exist; response
gains `"estimated": true`; the UI badges the table `~estimated`.

**Verify:** API test with a fake core state.db; recorder rows take
precedence when present.

## T4 — Distribution polish

- Capture populated-dashboard screenshots (work surface + entry card) into
  `docs/screenshots/`, reference from README.
- README "Status" section: v0.3.0, test counts, eval results.
- Submission to hermes-example-plugins / skill-hub: prepare; actual
  submission needs a public repo remote (user action).

## T5 — Upstream contributions (prepared, not submitted)

`upstream/` directory with ready-to-send material for NousResearch/hermes-agent:

1. `0001-fix-sdk-dts-registerslot-order.patch` — real patch: `sdk.d.ts`
   documents `registerSlot(slot, name, component)`; runtime
   (`slots.ts`/`registry.ts`) is `(plugin, slot, component)`. Cost us the
   entry card during QA (silently renders nothing).
2. `PR-tools-passthrough.md` — draft PR description for the unsanitized
   `tools` passthrough on `pre_api_request` (design plan Deferred TODO 1;
   the only path to attribution-grade schema costs).
3. `PR-overview-top-slot.md` — draft PR description for a
   `sessions:overview-top` slot (design plan Deferred TODO 2).

Submitting requires a fork + auth — explicitly a user action.

## Ship

All tests green → CHANGELOG v0.3.0 → tag → HANDOFF update.

---

# BACKLOG (explicitly not M3)

## Replay lab — counterfactual simulator (design plan Approach C, phase-2 north star)

Replay recorded per-call request fingerprints under hypothetical configs
("what if MCP X was disabled? tools deferred? compression one turn
earlier?") to attach evidence-backed *projected* savings to every suggestion
instead of estimates. Effort XL (human ~6–8 wk / CC ~1–2 d). Data
prerequisite (per-call rows) has been accumulating since M1 install.
**Needs its own design pass (/office-hours → /plan-eng-review) before any
code** — fingerprint storage shape, simulation fidelity, and replay cost
model are all open. Do not start it as a side quest.
