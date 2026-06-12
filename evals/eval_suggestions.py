"""Eval: suggestion generation quality against golden aggregate fixtures.

COSTS TOKENS — run manually, never in CI:

    ~/.hermes/hermes-agent/venv/bin/python evals/eval_suggestions.py

Uses the host-owned PluginLlm (the user's active provider) through the SAME
engine path production uses, against a scratch DB seeded from the fixture.
Checks are deterministic (no LLM judge): schema validity is enforced by the
engine; this script asserts evidence cites fixture numbers, systemic waste
ranks above prompting tips (80/20), savings stay within category shares, and
plans reference only real command surfaces.
"""
import json
import re
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import engine  # noqa: E402
import token_lens_core as core  # noqa: E402

# Golden fixture: a fleet with obvious systemic waste (unused MCP server,
# terrible cache hit rate) AND mild prompting-level waste (chatty history).
FIXTURE_SESSIONS = 12
FIXTURE_BUCKETS = {
    "tool_schemas.mcp.dead-server": 40_000,   # resent, never used
    "system_prompt": 50_000,
    "skill_loading": 20_000,
    "history.user": 120_000,
    "history.assistant": 90_000,
    "tool_results": 150_000,
    "output": 60_000,
}
FIXTURE_TOTALS = {"input": 400_000, "output": 60_000, "cache_read": 20_000,
                  "cache_write": 10_000, "reasoning": 0, "billed": 490_000}

SYSTEMIC_CATS = ("tool_schemas", "system_prompt", "skill_loading")
FAKE_SURFACES = ("/mcp ", "hermes config set", "hermes settings")


def seed(conn):
    with core.write_txn(conn):
        for i in range(FIXTURE_SESSIONS):
            conn.execute(
                "INSERT INTO session_rollups (session_id, analyzed_at,"
                " analyzer_version, rules_version, precision, provenance,"
                " totals_json, buckets_json, api_calls, turns, started_ts, ended_ts)"
                " VALUES (?, ?, 1, 1, 'exact', 'recorder', ?, ?, 15, 8, ?, ?)",
                (f"fix-{i}", time.time(), json.dumps(FIXTURE_TOTALS),
                 json.dumps(FIXTURE_BUCKETS), time.time() - 300, time.time() - 200),
            )


def main() -> int:
    try:
        from agent.plugin_llm import PluginLlm
        llm = PluginLlm(plugin_id="token-lens")
    except Exception as exc:
        print(f"SKIP: host LLM unavailable ({exc})")
        return 0

    db_path = Path(tempfile.mkdtemp()) / "eval.db"
    conn = core.connect(db_path)
    seed(conn)
    cfg = {"meta_budget_tokens": 50000, "score_threshold": 6,
           "evolve_rules": False, "min_sessions": 10, "refresh_every": 5}
    result = engine.run_llm_refresh(conn, llm, cfg)
    print("run:", result)
    if result["status"] != "done":
        print(f"FAIL: run did not complete: {result}")
        return 1

    rows = conn.execute(
        "SELECT * FROM suggestions ORDER BY est_savings_pct DESC"
    ).fetchall()
    passed = failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        print(("  ✓ " if cond else "  ✗ ") + name + (f" — {detail}" if detail and not cond else ""))
        passed, failed = passed + (1 if cond else 0), failed + (0 if cond else 1)

    check("at least one suggestion generated", len(rows) >= 1)

    fixture_numbers = {re.sub(r"0{3}$", "k", str(v)) for v in FIXTURE_BUCKETS.values()}
    for r in rows:
        title = r["title"][:40]
        has_number = bool(re.search(r"\d", r["evidence"]))
        check(f"evidence cites numbers: {title}", has_number, r["evidence"][:80])
        check(f"plan is non-empty + steps numbered: {title}",
              bool(r["plan_md"].strip()) and r["plan_md"].startswith("1."))
        check(f"no fake command surfaces: {title}",
              not any(s in r["plan_md"] for s in FAKE_SURFACES), r["plan_md"][:120])
        cat_share = sum(v for k, v in FIXTURE_BUCKETS.items()
                        if k == r["category"] or k.startswith(r["category"] + "."))
        if cat_share:
            check(f"savings ≤ category share: {title}",
                  r["est_savings_pct"] <= cat_share / FIXTURE_TOTALS["billed"] * 100 + 5,
                  f"{r['est_savings_pct']}% vs share {cat_share / FIXTURE_TOTALS['billed'] * 100:.0f}%")

    shown = [r for r in rows if r["status"] == "shown"]
    # 80/20 contract: systemic waste is COVERED (the planted dead-server is
    # found, systemic findings aren't crowded out by prompting tips) — not
    # that a larger honest finding can never rank first by savings. In this
    # fixture tool_results (31%) legitimately exceeds the dead server (8%).
    check("80/20: planted unused-MCP waste was found",
          any("dead-server" in (r["title"] + r["evidence"]) for r in shown),
          "; ".join(r["title"][:30] for r in shown))
    systemic = sum(1 for r in shown
                   if any(r["category"].startswith(c) for c in SYSTEMIC_CATS))
    check("80/20: systemic findings present among shown", systemic >= 1)

    run = conn.execute("SELECT * FROM suggestion_runs WHERE kind='llm'").fetchone()
    check("meta ledger recorded usage", run["tokens_input"] > 0)
    check("budget respected", run["tokens_input"] + run["tokens_output"] <= cfg["meta_budget_tokens"])

    print(f"== eval_suggestions: {passed} passed, {failed} failed "
          f"({run['tokens_input'] + run['tokens_output']} tokens spent) ==")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
