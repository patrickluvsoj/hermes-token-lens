"""Eval: rubric evaluator discrimination against golden suggestion fixtures.

COSTS TOKENS — run manually, never in CI:

    ~/.hermes/hermes-agent/venv/bin/python evals/eval_evaluator.py

Feeds the evaluator (same schema + rubric the engine uses) three fixtures:
STRONG (evidence-cited, credible, honest), VAGUE (no numbers, generic), and
DISHONEST (inflated savings, no risk note). Passes when STRONG scores >=
threshold and VAGUE + DISHONEST score below it. Re-run on every rubric
version bump.
"""
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import engine  # noqa: E402

THRESHOLD = 6

AGGREGATES = {
    "week_total_tokens": 490_000,
    "cache_hit_rate": 0.04,
    "category_tokens": {"tool_schemas.mcp.dead-server": 40_000,
                        "history.user": 120_000, "tool_results": 150_000},
    "sessions": 12,
}

FIXTURES = [
    {  # STRONG
        "title": "Disable unused MCP server: dead-server",
        "target": "mcp:dead-server",
        "category": "tool_schemas.mcp",
        "evidence": "40k schema tokens this week (8% of 490k total), 0 tool results in 12 sessions",
        "est_savings_pct": 8,
        "risk": "low",
        "plan_steps": ["Run: hermes mcp disable dead-server",
                       "Restart any running gateway/CLI session",
                       "Re-check Token Lens after ~5 sessions"],
    },
    {  # VAGUE
        "title": "Optimize your prompting style",
        "target": "behavior:prompting",
        "category": "history.user",
        "evidence": "prompts could be more efficient",
        "est_savings_pct": 15,
        "risk": "low",
        "plan_steps": ["Write better prompts"],
    },
    {  # DISHONEST
        "title": "Disable all tool schemas for massive savings",
        "target": "config:all-tools",
        "category": "tool_schemas.builtin",
        "evidence": "tools use tokens",
        "est_savings_pct": 70,
        "risk": "low",
        "plan_steps": ["Disable every toolset"],
    },
]


def main() -> int:
    try:
        from agent.plugin_llm import PluginLlm
        llm = PluginLlm(plugin_id="token-lens")
    except Exception as exc:
        print(f"SKIP: host LLM unavailable ({exc})")
        return 0

    rubric_md = engine.RUBRIC_PATH.read_text(encoding="utf-8")
    ev = llm.complete_structured(
        instructions=("Score each suggestion 0-10 against the rubric. Be strict: "
                      "evidence must reference the aggregates; savings must be credible."),
        input=[
            {"type": "text", "text": "RUBRIC (v1):\n" + rubric_md},
            {"type": "text", "text": "AGGREGATES:\n" + json.dumps(AGGREGATES, indent=1)},
            {"type": "text", "text": "SUGGESTIONS:\n" + json.dumps(FIXTURES, indent=1)},
        ],
        json_schema=engine.EVALUATION_SCHEMA,
        schema_name="token_lens_scores",
        max_tokens=1500,
        purpose="token-lens.evaluate",
    )
    parsed = getattr(ev, "parsed", None)
    if not isinstance(parsed, dict):
        print("FAIL: evaluator returned no valid JSON")
        return 1
    totals = {int(s["index"]): float(s["total"]) for s in parsed.get("scores", [])
              if isinstance(s, dict) and "index" in s}
    print("scores:", totals)
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        print(("  ✓ " if cond else "  ✗ ") + name)
        passed, failed = passed + (1 if cond else 0), failed + (0 if cond else 1)

    check(f"STRONG >= {THRESHOLD}", totals.get(0, -1) >= THRESHOLD)
    check(f"VAGUE < {THRESHOLD}", totals.get(1, 99) < THRESHOLD)
    check(f"DISHONEST < {THRESHOLD}", totals.get(2, 99) < THRESHOLD)
    check("STRONG outscored both", totals.get(0, -1) > max(totals.get(1, 99), totals.get(2, 99))
          if 0 in totals else False)

    usage = getattr(ev, "usage", None)
    spent = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
    print(f"== eval_evaluator: {passed} passed, {failed} failed ({spent} tokens spent) ==")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
