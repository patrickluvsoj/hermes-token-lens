# UPSTREAM HANDOFF — hermes-agent contributions from token-lens

Self-contained. Written for review/resume by the user, Claude, Codex, or a
Hermes session — no conversation context needed. Last updated 2026-06-12
after guideline-compliance work.

## The three contributions at a glance

| Change | Branch on `patrickluvsoj/hermes-agent` | State | Risk |
|---|---|---|---|
| sdk.d.ts registerSlot arg order | `fix/sdk-registerslot-order` | **PR OPEN: NousResearch/hermes-agent#44698** | none (doc-only) |
| `request_tools` hook passthrough | `feat/pre-api-request-tools-passthrough` | code + test pushed, **PR not opened** | low (additive kwarg) |
| `sessions:overview-top` slot | `feat/sessions-overview-top-slot` | code pushed, **PR not opened** | low (additive UI slot) |

PR bodies ready to paste: `upstream/PR-tools-passthrough.md` and
`upstream/PR-overview-top-slot.md` (each follows CONTRIBUTING.md's
What/Why/How-to-test/Platforms spec). Submit commands are at the bottom.

## WHY each change matters (the one-paragraph versions)

1. **sdk.d.ts fix** — the typed SDK contract documents `registerSlot(slot,
   name, component)`; the runtime is `(plugin, slot, component)`. Following
   the docs registers your component into a slot named after your plugin:
   it silently never renders. This is a trap every future plugin author
   walks into (it cost us the token-lens entry card during QA). Doc-only,
   zero behavior change.

2. **`request_tools` passthrough** — per-call tool schemas are usually the
   largest *controllable* token cost (resent on every API call), and no
   plugin can measure them exactly: registry state isn't what gets sent
   (post-registry appends + provider mutation), the sanitized payload
   truncates at 50k chars, and `tool_count` is just an integer. One
   additive kwarg — mirroring the existing `request_messages` passthrough —
   turns every observability plugin's schema-cost numbers from estimates
   into facts. token-lens adopts it with a rules-version bump the day it
   lands; until then its calibration absorbs the estimate error.

3. **`sessions:overview-top` slot** — the Sessions Overview is the page
   users read daily, and it's the highest-traffic composition with no
   page-scoped slot. `sessions:top` sits above the view toggle (outside the
   overview, visible even in List view). One slot lets any session-adjacent
   plugin (usage analytics, cost alerts) put an insight card inside the
   overview flow. Renders nothing when no plugin registers — invisible to
   everyone else.

## Guideline compliance (CONTRIBUTING.md), verified 2026-06-12

| Requirement | tools-passthrough | overview-top slot |
|---|---|---|
| Branch naming (`feat/...`) | ✓ | ✓ |
| Conventional commit (`feat(scope): ...`) | ✓ `feat(plugins):` | ✓ `feat(dashboard):` |
| Tests run | ✓ targeted: `-k api_request` 1 passed, `-k hook` 12 passed, `test_plugins.py` + `test_langfuse_plugin.py` 127 passed (fork source verified via `run_agent.__file__`) | ✓ `tsc -p . --noEmit` exit 0; eslint: 4 errors but **identical on main** (pre-existing `react-hooks/set-state-in-effect`), 0 new |
| Test ADDED | ✓ `request_tools` list-shape assertion in `test_request_scoped_api_hooks_fire_for_each_api_call` | n/a (no web unit-test harness; manual steps in PR body) |
| Docs | inline comment + `hermes hooks test` sample payload — consistent with `request_messages`, which has no website-doc entry either | `KNOWN_SLOT_NAMES` doc comment updated (the slot table IS the doc, per extending-the-dashboard.md) |
| One logical change per PR | ✓ | ✓ |
| Cross-platform | pure-Python kwarg plumbing | web-only |
| Priority fit | #4 robustness / plugin ecosystem | #7-adjacent (plugin surface) |

**Full-suite caveat:** only targeted test files were run (the repo's
`scripts/run_tests.sh` full suite needs the uv dev env per CONTRIBUTING;
not built here). If a reviewer wants the full run first:
`cd /tmp/tl-fork && uv venv venv --python 3.11 && uv pip install -e ".[all,dev]" && scripts/run_tests.sh`.

## How to review (any agent or human, ~10 min)

1. Diffs: `git diff main...feat/pre-api-request-tools-passthrough` and
   `git diff main...feat/sessions-overview-top-slot` in a clone of
   `patrickluvsoj/hermes-agent` (a working clone exists at `/tmp/tl-fork`
   on this machine — may not survive reboot; re-clone if gone).
2. Check the WHY claims: post-registry appends at `agent/agent_init.py`
   ~949/~1196/~1568; provider mutation at `agent/chat_completion_helpers.py`
   ~620; payload cap in `run_agent.py` `_hook_payload_max_chars`;
   `sessions:top` placement at `SessionsPage.tsx` ~1119 vs overview branch
   ~1526.
3. Re-run the verification commands from the compliance table.

## Submit (user action — needs your gh auth)

```bash
cd ~/.hermes/plugins/token-lens
gh pr create --repo NousResearch/hermes-agent \
  --base main --head patrickluvsoj:feat/pre-api-request-tools-passthrough \
  --title "feat(plugins): full-fidelity request_tools passthrough on pre_api_request" \
  --body-file upstream/PR-tools-passthrough.md

gh pr create --repo NousResearch/hermes-agent \
  --base main --head patrickluvsoj:feat/sessions-overview-top-slot \
  --title "feat(dashboard): add sessions:overview-top plugin slot" \
  --body-file upstream/PR-overview-top-slot.md
```

Or web: compare links in `SUBMISSION-STATUS.md`.

## After they land (token-lens side)

- tools-passthrough merged → switch schema costing to `request_tools` in
  `__init__.py::on_pre_api_request` (read the kwarg, sum
  `estimate_tokens(json.dumps(schema))` per schema, group by MCP prefix);
  bump `category_rules` version so rollups recompute; the self-correcting
  est-ratio (M3-T2) then converges to ~1.0 for the schema share.
- overview-top merged → add `"sessions:overview-top"` to
  `dashboard/manifest.json` slots + a second `registerSlot` call in
  `dashboard/dist/index.js` (keep `sessions:top` for older hosts).
- PR #44698 merged → delete the local workaround comment in
  `dashboard/dist/index.js` (the arg-order note) — behavior already correct.
