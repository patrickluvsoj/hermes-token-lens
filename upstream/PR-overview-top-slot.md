# Draft PR: `sessions:overview-top` page slot

**Target:** NousResearch/hermes-agent · **Status:** draft, not submitted

## What

A new shell slot rendering inside the Sessions page's Overview tab, above
the "Recent Sessions" heading — `sessions:overview-top` — added to
KNOWN_SLOT_NAMES in web/src/plugins/slots.ts and rendered from
SessionsPage.tsx (~1072, near `recentSessions`).

## Why

`sessions:top` (the closest existing slot) renders above the Overview/List
view toggle, outside the overview composition. Plugins that surface a
per-overview insight card (e.g. token-lens' "found N% avoidable waste"
strip) want to live INSIDE the overview flow, under the platform stats,
where the user's eye already is. Benefits any plugin author building
session-adjacent surfaces.

## How

1. Add `"sessions:overview-top"` to KNOWN_SLOT_NAMES (slots.ts ~73) + the
   doc comment table.
2. Render `<PluginSlot name="sessions:overview-top" />` in SessionsPage.tsx
   inside the Overview tab branch, above Recent Sessions.

Plugins keep registering via manifest `slots` + `registerSlot` — no API
change. Cards should support both placements during the transition.
