# PR: feat(dashboard): add `sessions:overview-top` plugin slot

**Target:** NousResearch/hermes-agent · **Branch:** `patrickluvsoj:feat/sessions-overview-top-slot` · **Status:** code pushed, PR not yet opened

*(This file is the PR body — paste as-is. Written to CONTRIBUTING.md's
"PR description" spec: What / Why / How to test / Platforms.)*

## What

A new page-scoped plugin slot, `sessions:overview-top`, rendering inside the
Sessions page's **Overview tab**, above the Platforms card and Recent
Sessions. Two diffs:

- `web/src/plugins/slots.ts` — slot added to `KNOWN_SLOT_NAMES` + the doc
  comment table
- `web/src/pages/SessionsPage.tsx` — `<PluginSlot name="sessions:overview-top" />`
  rendered at the top of the overview branch

No API change: plugins keep registering via manifest `slots` +
`registerSlot()`.

## Why this matters

**The Sessions Overview is where users form their daily picture of agent
activity, and plugins currently can't participate in it.**

The closest existing slot, `sessions:top`, renders above the Overview/List
view toggle — outside the overview composition, visually detached from the
stats the user is reading, and present even in List view where an
overview-style insight card is noise. A plugin surfacing a per-overview
insight (the motivating case: token-lens' "found N% avoidable weekly token
waste" strip, https://github.com/patrickluvsoj/hermes-token-lens) has to
choose between squatting above the page chrome or not existing.

Page-scoped slots exist exactly for this ("augment a built-in page without
overriding the whole route" — extending-the-dashboard.md); the Overview tab
is the highest-traffic composition without one. Any session-adjacent plugin
(usage analytics, cost trackers, alerting summaries) benefits, not just the
motivating case.

## How to test

- `cd web && npx tsc -p . --noEmit` — clean on this branch (exit 0).
- `npx eslint src/plugins/slots.ts src/pages/SessionsPage.tsx` — the 4
  pre-existing `react-hooks/set-state-in-effect` errors are identical on
  `main` (verified side-by-side); this change introduces no new findings.
- Manual: install any slot plugin, point its `registerSlot` at
  `sessions:overview-top`, open /sessions → Overview: the component renders
  above Platforms; switch to History view: it does not render (scoped to
  the overview branch). With no plugin registered, nothing renders
  (PluginSlot returns null) — zero visual change for non-plugin users.

## Platforms tested

macOS (Chromium via the dashboard). Web-only change; no OS-sensitive surface.

## Compatibility

Purely additive. Existing `sessions:top` users are unaffected; cards meant
for both placements can register in both during a transition.
