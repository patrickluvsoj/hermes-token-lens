"""Deterministic starter detectors (M1, plan step 5).

Rule-based suggestion generators — no LLM anywhere. Each emits suggestion
rows with evidence from exact-path aggregates only (provenance=recorder),
a savings estimate as % of trailing-7-day total tokens, a capability-risk
note, and a copy-paste plan referencing ONLY real Hermes command surfaces
(`hermes mcp disable <server>`, `hermes plugins`, dashboard pages — there is
no /mcp slash command). These rows double as few-shot evidence patterns for
the M2 LLM generator.

Detector findings fire from the detector gate (detector_min_sessions=3,
design review D9) and flow through the same fingerprint inheritance as LLM
suggestions, so Dismiss/Mark-done stick across runs (D6/D22/D23).
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

try:
    from . import token_lens_core as core
except ImportError:  # pragma: no cover — top-level module load
    import token_lens_core as core  # type: ignore

WINDOW_SECONDS = 7 * 86400.0

# Thresholds (named constants; conservative so early findings are credible)
UNUSED_MCP_MIN_SCHEMA_SHARE = 0.03   # server schemas >=3% of window input
LOW_CACHE_HIT_THRESHOLD = 0.30       # cache reads < 30% of prompt side
LOW_CACHE_MIN_CALLS = 30
OVERSIZED_PROMPT_SHARE = 0.25        # system+skills >= 25% of input
RUNAWAY_TURNS_AVG_CALLS = 40
HEAVY_TOOL_RESULTS_SHARE = 0.40


def _window_aggregates(conn) -> Optional[Dict[str, Any]]:
    cutoff = time.time() - WINDOW_SECONDS
    rows = conn.execute(
        "SELECT * FROM session_rollups WHERE provenance='recorder' AND ended_ts >= ?",
        (cutoff,),
    ).fetchall()
    if not rows:
        return None
    buckets, totals, _est = core.aggregate_buckets(rows)
    sessions = len(rows)
    api_calls = sum(r["api_calls"] for r in rows)
    input_side = sum(
        v for k, v in buckets.items() if k not in ("output", "reasoning")
    )
    week_total = totals["billed"]
    return {
        "buckets": buckets,
        "totals": totals,
        "sessions": sessions,
        "api_calls": api_calls,
        "input_side": input_side,
        "week_total": week_total,
    }


def _fmt(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def _candidates(agg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    buckets = agg["buckets"]
    week_total = agg["week_total"] or 1.0
    input_side = agg["input_side"] or 1.0
    totals = agg["totals"]

    # 1. Unused MCP server: heavy schema cost, zero observed tool results.
    for key, schema_tokens in sorted(buckets.items()):
        if not key.startswith("tool_schemas.mcp."):
            continue
        server = key.split(".", 2)[2]
        if schema_tokens / input_side < UNUSED_MCP_MIN_SCHEMA_SHARE:
            continue
        results_tokens = sum(
            v for k, v in buckets.items()
            if k == f"tool_results.{server}"
        )
        if results_tokens > 0:
            continue
        pct = schema_tokens / week_total * 100
        out.append({
            "fingerprint": f"mcp:{server}",
            "title": f"Disable unused MCP server: {server}",
            "category": key,
            "est_savings_pct": pct,
            "risk": "low",
            "evidence": (
                f"{_fmt(schema_tokens)} schema tokens this week "
                f"({pct:.0f}% of total) across {agg['api_calls']} API calls — "
                f"0 tool results observed from this server in 7 days"
            ),
            "risk_note": (
                "If a workflow later needs this server, re-enable it with "
                "`hermes mcp enable` — nothing is deleted."
            ),
            "plan": [
                f"Run: hermes mcp disable {server}",
                "Restart any running gateway/CLI session (hooks and schemas load at process start)",
                "After ~5 sessions, re-check Token Lens — the MCP schemas share should drop",
            ],
        })

    # 2. Low cache hit rate.
    prompt_side = totals["input"] + totals["cache_read"] + totals["cache_write"]
    if prompt_side > 0 and agg["api_calls"] >= LOW_CACHE_MIN_CALLS:
        hit = totals["cache_read"] / prompt_side
        if hit < LOW_CACHE_HIT_THRESHOLD:
            # Realistic upside: raising hit toward ~60% reprices misses; the
            # honest token-count framing is "fewer uncached prompt tokens".
            potential = (0.60 - hit) * prompt_side
            pct = potential / week_total * 100
            out.append({
                "fingerprint": "config:cache-prefix",
                "title": "Cache hit rate is low — stabilize your prompt prefix",
                "category": "system_prompt",
                "est_savings_pct": min(pct, 35.0),
                "risk": "low",
                "evidence": (
                    f"Cache hit rate {hit * 100:.0f}% over {agg['api_calls']} calls "
                    f"(threshold {LOW_CACHE_HIT_THRESHOLD * 100:.0f}%) — "
                    f"{_fmt(prompt_side)} prompt-side tokens this week"
                ),
                "risk_note": (
                    "Reordering context is invisible to the model's behavior; "
                    "the risk is mainly that volatile content (timestamps, dynamic "
                    "status) re-enters the prefix and undoes the gain."
                ),
                "plan": [
                    "Open the dashboard → Config and review what changes per turn near the top of your system prompt",
                    "Move volatile content (timestamps, dynamic injections) AFTER stable content (identity, skills index)",
                    "Re-check the cache-hit KPI on Token Lens after ~5 sessions",
                ],
            })

    # 3. Oversized system prompt (incl. skills index).
    sys_tokens = buckets.get("system_prompt", 0) + buckets.get("skill_loading", 0)
    if input_side > 0:
        share = sys_tokens / input_side
        if share >= OVERSIZED_PROMPT_SHARE:
            pct = (share - 0.15) * input_side / week_total * 100  # target ~15%
            out.append({
                "fingerprint": "config:system-prompt",
                "title": "System prompt + skills index is oversized",
                "category": "system_prompt",
                "est_savings_pct": max(pct, 1.0),
                "risk": "medium",
                "evidence": (
                    f"System prompt + skills consume {share * 100:.0f}% of input "
                    f"tokens ({_fmt(sys_tokens)} this week; resent on every call)"
                ),
                "risk_note": (
                    "Removing skills or trimming AGENTS.md changes what the agent "
                    "knows by default — review each removal; roll back any skill "
                    "the agent stops finding."
                ),
                "plan": [
                    "Run: hermes plugins  (review enabled plugins/skills you no longer use)",
                    "Open dashboard → Skills and disable unused skills (each removes its index entry from every call)",
                    "Trim long static sections of AGENTS.md / SOUL.md where duplicated by skills",
                    "Re-check the system prompt share on Token Lens after ~5 sessions",
                ],
            })

    # 4. Runaway turn counts.
    if agg["sessions"] > 0:
        avg_calls = agg["api_calls"] / agg["sessions"]
        if avg_calls >= RUNAWAY_TURNS_AVG_CALLS:
            # Each call re-sends the growing context; trimming late-session
            # exploration saves roughly the marginal calls' input share.
            pct = min(25.0, (avg_calls - RUNAWAY_TURNS_AVG_CALLS) / avg_calls * 50)
            out.append({
                "fingerprint": "behavior:turn-cap",
                "title": "Sessions run long — cap exploratory turns",
                "category": "history.assistant",
                "est_savings_pct": max(pct, 2.0),
                "risk": "medium",
                "evidence": (
                    f"Average {avg_calls:.0f} API calls per session across "
                    f"{agg['sessions']} sessions (each call re-sends the full context)"
                ),
                "risk_note": (
                    "Aggressive caps can cut off multi-step work — prefer asking "
                    "the agent to checkpoint and continue in a fresh session."
                ),
                "plan": [
                    "For long tasks, ask the agent to summarize state and continue in a new session (fresh, smaller context)",
                    "Review sessions with the highest call counts in dashboard → Sessions for loops worth interrupting earlier",
                ],
            })

    # 5. Tool results dominate the context.
    tool_res = sum(v for k, v in buckets.items() if k.startswith("tool_results"))
    if input_side > 0 and tool_res / input_side >= HEAVY_TOOL_RESULTS_SHARE:
        share = tool_res / input_side
        pct = (share - 0.25) * input_side / week_total * 100
        out.append({
            "fingerprint": "behavior:tool-results",
            "title": "Tool results dominate your context",
            "category": "tool_results",
            "est_savings_pct": max(pct, 2.0),
            "risk": "medium",
            "evidence": (
                f"Tool results are {share * 100:.0f}% of input tokens "
                f"({_fmt(tool_res)} this week) — large outputs are carried in "
                f"context for the rest of each session"
            ),
            "risk_note": (
                "Truncating or summarizing tool output can drop details the "
                "agent needed later in the session."
            ),
            "plan": [
                "Prefer targeted reads (offsets/limits, search-first) over whole-file or whole-page tool calls",
                "Ask the agent to summarize bulky tool results it only needs conclusions from",
                "Re-check the tool-results share on Token Lens after ~5 sessions",
            ],
        })

    return out


def run_detectors(conn) -> int:
    """Run all detectors once per watermark. Returns suggestions inserted.

    Caller has already passed the detector gate. begin_suggestion_run's
    UNIQUE(kind, watermark) makes this idempotent per session count — a
    double-claimed refresh cannot double-insert (plan §refresh_requests).
    """
    agg = _window_aggregates(conn)
    if agg is None:
        return 0
    with core.write_txn(conn):
        run_id = core.begin_suggestion_run(conn, kind="detector")
    if run_id is None:
        return 0  # this watermark already ran
    inserted = 0
    for cand in _candidates(agg):
        plan_md = "\n".join(
            f"{i + 1}. {step}" for i, step in enumerate(cand["plan"])
        )
        evidence = cand["evidence"] + "\nCapability risk: " + cand["risk_note"]
        with core.write_txn(conn):
            core.insert_suggestion(
                conn,
                run_id=run_id,
                fingerprint=cand["fingerprint"],
                title=cand["title"],
                evidence=evidence,
                plan_md=plan_md,
                category=cand["category"],
                est_savings_pct=round(float(cand["est_savings_pct"]), 1),
                risk=cand["risk"],
                kind="detector",
            )
        inserted += 1
    with core.write_txn(conn):
        conn.execute(
            "UPDATE suggestion_runs SET purpose_breakdown_json=? WHERE id=?",
            (json.dumps({"detector_candidates": inserted}), run_id),
        )
    return inserted
