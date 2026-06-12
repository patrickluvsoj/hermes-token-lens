"""Rollups: idempotent upsert, ±2% reconciliation precision, sweep."""
import json
import time

import token_lens_core as core


def _call(conn, session_id, rid, billed=1000, status="complete"):
    with core.write_txn(conn):
        core.upsert_pre_call(
            conn, api_request_id=rid, session_id=session_id, turn_id=rid.split(":")[0],
            ts=time.time(), model="m", provider="p", request_hash="h",
            buckets={"system_prompt": 100, "history.user": 100},
        )
        if status == "complete":
            core.complete_post_call(
                conn, api_request_id=rid,
                usage={"input_tokens": billed, "prompt_tokens": billed,
                       "output_tokens": 10},
            )
        elif status == "no_usage":
            core.complete_post_call(conn, api_request_id=rid, usage=None)


def test_rollup_aggregates_and_is_idempotent(db):
    _call(db, "s1", "t1:api:1")
    _call(db, "s1", "t2:api:2")
    with core.write_txn(db):
        payload = core.rollup_session(db, "s1")
    assert payload is not None
    assert payload["api_calls"] == 2
    assert payload["turns"] == 2
    # second call under same versions: no-op
    with core.write_txn(db):
        assert core.rollup_session(db, "s1") is None
    assert db.execute("SELECT COUNT(*) FROM session_rollups").fetchone()[0] == 1


def test_incomplete_calls_excluded(db):
    with core.write_txn(db):
        core.upsert_pre_call(
            db, api_request_id="t1:api:1", session_id="s2", turn_id="t1",
            ts=time.time(), model="m", provider="p", request_hash="h",
            buckets={"system_prompt": 100},
        )  # never paired -> stays incomplete
    with core.write_txn(db):
        assert core.rollup_session(db, "s2") is None


def test_no_usage_call_downgrades_precision(db):
    _call(db, "s3", "t1:api:1")
    _call(db, "s3", "t2:api:2", status="no_usage")
    with core.write_txn(db):
        payload = core.rollup_session(db, "s3")
    assert payload["precision"] == "estimated"


def test_reconciliation_within_2pct_is_exact(db):
    _call(db, "s4", "t1:api:1", billed=1000)
    with core.write_txn(db):
        p = core.rollup_session(db, "s4", session_totals={"total_tokens": 1015})
    assert p["precision"] == "exact"  # 1010 billed vs 1015 core ≈ 0.5%


def test_reconciliation_beyond_2pct_is_estimated(db):
    _call(db, "s5", "t1:api:1", billed=1000)
    with core.write_txn(db):
        p = core.rollup_session(db, "s5", session_totals={"total_tokens": 1500})
    assert p["precision"] == "estimated"


def test_backfill_provenance_always_estimated(db):
    _call(db, "s6", "t1:api:1")
    with core.write_txn(db):
        p = core.rollup_session(db, "s6", provenance="backfill")
    assert p["precision"] == "estimated"
    assert p["provenance"] == "backfill"


def test_sweep_finds_unanalyzed_sessions(db):
    _call(db, "s7", "t1:api:1")
    _call(db, "s8", "t2:api:1")
    assert core.sweep_unanalyzed(db) == 2
    assert core.sweep_unanalyzed(db) == 0  # nothing left


def test_sweep_reanalyzes_on_rules_bump(db):
    _call(db, "s9", "t1:api:1")
    assert core.sweep_unanalyzed(db) == 1
    with core.write_txn(db):
        db.execute(
            "INSERT INTO category_rules (version, rules_json, rationale, created_at)"
            " VALUES (2, ?, 'test bump', ?)",
            (json.dumps(core.DEFAULT_RULES), time.time()),
        )
    assert core.sweep_unanalyzed(db) == 1  # re-analyzed under rules v2
    row = db.execute("SELECT rules_version FROM session_rollups WHERE session_id='s9'").fetchone()
    assert row["rules_version"] == 2
