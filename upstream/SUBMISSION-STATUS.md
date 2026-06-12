# Upstream submission status

Fork: **patrickluvsoj/hermes-agent** (of NousResearch/hermes-agent).
All three changes are pushed as branches on the fork.

## Submitted

- **sdk.d.ts registerSlot order fix** → **PR opened**:
  https://github.com/NousResearch/hermes-agent/pull/44698
  Branch: `fix/sdk-registerslot-order`. One-line doc fix; `git apply --check`
  verified clean before push.

## Code written + pushed, PR NOT opened (yours to submit)

These carry real code on the fork; open the PR when you're ready.

1. **request_tools passthrough** — branch `feat/pre-api-request-tools-passthrough`
   - Adds `request_tools` (raw `api_kwargs['tools']`) to the `pre_api_request`
     hook in `agent/conversation_loop.py` + a sample in `hermes_cli/hooks.py`.
   - Open the PR:
     ```
     gh pr create --repo NousResearch/hermes-agent \
       --base main --head patrickluvsoj:feat/pre-api-request-tools-passthrough \
       --title "feat(plugins): full-fidelity request_tools passthrough on pre_api_request" \
       --body-file upstream/PR-tools-passthrough.md
     ```
   - Or web: https://github.com/NousResearch/hermes-agent/compare/main...patrickluvsoj:feat/pre-api-request-tools-passthrough?expand=1

2. **sessions:overview-top slot** — branch `feat/sessions-overview-top-slot`
   - Adds the slot to `KNOWN_SLOT_NAMES` + doc comment in
     `web/src/plugins/slots.ts` and renders `<PluginSlot>` in the overview
     branch of `web/src/pages/SessionsPage.tsx`.
   - Open the PR:
     ```
     gh pr create --repo NousResearch/hermes-agent \
       --base main --head patrickluvsoj:feat/sessions-overview-top-slot \
       --title "feat(dashboard): add sessions:overview-top plugin slot" \
       --body-file upstream/PR-overview-top-slot.md
     ```
   - Or web: https://github.com/NousResearch/hermes-agent/compare/main...patrickluvsoj:feat/sessions-overview-top-slot?expand=1

## Adoption note

Once the tools-passthrough PR lands, token-lens can switch its schema costing
from best-effort estimation to the real `request_tools` array via a
rules-version bump — no fork needed meanwhile.
