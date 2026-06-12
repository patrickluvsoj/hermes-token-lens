# PR: feat(plugins): full-fidelity `request_tools` passthrough on `pre_api_request`

**Target:** NousResearch/hermes-agent · **Branch:** `patrickluvsoj:feat/pre-api-request-tools-passthrough` · **Status:** code + test pushed, PR not yet opened

*(This file is the PR body — paste as-is. Written to CONTRIBUTING.md's
"PR description" spec: What / Why / How to test / Platforms.)*

## What

Adds one kwarg to the `pre_api_request` plugin hook: `request_tools` — the
actual per-call `api_kwargs["tools"]` array, passed as a raw passthrough
exactly the way `request_messages` already is (same precedent, same comment
block, same security posture: `api_kwargs` is the object already handed to
the provider client, so no secrets are expected in it).

Three small diffs:
- `agent/conversation_loop.py` — the kwarg at the hook invocation (~l.940)
- `hermes_cli/hooks.py` — the synthetic sample payload for `hermes hooks test`
- `tests/run_agent/test_run_agent.py` — payload-shape assertion alongside the
  existing `request_messages` assertion

## Why this matters

**Exact token attribution is impossible from outside the loop, and "exact"
is the difference between analytics users act on and analytics they ignore.**

An observability plugin that wants to answer "how many tokens does each MCP
server's schema cost per call?" has no correct data source today — the gap
is structural, not a missing convenience:

1. `agent.tools` (registry state) is **not what gets sent**: memory and
   context-engine schemas are appended after registry assembly
   (`agent/agent_init.py` ~949/~1196/~1568), and providers mutate schemas
   before send (`agent/chat_completion_helpers.py` ~620).
2. The sanitized `request` payload truncates at
   `HERMES_PLUGIN_PAYLOAD_MAX_CHARS` (50k default) — with a realistic tool
   count, the tools array is exactly the part that degrades or collapses
   to a `_truncated` preview.
3. The only other signal is `tool_count` — an integer.

So every plugin in this space (the bundled langfuse plugin included) either
ignores schema cost or estimates it. The token-lens plugin
(https://github.com/patrickluvsoj/hermes-token-lens) estimates from registry
state and rescales against billed totals — it works, but "calibrated
best-effort" is the ceiling. Tool schemas are typically the largest
*controllable* slice of per-call spend (resent on every API call), which
makes them precisely the slice users want exact numbers for before acting
on a "disable this server" recommendation.

One kwarg removes that ceiling for the whole plugin ecosystem, using the
pattern this hook already established for messages.

**Why not reconstruct it in plugins?** The assembly happens after every
observable seam (post-registry appends, in-provider mutation) — any
reconstruction silently drifts when either changes. The loop is the only
place that knows what was sent.

## How to test

- `pytest tests/run_agent/test_run_agent.py -k api_request` — extended
  `test_request_scoped_api_hooks_fire_for_each_api_call` asserts
  `request_tools` is a list on every pre-call payload.
- Manual: `hermes hooks test pre_api_request` shows the field; any plugin
  registering `pre_api_request` receives it on real calls.
- Run on this branch: `-k hook` (12 passed), `tests/hermes_cli/test_plugins.py`
  + `tests/plugins/test_langfuse_plugin.py` (127 passed) — existing
  consumers unaffected (bundled plugins all accept `**_`).

## Platforms tested

macOS (Darwin 23.5, Python 3.11.15). Pure-Python kwarg plumbing — no file
I/O, process, or terminal surface per CONTRIBUTING's cross-platform list.

## Compatibility

Additive and backward-compatible: hooks that don't name the kwarg swallow
it via `**kwargs` (the documented hook convention used by bundled plugins).
