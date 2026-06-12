# Draft PR: unsanitized `tools` passthrough on `pre_api_request`

**Target:** NousResearch/hermes-agent · **Status:** draft, not submitted

## What

Add a full-fidelity `request_tools` kwarg to the `pre_api_request` plugin
hook — the actual per-call tools array from `api_kwargs`, passed the same
way `request_messages` already is (an intentionally-unsanitized raw
passthrough; see the comment block at agent/conversation_loop.py ~903).

## Why

Observability plugins cannot reconstruct the real per-call tool array today:

- `agent.tools` is assembled post-registry — memory/context-engine schemas
  are appended after registry assembly (agent/agent_init.py ~949/~1196/~1568)
- providers mutate schemas before send (agent/chat_completion_helpers.py ~620)
- the sanitized `request` payload truncates at HERMES_PLUGIN_PAYLOAD_MAX_CHARS
  (50k default), and the hook otherwise exposes only `tool_count`

So per-schema token attribution (which server costs what, per call) is
best-effort estimation for every plugin. The token-lens plugin currently
estimates from registry state and calibrates against billed totals; a real
passthrough makes schema costs attribution-grade for every observability
plugin at once.

## How

In agent/conversation_loop.py where `pre_api_request` is invoked (~927),
add alongside `request_messages`:

```python
request_tools=list(api_kwargs.get("tools") or []),
```

Same precedent and security posture as `request_messages`: `api_kwargs` is
the object already sent to the provider client; secrets are not expected.

## Adoption

token-lens adopts opportunistically via a rules-version bump — no fork
needed meanwhile.
