"""Decomposition: request_messages -> estimated category sections.

Pins: section split by role, system-block marker rules, unknown content ->
unattributed, and full independence from the sanitized payload's tools array
(decomposition takes schema costs from the recorder, never from payloads).
"""
import token_lens_core as core


RULES = core.DEFAULT_RULES


def test_roles_split_into_categories():
    msgs = [
        {"role": "system", "content": "You are a helpful agent." * 10},
        {"role": "user", "content": "hello " * 50},
        {"role": "assistant", "content": "hi there " * 40},
        {"role": "tool", "content": "result data " * 30},
    ]
    buckets = core.decompose_request(msgs, rules=RULES)
    assert buckets["system_prompt"] > 0
    assert buckets["history.user"] > 0
    assert buckets["history.assistant"] > 0
    assert buckets["tool_results"] > 0


def test_skills_block_attributed_to_skill_loading():
    skills = "<available_skills>" + "skill entry " * 100 + "</available_skills>"
    system = "base prompt text " * 20 + skills
    msgs = [{"role": "system", "content": system}]
    buckets = core.decompose_request(msgs, rules=RULES)
    assert buckets["skill_loading"] == core.estimate_tokens(skills)
    # base prompt stays system_prompt, never unattributed
    assert buckets["system_prompt"] > 0
    assert "unattributed" not in buckets


def test_memory_heading_block_attributed():
    system = (
        "identity text\n"
        "## Relevant Memories\nremembered fact one\nremembered fact two\n"
        "## Other Section\nmore guidance\n"
    )
    buckets = core.decompose_system_prompt(system, RULES)
    assert buckets.get("memory", 0) > 0
    assert buckets.get("system_prompt", 0) > 0


def test_unknown_role_goes_unattributed():
    msgs = [{"role": "weird", "content": "mystery " * 50}]
    buckets = core.decompose_request(msgs, rules=RULES)
    assert list(buckets.keys()) == ["unattributed"]


def test_schema_costs_come_from_recorder_not_payload():
    msgs = [{"role": "user", "content": "hi"}]
    costs = {"tool_schemas.builtin": 900, "tool_schemas.mcp.browser": 4100}
    buckets = core.decompose_request(msgs, rules=RULES, schema_costs=costs)
    assert buckets["tool_schemas.builtin"] == 900
    assert buckets["tool_schemas.mcp.browser"] == 4100


def test_truncated_payload_independence():
    # A sanitized/collapsed payload shape must not be consulted at all:
    # decompose_request only reads request_messages + schema_costs.
    msgs = [{"role": "user", "content": "real full-fidelity message " * 20}]
    buckets = core.decompose_request(msgs, rules=RULES, schema_costs={})
    assert buckets["history.user"] == core.estimate_tokens(
        "real full-fidelity message " * 20
    )


def test_image_parts_flat_cost():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "look at this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA" * 10000}},
    ]}]
    buckets = core.decompose_request(msgs, rules=RULES)
    # flat ~1500 tokens for the image, not 10k+ of base64
    assert 1400 < buckets["history.user"] < 1700


def test_mcp_server_extraction():
    assert core.mcp_server_for_tool("mcp_browser-tools_navigate", RULES) == "browser-tools"
    assert core.mcp_server_for_tool("read_file", RULES) is None
