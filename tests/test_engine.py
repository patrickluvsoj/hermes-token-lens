"""M2 engine: generation→evaluation pipeline with a fake host LLM, threshold
hiding, meta budget abort, watermark, inheritance interplay."""
import json
import time
from types import SimpleNamespace

import engine
import token_lens_core as core


class FakeLlm:
    """Stands in for agent.plugin_llm.PluginLlm. Scripted responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        parsed, usage_in, usage_out = item
        return SimpleNamespace(
            text=json.dumps(parsed), provider="fake", model="fake-1",
            agent_id="", parsed=parsed, content_type="json",
            usage=SimpleNamespace(input_tokens=usage_in, output_tokens=usage_out,
                                  total_tokens=usage_in + usage_out,
                                  cache_read_tokens=0, cache_write_tokens=0,
                                  cost_usd=None),
            audit={},
        )


CFG = {"meta_budget_tokens": 50000, "score_threshold": 6, "evolve_rules": True,
       "min_sessions": 10, "refresh_every": 5}


def _seed_sessions(conn, n=10):
    with core.write_txn(conn):
        for i in range(n):
            conn.execute(
                "INSERT OR REPLACE INTO session_rollups (session_id, analyzed_at,"
                " analyzer_version, rules_version, precision, provenance, totals_json,"
                " buckets_json, api_calls, turns, started_ts, ended_ts)"
                " VALUES (?, ?, 1, 1, 'exact', 'recorder', ?, ?, 12, 6, ?, ?)",
                (f"llm-{i}", time.time(),
                 json.dumps({"input": 80_000, "output": 20_000, "cache_read": 10_000,
                             "cache_write": 0, "reasoning": 0, "billed": 110_000}),
                 json.dumps({"system_prompt": 30_000, "history.user": 50_000,
                             "output": 20_000}),
                 time.time() - 120, time.time() - 60),
            )


def _cand(title="Trim the skills index", target="config:skills-index",
          savings=8.0, risk="low"):
    return {"title": title, "target": target, "category": "skill_loading",
            "evidence": "skills are 27% of input per AGGREGATES",
            "est_savings_pct": savings, "risk": risk,
            "risk_note": "agent may stop discovering a removed skill",
            "plan_steps": ["Open dashboard → Skills", "Disable unused skills",
                           "Re-check Token Lens after ~5 sessions"]}


def test_full_run_shows_passing_and_hides_failing(db):
    _seed_sessions(db)
    llm = FakeLlm([
        ({"suggestions": [_cand(), _cand(title="Vague idea", target="misc:vibes")]},
         3000, 500),
        ({"scores": [
            {"index": 0, "total": 8, "verdict_note": "solid"},
            {"index": 1, "total": 3, "verdict_note": "no evidence"},
        ]}, 1000, 200),
    ])
    result = engine.run_llm_refresh(db, llm, CFG)
    assert result["status"] == "done"
    assert result["shown"] == 1 and result["hidden"] == 1
    rows = db.execute("SELECT title, status, kind, scores_json FROM suggestions ORDER BY id").fetchall()
    assert rows[0]["status"] == "shown" and rows[0]["kind"] == "llm"
    assert rows[1]["status"] == "hidden"
    assert json.loads(rows[0]["scores_json"])["total"] == 8


def test_meta_ledger_records_usage_and_purposes(db):
    _seed_sessions(db)
    llm = FakeLlm([
        ({"suggestions": [_cand()]}, 3000, 500),
        ({"scores": [{"index": 0, "total": 9}]}, 1000, 200),
    ])
    engine.run_llm_refresh(db, llm, CFG)
    run = db.execute("SELECT * FROM suggestion_runs WHERE kind='llm'").fetchone()
    assert run["tokens_input"] == 4000 and run["tokens_output"] == 700
    purposes = json.loads(run["purpose_breakdown_json"])
    assert set(purposes) == {"token-lens.suggest", "token-lens.evaluate"}
    assert run["rubric_version"] == 1
    assert run["model"] == "fake-1"


def test_budget_abort_skips_evaluation_and_shows_nothing(db):
    _seed_sessions(db)
    llm = FakeLlm([
        ({"suggestions": [_cand()]}, 60000, 5000),  # generation alone blows 50k
    ])
    result = engine.run_llm_refresh(db, llm, CFG)
    assert result["status"] == "skipped"
    assert "budget" in result["reason"]
    assert len(llm.calls) == 1  # evaluator never called — no further spend
    assert db.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0] == 0


def test_llm_failure_marks_skipped_with_reason(db):
    _seed_sessions(db)
    llm = FakeLlm([RuntimeError("provider down")])
    result = engine.run_llm_refresh(db, llm, CFG)
    assert result["status"] == "skipped"
    assert "provider down" in result["reason"]


def test_empty_candidates_is_a_valid_done(db):
    _seed_sessions(db)
    llm = FakeLlm([({"suggestions": []}, 2000, 100)])
    result = engine.run_llm_refresh(db, llm, CFG)
    assert result["status"] == "done"
    assert "clean bill" in result["reason"]
    assert len(llm.calls) == 1  # no evaluator call for zero candidates


def test_watermark_blocks_second_run_at_same_count(db):
    _seed_sessions(db)
    llm = FakeLlm([
        ({"suggestions": []}, 100, 10),
    ])
    assert engine.run_llm_refresh(db, llm, CFG)["status"] == "done"
    again = engine.run_llm_refresh(db, FakeLlm([]), CFG)
    assert again["status"] == "skipped" and "watermark" in again["reason"]


def test_dismissed_llm_suggestion_inherits_on_regeneration(db):
    _seed_sessions(db)
    llm1 = FakeLlm([
        ({"suggestions": [_cand()]}, 1000, 100),
        ({"scores": [{"index": 0, "total": 9}]}, 500, 50),
    ])
    engine.run_llm_refresh(db, llm1, CFG)
    row = db.execute("SELECT id FROM suggestions").fetchone()
    with core.write_txn(db):
        core.set_suggestion_status(db, row["id"], "dismissed")
    _seed_sessions(db, n=16)  # advance the watermark
    llm2 = FakeLlm([
        ({"suggestions": [_cand()]}, 1000, 100),  # same target -> same fingerprint
        ({"scores": [{"index": 0, "total": 9}]}, 500, 50),
    ])
    result = engine.run_llm_refresh(db, llm2, CFG)
    newest = db.execute("SELECT status FROM suggestions ORDER BY id DESC LIMIT 1").fetchone()
    assert newest["status"] == "dismissed"  # same savings: stays dismissed
    assert result["shown"] == 1  # engine counted the rubric pass; status honors the user


def test_bad_category_falls_back_to_unattributed(db):
    _seed_sessions(db)
    cand = _cand()
    cand["category"] = "made_up_category"
    llm = FakeLlm([
        ({"suggestions": [cand]}, 1000, 100),
        ({"scores": [{"index": 0, "total": 9}]}, 500, 50),
    ])
    engine.run_llm_refresh(db, llm, CFG)
    row = db.execute("SELECT category FROM suggestions").fetchone()
    assert row["category"] == "unattributed"


def test_inputs_never_contain_transcripts(db):
    _seed_sessions(db)
    inputs = engine.build_inputs(db)
    blob = json.dumps(inputs)
    assert "messages" not in blob and "content" not in blob
    assert inputs["week_total_tokens"] > 0
    assert "config_snapshot" in inputs
