"""Detectors: each rule fires on its signal and stays quiet without it;
plans reference real command surfaces; watermark idempotency; inheritance."""
import json
import time

import detectors
import token_lens_core as core


def _seed_rollup(conn, session_id, buckets, *, totals=None, api_calls=10):
    base_totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                   "reasoning": 0, "billed": 0}
    base_totals.update(totals or {})
    if not base_totals["billed"]:
        base_totals["billed"] = sum(buckets.values())
    with core.write_txn(conn):
        conn.execute(
            "INSERT OR REPLACE INTO session_rollups (session_id, analyzed_at,"
            " analyzer_version, rules_version, precision, provenance, totals_json,"
            " buckets_json, api_calls, turns, started_ts, ended_ts)"
            " VALUES (?, ?, 1, 1, 'exact', 'recorder', ?, ?, ?, 5, ?, ?)",
            (session_id, time.time(), json.dumps(base_totals),
             json.dumps(buckets), api_calls, time.time() - 60, time.time()),
        )


def _suggestions(conn):
    return conn.execute(
        "SELECT * FROM suggestions ORDER BY id"
    ).fetchall()


def test_unused_mcp_server_detected(db):
    buckets = {
        "tool_schemas.mcp.browser-tools": 50_000,
        "history.user": 100_000,
        "output": 20_000,
    }
    for i in range(3):
        _seed_rollup(db, f"s{i}", buckets)
    assert detectors.run_detectors(db) >= 1
    rows = [r for r in _suggestions(db) if r["fingerprint"] == "mcp_disable:browser-tools"]
    assert len(rows) == 1
    assert "hermes mcp disable browser-tools" in rows[0]["plan_md"]
    assert "0 tool results" in rows[0]["evidence"]
    assert rows[0]["status"] == "shown"


def test_used_mcp_server_not_flagged(db):
    buckets = {
        "tool_schemas.mcp.browser-tools": 50_000,
        "tool_results.browser-tools": 9_000,  # it IS used
        "history.user": 100_000,
    }
    _seed_rollup(db, "s1", buckets)
    detectors.run_detectors(db)
    assert not [r for r in _suggestions(db) if r["fingerprint"].startswith("mcp_disable")]


def test_low_cache_hit_detected(db):
    buckets = {"system_prompt": 500_000, "history.user": 400_000, "output": 50_000}
    totals = {"input": 900_000, "cache_read": 45_000, "billed": 995_000}
    _seed_rollup(db, "s1", buckets, totals=totals, api_calls=40)
    detectors.run_detectors(db)
    rows = [r for r in _suggestions(db) if r["fingerprint"] == "cache_prefix"]
    assert len(rows) == 1
    assert "%" in rows[0]["evidence"]


def test_high_cache_hit_not_flagged(db):
    buckets = {"system_prompt": 500_000, "output": 50_000}
    totals = {"input": 200_000, "cache_read": 700_000, "billed": 950_000}
    _seed_rollup(db, "s1", buckets, totals=totals, api_calls=40)
    detectors.run_detectors(db)
    assert not [r for r in _suggestions(db) if r["fingerprint"] == "cache_prefix"]


def test_oversized_system_prompt_detected(db):
    buckets = {"system_prompt": 200_000, "skill_loading": 150_000,
               "history.user": 400_000, "output": 30_000}
    _seed_rollup(db, "s1", buckets)
    detectors.run_detectors(db)
    rows = [r for r in _suggestions(db) if r["fingerprint"] == "system_prompt_trim"]
    assert len(rows) == 1
    assert rows[0]["risk"] == "medium"


def test_runaway_turns_detected(db):
    buckets = {"history.user": 100_000, "output": 10_000}
    _seed_rollup(db, "s1", buckets, api_calls=80)
    detectors.run_detectors(db)
    assert [r for r in _suggestions(db) if r["fingerprint"] == "turn_cap"]


def test_quiet_baseline_produces_nothing(db):
    # healthy: good cache, small prompt, modest calls, used servers
    buckets = {"system_prompt": 50_000, "history.user": 400_000,
               "tool_results": 100_000, "output": 60_000}
    totals = {"input": 150_000, "cache_read": 420_000, "billed": 630_000}
    _seed_rollup(db, "s1", buckets, totals=totals, api_calls=12)
    detectors.run_detectors(db)
    assert _suggestions(db) == []


def test_watermark_makes_rerun_noop(db):
    buckets = {"tool_schemas.mcp.x": 50_000, "history.user": 100_000}
    _seed_rollup(db, "s1", buckets)
    first = detectors.run_detectors(db)
    again = detectors.run_detectors(db)  # same session count -> same watermark
    assert first >= 1 and again == 0
    assert len(_suggestions(db)) == first


def test_regenerated_finding_inherits_dismissal(db):
    buckets = {"tool_schemas.mcp.x": 50_000, "history.user": 100_000}
    _seed_rollup(db, "s1", buckets)
    detectors.run_detectors(db)
    row = _suggestions(db)[0]
    with core.write_txn(db):
        core.set_suggestion_status(db, row["id"], "dismissed")
    _seed_rollup(db, "s2", buckets)  # new session -> new watermark
    detectors.run_detectors(db)
    newest = _suggestions(db)[-1]
    assert newest["fingerprint"] == row["fingerprint"]
    assert newest["status"] == "dismissed"  # same evidence scale: stays dismissed
