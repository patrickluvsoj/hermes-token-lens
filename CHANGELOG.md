# Changelog

## [0.2.0] — 2026-06-12

**M2: the suggestion engine thinks. AI-evaluated fixes, self-evolving rules, hard-capped overhead.**

Token Lens now generates AI suggestions from your aggregates — never your
transcripts — and grades its own output before showing you anything.

### What you can do now

- **AI suggestions after 10 sessions.** The engine feeds deterministic
  aggregates (category totals, schema cost vs usage, cache stats, config
  snapshot incl. tool-search state) to your active model and gets back
  evidence-cited suggestions with copy-paste plans. Verified live: it found
  the same unused-MCP-server waste the deterministic detector found,
  independently, citing exact numbers.
- **A rubric gates what you see.** Every suggestion is scored 0–10
  (usefulness, specificity, savings credibility, risk honesty,
  actionability); below-threshold output stays hidden. Eval-proven
  discrimination: strong fixture 8.5, vague 0.5, dishonest 0.0.
- **`hermes token-lens refresh`** — manual/cron entry point honoring all
  gates (`--force` for testing). The dashboard's Refresh button now spawns
  it detached, so results arrive in ~1 minute instead of at next session.
- **Self-evolution, never silent.** The engine may propose category
  mapping-rule improvements and rubric amendments; they auto-apply as
  versioned rows under hard guardrails (≤7 criteria, scale fixed at 10,
  threshold immutable, prior scores never rewritten) and every change is
  logged human-readably to `~/.hermes/token_lens.EVOLUTION.md`.
- **Overhead is visible and capped.** Every engine call's tokens land in the
  meta ledger (footer: "Token Lens overhead: N tokens this week");
  `meta_budget_tokens` (default 50k) hard-aborts a refresh that exceeds it.

### For contributors

113 pytest paths + 2 e2e scripts + 2 manual evals (`evals/` — they cost
tokens; both green against a live provider at release).

## [0.1.0] — 2026-06-12

**M1: the honest ledger ships. Exact totals, calibrated attribution, deterministic findings.**

Token Lens now answers "where do my tokens go" with numbers that sum to your
bill, and hands you your first fix after three recorded sessions.

Install: clone into `~/.hermes/plugins/token-lens`, `hermes plugins enable
token-lens`, restart the dashboard AND any running gateway/CLI session.

### What you can do now

- **See where tokens go.** Every API call is decomposed into stable categories
  (system prompt, tool schemas per MCP server, skills, memory, history, tool
  results, output) and rescaled per call so buckets sum exactly to billed
  prompt tokens. Verified on live traffic: 15,654 estimated = 15,654 billed.
- **Import your history.** One click backfills the last 30 days from core's
  session store as honestly-badged estimates (hatched bars, `~estimated`).
- **Get your first fix on day one.** Five deterministic detectors (unused MCP
  servers, low cache hit rate, oversized system prompt, runaway turns, heavy
  tool results) fire after 3 recorded sessions — each with evidence, a
  capability-risk note, and a copy-paste plan using real Hermes commands.
- **Dismiss means dismissed.** Suggestions carry a stable fingerprint; your
  dismiss/done survives every regeneration (a dismissed finding returns only
  if its savings at least double, and says why). Mark one done and the
  Acted-on strip tracks predicted vs observed change per session.
- **Trust the states.** First-run import screen, recorder-not-detected banner
  with the fix inline, per-region loading/error states, precision badges
  everywhere, drill-down per session, and a footer that reports Token Lens'
  own overhead (0 tokens in M1 — no LLM anywhere yet).

### Reliability properties

- Recorder never raises into the conversation loop: fail-open + a 5-error
  circuit breaker surfaced in `/health`. Posts flush immediately and an
  atexit backstop covers `hermes -z` one-shots that exit before finalize.
- Any process may write: WAL + `busy_timeout` + `BEGIN IMMEDIATE` everywhere;
  schema versioned via `PRAGMA user_version`; a DB from a newer plugin
  version refuses to open (dashboard shows a mismatch card, agent keeps working).
- Crashed/never-finalized sessions appear via the debounced catch-up sweep
  within one dashboard visit.

### For contributors

95 pytest paths (incl. a 100-round dismiss/insert race) + 2 e2e scripts.
`~/.hermes/hermes-agent/venv/bin/python -m pytest` from the repo root.
