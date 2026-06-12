# HANDOFF — Token Lens plugin (M1 + M2 + M3 COMPLETE — v0.3.0)

Self-contained status doc for any agent (Claude, Codex, Hermes) or human
resuming this work. No prior conversation context required.

## What this is

**Token Lens** — a drop-in dashboard plugin for Hermes Agent that shows *where
tokens go* (per-category attribution calibrated to billed totals) and *what to
change* (evidence-backed suggestions with copy-paste plans).

- **This repo:** `~/.hermes/plugins/token-lens/` (git, 6 commits, all work committed)
- **Design plan (source of truth):** `~/.gstack/projects/hiroyoshisuzuki/hiroyoshisuzuki-unknown-design-20260611-091729.md`
  — fully reviewed (eng ×2, design ×1, Codex ×2; all CLEAR). Read its
  "Recommended Approach", "Implementation Tasks" (T1–T22), and the design
  decisions tagged D3–D26 inline.
- **QA test plan:** `~/.gstack/projects/hiroyoshisuzuki/hiroyoshisuzuki-unknown-eng-review-test-plan-20260612-091500.md`
- **Approved visual direction:** `~/.gstack/projects/hiroyoshisuzuki/designs/token-lens-tab-20260612/variant-C.png`
  (dense terminal-inspired, mono-forward; A/B are rejected alternates;
  wireframe.html is superseded — do not build from it)

## Architecture (one paragraph)

Two processes share `~/.hermes/token_lens.db` (SQLite WAL, busy_timeout=5000,
`BEGIN IMMEDIATE` for all multi-statement writes, `PRAGMA user_version`
migrations, newer-than-code DBs refuse to open). The **recorder**
(`__init__.py`) runs agent-side via plugin hooks, never raises into the
conversation loop (fail-open + 5-error circuit breaker), buffers writes
(flush at 20 calls / 5s / session-finalize), and decomposes every API call
into category buckets calibrated so they sum exactly to billed tokens. The
**dashboard API** (`dashboard/plugin_api.py`, FastAPI router) runs in the
dashboard process. **Shared logic** is `token_lens_core.py` (imported by the
dashboard via sys.path insertion — importlib doesn't resolve siblings).
**Detectors** (`detectors.py`) are deterministic rules (no LLM) that emit
suggestion rows after 3 recorded sessions. The **UI** is a no-build IIFE
(`dashboard/dist/index.js`) on `window.__HERMES_PLUGIN_SDK__`.

## State: DONE (committed, tested)

| Piece | File | Tests |
|---|---|---|
| Schema/migrations/calibration/decomposition | `token_lens_core.py` | `tests/test_core_*.py`, `test_migrations.py` |
| Suggestion lifecycle: fingerprint inheritance (single SQL statement, latest USER ACTION wins, dismiss/done cascade across fingerprint), dismissed resurrects only at ≥2× savings | `token_lens_core.py::insert_suggestion/set_suggestion_status` | `test_suggestion_lifecycle.py` incl. 100-round two-thread race |
| Recorder hooks + breaker + buffering | `__init__.py` | `test_recorder.py`, `test_recorder_failopen.py` |
| Rollups (±2% reconciliation→exact/estimated), debounced sweep, retention | `token_lens_core.py` | `test_rollups.py` |
| Refresh queue (atomic claim, 30-min TTL reclaim, UNIQUE watermark), split gates (detector=3 / llm=10, backfill NEVER counts) | `token_lens_core.py` | `test_queue.py` |
| Backfill from core `state.db` (read-only URI, chunked, resumable) | `token_lens_core.py::backfill` | `test_backfill.py` |
| 5 detectors (unused MCP server, low cache hit, oversized prompt, runaway turns, heavy tool results) | `detectors.py` | `test_detectors.py` |
| Dashboard API (summary/categories/timeseries/by-model/sessions/{id}/suggestions+refresh+dismiss+done/backfill+status/meta/health) | `dashboard/plugin_api.py` | `test_api.py` |
| UI: composed work surface, waste map, pills below suggestions, state table, first-run backfill, acted-on strip, insight-strip entry card, drill-down, a11y/responsive | `dashboard/dist/index.js` | syntax-checked; browser QA pending |

**95 tests passing**: `~/.hermes/hermes-agent/venv/bin/python -m pytest`
(run from the repo root; system python3 has no pytest — always use the hermes venv).

## Verified live (headless smoke, 2026-06-12)

- `hermes plugins enable token-lens` ✓ (already enabled)
- Dashboard discovery: `GET /api/dashboard/plugins` lists token-lens, `has_api: true`, slot `sessions:top` ✓
- API mounted + authenticated: `/health`, `/summary` (empty-DB → `has_any_data:false` → first-run state), `/suggestions` (gate reasons "0/3", "0/10") all return correct shapes ✓
- Bundle served: `GET /dashboard-plugins/token-lens/dist/index.js` → 200, 36KB ✓
  (note: that's the asset route; `/plugins/...` paths return the SPA fallback HTML)

A QA dashboard may still be running on port 9119 (started with
`HERMES_DASHBOARD_SESSION_TOKEN=$(cat /tmp/token-lens-qa-token)`; log at
`/tmp/token-lens-dashboard.log`). Kill it or reuse it. To restart:

```bash
HERMES_DASHBOARD_SESSION_TOKEN=$(cat /tmp/token-lens-qa-token) hermes dashboard --no-open --port 9119 --skip-build
# authenticated curl:
curl -H "Authorization: Bearer $(cat /tmp/token-lens-qa-token)" http://127.0.0.1:9119/api/plugins/token-lens/health
```

## M1 EXIT QA — DONE (2026-06-12, live against the real dashboard + real data)

Full loop verified end-to-end via headless browser ($B / gstack browse) +
4 real one-shot sessions (`hermes -z`):

- First-run state → **Import 30 days** → progress → charts filled with the
  user's real history (29.9M tokens, hatched + `~estimated` badge) ✓
- Backfilled sessions correctly did NOT satisfy gates (0/3 stayed) ✓
- Live recorder: buckets sum EXACTLY to billed prompt tokens on real traffic
  (15,654 = 15,654, calib_scale stored) ✓
- Sweep recovered an unfinalized session within one dashboard visit
  (precision=exact, ±2% reconciliation passed) ✓
- Detector gate opened at 3/3; detectors fired and produced a REAL finding:
  "Disable unused MCP server: gbrain — 3.3k schema tokens/call, 0 results"
  with evidence, risk note, copy-paste plan ✓
- Composed work surface renders per variant C: savings figure leading, waste
  map with expandable MCP legend, split-gate copy "(4/10)", pills below
  suggestions, KPI strip, 7-day bars, by-model with recorder data, footer ✓
- Entry card on /sessions: setup state pre-data, then insight strip
  "Token Lens found 4% avoidable weekly token waste · Top: …" ✓

### Bugs found live and FIXED (commit "fix: two live-QA bugs")
1. **registerSlot arg order** — runtime is `registerSlot(plugin, slot,
   component)` (`web/src/plugins/slots.ts`); the host's `sdk.d.ts` documents
   `(slot, name, component)` and is WRONG. Upstream doc bug worth a PR.
2. **One-shot data loss** — fast `hermes -z` runs exited before any buffer
   flush; `on_session_finalize` does not reliably fire in `-z` mode. Posts
   now flush immediately + atexit backstop; detectors also run gate-checked
   at session START. If touching the recorder, preserve these properties.

Known minor observations (not bugs): calib_scale ≈ 0.48 on real traffic —
the chars/4 estimator overestimates ~2× and calibration absorbs it exactly
as designed, but /health will raise the drift alert once a median builds;
improving the estimator is an M2 candidate. By-model table shows recorder
data only (backfill windows show it empty while charts have data).

## M2 — DONE (2026-06-12, v0.2.0; all design-plan scope implemented)

- Theme QA: Nous Blue (light), Midnight, Cyberpunk all render via `--tl-*`
  token mapping; original theme restored. Screenshots in `/tmp/tl-theme-qa/`.
- e2e scripts written + green: `e2e/install_flow.sh` (5/5; `TL_RUN_SESSION=1`
  adds the live-recorder leg), `e2e/unlock_flow.sh` (7/7, scratch DB).
- **Engine** (`engine.py`): generation (`purpose=token-lens.suggest`) +
  evaluator (`purpose=token-lens.evaluate`) via `ctx.llm.complete_structured`,
  deterministic aggregates only (never transcripts), score_threshold hiding,
  meta ledger + `meta_budget_tokens` hard abort.
- **CLI**: `hermes token-lens refresh [--force]` registered via
  `ctx.register_cli_command`; dashboard POST /suggestions/refresh spawns it
  detached (own copy of the `_spawn_hermes_action` pattern — the core helper
  is name-gated; log at `~/.hermes/logs/token-lens-refresh.log`).
- **Evolution**: rule proposals + rubric amendments auto-apply as versioned
  rows under guardrails; human-readable log at
  `~/.hermes/token_lens.EVOLUTION.md` (`engine.log_evolution`).
- **Evals green against a live provider**: eval_evaluator 4/4 (strong 8.5 /
  vague 0.5 / dishonest 0.0), eval_suggestions 17/17 (planted dead-server
  waste found; evidence numeric; no fake command surfaces; budget respected).
- **Live integration proof**: `hermes token-lens refresh --force` on the real
  install → 1 LLM suggestion shown (3,859 tokens, ledgered) that independently
  re-found the same unused `gbrain` MCP server the detector flagged.

## M3 — DONE (2026-06-12, v0.3.0; see M3-PLAN.md for the spec)

- **T1 canonical fingerprints**: schema v2 migrates `mcp_disable:*`/`llm:*`
  into one target-derived grammar; dismiss/done cascades across kinds.
  Live-verified: the doubled gbrain card collapsed to one after migration.
- **T2 self-correcting estimator**: per-model `est_ratio:<model>` in meta_kv,
  multiplicative update from calib_scale medians, applied at record time.
- **T3 by-model fallback**: backfill-only windows read core state.db,
  badged `~estimated`; recorder rows take precedence.
- **T4 README**: populated screenshots in `docs/screenshots/` + status.
- **T5 upstream/**: `0001-fix-sdk-dts-registerslot-order.patch` (verified
  `git apply --check` clean against the local hermes-agent checkout) + draft
  PR descriptions for the tools passthrough and overview-top slot.

## REMAINING — user actions + backlog

- **Submit upstream material** (`upstream/`) to NousResearch/hermes-agent —
  needs a fork + gh auth (user action; a chip for the sdk.d.ts fix exists).
- **Publish the repo + submit to hermes-example-plugins / skill-hub** — needs
  a public remote (user action).
- **BACKLOG: replay-lab counterfactual simulator** (design plan Approach C) —
  see M3-PLAN.md §BACKLOG. Needs its own design pass before any code; its
  per-call data prerequisite has been accumulating since M1 install.

## Key contracts (verified against hermes-agent source — do not re-derive)

- Hook payloads (`agent/conversation_loop.py` ~903/~3316):
  `api_request_id = f"{turn_id}:api:{api_call_count}"` on pre AND post;
  pre carries `request_messages` (full-fidelity list — the sanitized
  `request` payload is truncated at 50k chars, use it for NOTHING);
  post carries `usage` = dict with `input_tokens/output_tokens/cache_read_tokens/cache_write_tokens/reasoning_tokens/prompt_tokens/total_tokens`.
  `on_session_finalize(session_id, platform, reason)`. Register via
  `register(ctx)` + `ctx.register_hook(name, fn)`; `ctx.llm` is the M2 facade.
- Dashboard plugin loading: `~/.hermes/plugins/<name>/dashboard/manifest.json`;
  `api: plugin_api.py` must expose module-level `router`; mounts at
  `/api/plugins/<name>/` AT STARTUP ONLY (restart dashboard after changes).
  UI registers via `window.__HERMES_PLUGINS__.register("token-lens", Page)` +
  `registerSlot("sessions:top", "token-lens", Card)`.
- Billed-total formula (matches core Analytics, decision D16):
  `input + cache_read + cache_write + output`.
- Category IDs are FROZEN (palette slot per ID, decision D14); only mapping
  rules evolve, as versioned `category_rules` rows; rollups recompute lazily
  on version bump (the sweep handles it).
- Time series stacks by CATEGORY only, never by model (user decision D20).

## Gotchas discovered

- `jq` is not installed on this machine (gstack task JSONLs are empty touch-files).
- The hermes venv python is `~/.hermes/hermes-agent/venv/bin/python` (3.11);
  pytest + httpx were installed into it for this work.
- gstack designer binary: `variants` has a 120s/image timeout — use sequential
  `generate`; OpenAI intermittently returns Cloudflare 520, retry once.
- Plugin asset URL is `/dashboard-plugins/<name>/<path>` — other paths return
  the SPA index.html with status 200 (looks like success, isn't).
- SQLite `INSERT ... SELECT` inheritance must read only user-actioned rows
  (`status IN ('done','dismissed')` by `status_changed_at`); inheriting from
  "latest row" loses a dismiss that races a generator insert (test
  `test_concurrent_dismiss_and_insert_race` pins this).

## Definition of done for M1

Design plan §Success Criteria 1–7. Quick version: buckets sum to billed on
exact rollups; rollup ≤2s; suggestions gated with evidence+plan; first
suggestion actionable in 5 min; overhead visible+capped; install = clone +
enable + 2 restarts; crashed sessions appear via sweep within one visit.
