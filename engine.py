"""M2 suggestion engine: LLM generation + rubric evaluation + evolution.

Runs AGENT-SIDE only (the dashboard process never constructs LLM clients —
plan Constraint 5/host trust gate). The caller (recorder drain / CLI command)
checks the LLM gate first and passes the host-owned ``ctx.llm`` facade
(:class:`agent.plugin_llm.PluginLlm`).

The LLM never reads transcripts: inputs are deterministic pre-aggregates
(category totals/trends, schema cost vs usage, cache stats, turn stats,
config snapshot incl. tool-search state). All LLM-dependent paths fail open —
if ``ctx.llm`` is unavailable or errors, deterministic detectors still work
and the refresh is marked skipped with the reason.

Meta-cost: every call's usage lands in ``suggestion_runs``
(tokens_input/tokens_output, purpose breakdown); ``meta_budget_tokens``
hard-caps a refresh (generation that exceeds the budget skips evaluation and
hides its output rather than spending more).
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from . import token_lens_core as core
except ImportError:  # pragma: no cover — top-level module load
    import token_lens_core as core  # type: ignore

PLUGIN_ROOT = Path(__file__).resolve().parent
GUIDELINES_PATH = PLUGIN_ROOT / "suggestion-guidelines.md"
RUBRIC_PATH = PLUGIN_ROOT / "rubric.md"

GENERATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "target": {"type": "string"},
                    "category": {"type": "string"},
                    "evidence": {"type": "string"},
                    "est_savings_pct": {"type": "number"},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                    "risk_note": {"type": "string"},
                    "plan_steps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "target", "category", "evidence",
                             "est_savings_pct", "risk", "plan_steps"],
            },
        },
        "rule_proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["system_block"]},
                    "category": {"type": "string"},
                    "pattern": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["kind", "category", "pattern", "rationale"],
            },
        },
    },
    "required": ["suggestions"],
}

EVALUATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "usefulness": {"type": "number"},
                    "specificity": {"type": "number"},
                    "savings_credibility": {"type": "number"},
                    "capability_risk_honesty": {"type": "number"},
                    "actionability": {"type": "number"},
                    "total": {"type": "number"},
                    "verdict_note": {"type": "string"},
                },
                "required": ["index", "total"],
            },
        },
        "rubric_amendment": {
            "type": "object",
            "properties": {
                "rationale": {"type": "string"},
                "rubric_md": {"type": "string"},
            },
            "required": ["rationale", "rubric_md"],
        },
    },
    "required": ["scores"],
}


# ---------------------------------------------------------------------------
# Deterministic inputs (the LLM never reads transcripts)
# ---------------------------------------------------------------------------

def build_inputs(conn) -> Dict[str, Any]:
    week_ago = time.time() - 7 * 86400
    rows = conn.execute(
        "SELECT * FROM session_rollups WHERE provenance='recorder' AND ended_ts >= ?",
        (week_ago,),
    ).fetchall()
    buckets, totals, est_share = core.aggregate_buckets(rows)
    sessions = len(rows)
    api_calls = sum(r["api_calls"] for r in rows)
    prompt_side = totals["input"] + totals["cache_read"] + totals["cache_write"]

    top_sessions = conn.execute(
        "SELECT session_id, api_calls, totals_json FROM session_rollups "
        "WHERE provenance='recorder' AND ended_ts >= ? "
        "ORDER BY json_extract(totals_json, '$.billed') DESC LIMIT 5",
        (week_ago,),
    ).fetchall()

    return {
        "window": "trailing_7d",
        "week_total_tokens": totals["billed"],
        "sessions": sessions,
        "api_calls": api_calls,
        "avg_api_calls_per_session": round(api_calls / sessions, 1) if sessions else 0,
        "cache_hit_rate": round(totals["cache_read"] / prompt_side, 3) if prompt_side else 0,
        "estimated_share_pct": est_share,
        "category_tokens": {k: round(v) for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])},
        "top_sessions": [
            {"api_calls": r["api_calls"],
             "billed": json.loads(r["totals_json"]).get("billed", 0)}
            for r in top_sessions
        ],
        "config_snapshot": _config_snapshot(),
        # evidence rule: exact-path aggregates only (plan §Backfill)
        "note": "all aggregates are recorder-observed (exact path); backfilled history excluded",
    }


def _config_snapshot() -> Dict[str, Any]:
    """Enabled MCP servers / plugins / tool-search state — best-effort, so the
    LLM targets residual waste and never recommends already-active deferral."""
    snap: Dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config  # type: ignore
        cfg = load_config() or {}
        snap["tool_search"] = (cfg.get("tool_search")
                               or (cfg.get("tools") or {}).get("tool_search")
                               or "auto (default — MCP/plugin schemas deferred)")
        mcp = cfg.get("mcp") or {}
        servers = mcp.get("servers") if isinstance(mcp, dict) else None
        if isinstance(servers, dict):
            snap["mcp_servers_enabled"] = sorted(servers.keys())
        plugins = (cfg.get("plugins") or {}).get("enabled")
        if isinstance(plugins, list):
            snap["plugins_enabled"] = sorted(str(p) for p in plugins)
    except Exception:
        snap["unavailable"] = True
    return snap


# ---------------------------------------------------------------------------
# Rubric versioning
# ---------------------------------------------------------------------------

RUBRIC_MAX_CRITERIA = 7
RUBRIC_SCALE = 10


def active_rubric(conn) -> Tuple[int, str]:
    """Latest rubric version, seeding v1 from rubric.md on first use."""
    row = conn.execute(
        "SELECT version, rubric_md FROM rubric_versions ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is not None:
        return int(row["version"]), row["rubric_md"]
    try:
        md = RUBRIC_PATH.read_text(encoding="utf-8")
    except Exception:
        md = "Score 0-10 on usefulness, specificity, credibility, risk honesty, actionability."
    with core.write_txn(conn):
        conn.execute(
            "INSERT OR IGNORE INTO rubric_versions (version, rubric_md, rationale, created_at)"
            " VALUES (1, ?, 'v1 shipped with the plugin', ?)",
            (md, time.time()),
        )
    return 1, md


def _count_rubric_criteria(rubric_md: str) -> int:
    """Criteria = scored table rows ('| name | 0–N |')."""
    return len(re.findall(r"^\|[^|]+\|\s*0\s*[–-]\s*\d+\s*\|", rubric_md, re.M))


def apply_rubric_amendment(conn, *, rubric_md: str, rationale: str) -> Optional[int]:
    """Auto-apply an evaluator-proposed amendment under hard guardrails
    (≤7 criteria, scale text intact, threshold never self-modifiable —
    the threshold lives in config, which this function never touches).
    Returns the new version, or None if rejected."""
    if not rubric_md.strip() or not rationale.strip():
        return None
    if _count_rubric_criteria(rubric_md) > RUBRIC_MAX_CRITERIA:
        logger.warning("token-lens: rubric amendment rejected (>%d criteria)", RUBRIC_MAX_CRITERIA)
        return None
    if "score_threshold" in rubric_md and "never self-modifiable" not in rubric_md:
        logger.warning("token-lens: rubric amendment rejected (touches score_threshold)")
        return None
    version, _ = active_rubric(conn)
    new_version = version + 1
    with core.write_txn(conn):
        conn.execute(
            "INSERT INTO rubric_versions (version, rubric_md, rationale, created_at)"
            " VALUES (?, ?, ?, ?)",
            (new_version, rubric_md, rationale, time.time()),
        )
    log_evolution(
        kind="rubric",
        old_version=version,
        new_version=new_version,
        what="rubric amendment (evaluator-proposed, auto-applied)",
        rationale=rationale,
        impact="prior scores never rewritten; new runs scored under the new version",
    )
    return new_version


# ---------------------------------------------------------------------------
# Rules evolution (category mapping rules — IDs frozen, rules versioned)
# ---------------------------------------------------------------------------

def apply_rule_proposal(conn, proposal: Dict[str, Any]) -> Optional[int]:
    """Auto-apply an LLM-proposed mapping-rule change as a new
    category_rules version (user decision D16: auto-approve, always logged).
    Rejects new top-level ids and invalid regexes. Rollups recompute lazily
    via the sweep (idempotency key includes rules_version)."""
    category = str(proposal.get("category", ""))
    pattern = str(proposal.get("pattern", ""))
    rationale = str(proposal.get("rationale", ""))
    if category not in core.CATEGORY_IDS:
        logger.warning("token-lens: rule proposal rejected (unknown category %r)", category)
        return None
    try:
        re.compile(pattern)
    except re.error as exc:
        logger.warning("token-lens: rule proposal rejected (bad regex: %s)", exc)
        return None
    version, rules = core.load_rules(conn)
    new_rules = json.loads(json.dumps(rules))  # deep copy
    blocks = new_rules.setdefault("system_blocks", [])
    if any(b.get("pattern") == pattern for b in blocks):
        return None  # duplicate
    blocks.append({"category": category, "pattern": pattern})
    new_version = version + 1
    with core.write_txn(conn):
        conn.execute(
            "INSERT INTO category_rules (version, rules_json, rationale, created_at)"
            " VALUES (?, ?, ?, ?)",
            (new_version, json.dumps(new_rules), rationale, time.time()),
        )
    log_evolution(
        kind="rules",
        old_version=version,
        new_version=new_version,
        what=f"new system-block rule -> {category}: /{pattern}/",
        rationale=rationale,
        impact="historical rollups recompute lazily under the new version (trend lines stay comparable)",
    )
    return new_version


def evolution_log_path() -> Path:
    return core.default_db_path().parent / "token_lens.EVOLUTION.md"


def log_evolution(*, kind: str, old_version: int, new_version: int,
                  what: str, rationale: str, impact: str) -> None:
    """Human-readable, append-only evolution log (eng review D16: every
    auto-applied change is logged, never silent)."""
    path = evolution_log_path()
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    entry = (
        f"\n## {stamp} — {kind} v{old_version} → v{new_version}\n\n"
        f"- **What:** {what}\n"
        f"- **Why:** {rationale}\n"
        f"- **Impact:** {impact}\n"
    )
    try:
        if not path.exists():
            path.write_text(
                "# Token Lens — evolution log\n\nEvery self-applied change to "
                "category mapping rules or the evaluation rubric, in order. "
                "Auto-approved, always logged, never silent.\n",
                encoding="utf-8",
            )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
    except Exception as exc:  # pragma: no cover — log must never break a run
        logger.debug("token-lens: evolution log write failed: %s", exc)


# ---------------------------------------------------------------------------
# The refresh run
# ---------------------------------------------------------------------------

def _usage_tokens(usage: Any) -> Tuple[int, int]:
    return (int(getattr(usage, "input_tokens", 0) or 0),
            int(getattr(usage, "output_tokens", 0) or 0))


def run_llm_refresh(conn, llm, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one gated LLM suggestion run. Caller has already passed the
    kind='llm' gate and holds the host LLM facade.

    Returns {"status": "done"|"skipped", "reason", "shown", "hidden", ...}.
    """
    budget = int(cfg.get("meta_budget_tokens", 50000))
    threshold = float(cfg.get("score_threshold", 6))
    evolve = bool(cfg.get("evolve_rules", True))

    with core.write_txn(conn):
        run_id = core.begin_suggestion_run(conn, kind="llm")
    if run_id is None:
        return {"status": "skipped", "reason": "watermark already ran at this session count"}

    inputs = build_inputs(conn)
    try:
        guidelines = GUIDELINES_PATH.read_text(encoding="utf-8")
    except Exception:
        guidelines = "Generate evidence-cited token-reduction suggestions. Empty list is valid."
    rubric_version, rubric_md = active_rubric(conn)

    spent_in = spent_out = 0
    purposes: Dict[str, Dict[str, int]] = {}

    def record_usage(purpose: str, usage: Any) -> None:
        nonlocal spent_in, spent_out
        ti, to = _usage_tokens(usage)
        spent_in += ti
        spent_out += to
        purposes[purpose] = {"input": ti, "output": to}

    def finish(status: str, reason: str = "", shown: int = 0, hidden: int = 0) -> Dict[str, Any]:
        with core.write_txn(conn):
            conn.execute(
                "UPDATE suggestion_runs SET tokens_input=?, tokens_output=?,"
                " rubric_version=?, model=?, purpose_breakdown_json=? WHERE id=?",
                (spent_in, spent_out, rubric_version, model_used,
                 json.dumps(purposes), run_id),
            )
        return {"status": status, "reason": reason, "run_id": run_id,
                "shown": shown, "hidden": hidden,
                "tokens": spent_in + spent_out}

    model_used = ""

    # ---- generation (purpose=token-lens.suggest) ----------------------------
    try:
        gen = llm.complete_structured(
            instructions=(
                "Generate token-reduction suggestions for this Hermes user "
                "from the aggregates below, following the guidelines exactly. "
                "Return JSON matching the schema. An empty suggestions list "
                "is a valid answer when the data shows no clear waste."
            ),
            input=[
                {"type": "text", "text": "GUIDELINES:\n" + guidelines},
                {"type": "text", "text": "AGGREGATES:\n" + json.dumps(inputs, indent=1)},
            ],
            json_schema=GENERATION_SCHEMA,
            schema_name="token_lens_suggestions",
            max_tokens=4000,
            purpose="token-lens.suggest",
        )
        model_used = getattr(gen, "model", "") or ""
        record_usage("token-lens.suggest", getattr(gen, "usage", None))
    except Exception as exc:
        return finish("skipped", f"LLM unavailable: {exc}")

    parsed = getattr(gen, "parsed", None)
    if not isinstance(parsed, dict):
        return finish("skipped", "generation returned no valid JSON")
    candidates: List[Dict[str, Any]] = [
        c for c in parsed.get("suggestions", []) if isinstance(c, dict)
    ]

    # rule proposals: at most one per refresh cycle (plan §self-improvement)
    if evolve:
        for proposal in (parsed.get("rule_proposals") or [])[:1]:
            if isinstance(proposal, dict):
                apply_rule_proposal(conn, proposal)

    if not candidates:
        return finish("done", "no candidates (clean bill of health)")

    if spent_in + spent_out > budget:
        # Budget blown on generation alone: never spend more; nothing shows.
        return finish("skipped",
                      f"meta budget exceeded ({spent_in + spent_out} > {budget}) — refresh aborted")

    # ---- evaluation (purpose=token-lens.evaluate) ----------------------------
    scores_by_index: Dict[int, Dict[str, Any]] = {}
    try:
        ev = llm.complete_structured(
            instructions=(
                "Score each suggestion 0-10 against the rubric. Be strict: "
                "evidence must reference the aggregates; savings must be "
                "credible. Optionally propose ONE rubric amendment with a "
                "rationale (only if a scoring blind spot showed up)."
            ),
            input=[
                {"type": "text", "text": "RUBRIC (v%d):\n%s" % (rubric_version, rubric_md)},
                {"type": "text", "text": "AGGREGATES:\n" + json.dumps(inputs, indent=1)},
                {"type": "text", "text": "SUGGESTIONS:\n" + json.dumps(
                    [{k: c.get(k) for k in ("title", "target", "category", "evidence",
                                             "est_savings_pct", "risk", "plan_steps")}
                     for c in candidates], indent=1)},
            ],
            json_schema=EVALUATION_SCHEMA,
            schema_name="token_lens_scores",
            max_tokens=2000,
            purpose="token-lens.evaluate",
        )
        record_usage("token-lens.evaluate", getattr(ev, "usage", None))
        ev_parsed = getattr(ev, "parsed", None)
        if isinstance(ev_parsed, dict):
            for s in ev_parsed.get("scores", []):
                if isinstance(s, dict) and "index" in s:
                    scores_by_index[int(s["index"])] = s
            amendment = ev_parsed.get("rubric_amendment")
            if evolve and isinstance(amendment, dict):
                apply_rubric_amendment(
                    conn,
                    rubric_md=str(amendment.get("rubric_md", "")),
                    rationale=str(amendment.get("rationale", "")),
                )
    except Exception as exc:
        logger.warning("token-lens: evaluator failed (%s) — all candidates hidden", exc)

    # ---- insert (inheritance applies; unevaluated candidates stay hidden) ----
    shown = hidden = 0
    for i, cand in enumerate(candidates):
        score = scores_by_index.get(i, {})
        total = float(score.get("total", -1))
        passes = total >= threshold
        plan_md = "\n".join(f"{n + 1}. {s}" for n, s in enumerate(cand.get("plan_steps", [])))
        evidence = str(cand.get("evidence", ""))
        if cand.get("risk_note"):
            evidence += "\nCapability risk: " + str(cand["risk_note"])
        category = str(cand.get("category", "unattributed"))
        if category not in core.CATEGORY_IDS and not any(
            category.startswith(c + ".") for c in core.CATEGORY_IDS
        ):
            category = "unattributed"
        fingerprint = "llm:" + re.sub(r"[^a-z0-9:_-]+", "-",
                                      str(cand.get("target", cand.get("title", ""))).lower())
        with core.write_txn(conn):
            sid = core.insert_suggestion(
                conn, run_id=run_id, fingerprint=fingerprint,
                title=str(cand.get("title", ""))[:200],
                evidence=evidence, plan_md=plan_md, category=category,
                est_savings_pct=max(0.0, float(cand.get("est_savings_pct", 0))),
                risk=str(cand.get("risk", "medium")),
                kind="llm", scores=score,
            )
            if not passes:
                # below threshold (or unevaluated): hidden unless inheritance
                # already retired it (done/dismissed stay as inherited)
                row = conn.execute(
                    "SELECT status FROM suggestions WHERE id=?", (sid,)
                ).fetchone()
                if row["status"] == "shown":
                    conn.execute(
                        "UPDATE suggestions SET status='hidden' WHERE id=?", (sid,)
                    )
        if passes:
            shown += 1
        else:
            hidden += 1

    return finish("done", shown=shown, hidden=hidden)
