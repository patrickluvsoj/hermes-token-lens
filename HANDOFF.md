# HANDOFF — Token Lens plugin (M1 ~95% complete)

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

## REMAINING — in order

### 1. Finish M1 exit QA (task in progress)
- Open `http://127.0.0.1:9119/token-lens` in a browser (the printed dashboard
  URL carries `?token=`; or use the env-token instance above). Verify: tab
  renders, first-run state shows with the Import button, entry card setup
  state appears at the top of `/sessions`.
- Click **Import 30 days** → progress → charts fill hatched (there IS real
  history in `~/.hermes/state.db` to import).
- **Recorder live-path:** running gateway/CLI sessions predate the plugin
  enable, so hooks aren't loaded in them — the recorder-not-detected banner
  should show after backfill (it keys off zero `api_calls` + core sessions
  newer than install). Start a NEW `hermes` CLI session, chat once, exit;
  verify `/summary` flips to recorder data and buckets sum to billed tokens.
  ⚠️ Do not kill the user's existing gateway (PID may vary) without asking.
- Walk the QA test plan file (top of this doc) — it lists every state to check.
- The plan's e2e scripts (`e2e/install_flow.sh`, `e2e/unlock_flow.sh`) are
  NOT yet written — they're specified in the design plan §Testing.

### 2. M1 polish
- Theme-switch QA (3 Hermes themes) — `--tl-*` vars map to host tokens, verify
  contrast + hatching in light themes.
- README screenshots; tag v0.1.0.

### 3. M2 (design plan §Suggestion engine — all specified, none built)
- LLM suggestion generation via `ctx.llm.complete_structured`
  (`purpose="token-lens.suggest"`), `suggestion-guidelines.md`, rubric
  evaluator (`rubric.md`, guardrails: ≤7 criteria, scale=10, threshold
  immutable), meta-cost ledger + `meta_budget_tokens` cap.
- `hermes token-lens refresh` CLI command + dashboard spawn wiring
  (plan T11; `_spawn_hermes_action` pattern in `hermes_cli/web_server.py`).
- Rules/rubric evolution + EVOLUTION.md auto-log (plan T12, user decision D16).
- Eval suites `evals/eval_suggestions.py`, `evals/eval_evaluator.py` (plan §Testing).

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
