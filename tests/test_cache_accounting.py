"""Issue #4 — cache accounting.

`prompt_tokens` from the host's canonical usage summary is INCLUSIVE
(input + cache_read + cache_write); `input_tokens` is uncached-only. The
recorder must prefer the exclusive field, and a fully cached call
(input_tokens == 0) must not fall through to the inclusive figure.
Also covers the v2 -> v3 migration that repairs already-recorded rows.
"""
import json
import sqlite3

import token_lens_core as core


def _record(conn, api_request_id, usage, buckets=None):
    with core.write_txn(conn):
        core.upsert_pre_call(
            conn,
            api_request_id=api_request_id,
            session_id="s",
            turn_id="t",
            ts=1.0,
            model="m",
            provider="p",
            request_hash="h",
            buckets=buckets or {"tool_results": 1000},
        )
        core.complete_post_call(conn, api_request_id=api_request_id, usage=usage)


def test_prefers_exclusive_input_tokens(db):
    # host shape: input_tokens=39 uncached, prompt_tokens=27303 inclusive,
    # 27264 served from cache — exactly the wire numbers from issue #4.
    _record(db, "x:1", {
        "input_tokens": 39, "prompt_tokens": 27303,
        "cache_read_tokens": 27264, "output_tokens": 8,
    })
    row = db.execute("SELECT * FROM api_calls WHERE api_request_id='x:1'").fetchone()
    assert row["actual_input"] == 39
    assert row["actual_cache_read"] == 27264
    buckets = json.loads(row["buckets_json"])
    # billed prompt side = 39 + 27264 = 27303, not 54567
    input_side = sum(v for k, v in buckets.items() if k not in ("output", "reasoning"))
    assert abs(input_side - 27303) < 5


def test_fully_cached_call_records_zero_uncached(db):
    # input_tokens == 0 is legitimate (everything cached); truthiness-based
    # fallback would resurrect the double count.
    _record(db, "x:2", {
        "input_tokens": 0, "prompt_tokens": 5000,
        "cache_read_tokens": 5000, "output_tokens": 3,
    })
    row = db.execute("SELECT * FROM api_calls WHERE api_request_id='x:2'").fetchone()
    assert row["actual_input"] == 0
    assert row["actual_cache_read"] == 5000


def test_legacy_usage_without_input_tokens_still_records(db):
    # Old hosts that send only prompt_tokens keep the documented fallback.
    _record(db, "x:3", {"prompt_tokens": 700, "cache_read_tokens": 200, "output_tokens": 4})
    row = db.execute("SELECT * FROM api_calls WHERE api_request_id='x:3'").fetchone()
    assert row["actual_input"] == 700
    assert row["actual_cache_read"] == 200


def _build_v2_db(path):
    raw = sqlite3.connect(str(path))
    raw.executescript(core._MIGRATIONS[1])
    raw.executescript(core._MIGRATIONS[2])
    raw.execute("PRAGMA user_version=2")
    # buggy row: uncached=100, cr=1000 -> recorded actual_input=1100 (inclusive),
    # buckets calibrated to billed=2100
    raw.execute(
        "INSERT INTO api_calls (api_request_id, session_id, ts, model, provider,"
        " actual_input, actual_output, actual_cache_read, actual_cache_write,"
        " calib_scale, buckets_json, status)"
        " VALUES ('bug:1','s1',1.0,'m','p',1100,5,1000,0,2.1,"
        " ?, 'complete')",
        (json.dumps({"tool_results": 2100}),),
    )
    raw.execute(
        "INSERT INTO api_calls (api_request_id, session_id, ts, model, provider,"
        " actual_input, actual_output, actual_cache_read, actual_cache_write,"
        " calib_scale, buckets_json, status)"
        " VALUES ('cold:1','s1',2.0,'m','p',500,5,0,0,1.0,"
        " ?, 'complete')",
        (json.dumps({"tool_results": 500}),),
    )
    raw.execute(
        "INSERT INTO session_rollups (session_id, analyzed_at, analyzer_version,"
        " rules_version, precision, provenance, totals_json, buckets_json,"
        " api_calls, turns, started_ts, ended_ts)"
        " VALUES ('s1', 3.0, 1, 1, 'exact', 'recorder', ?, ?, 2, 2, 1.0, 2.0)",
        (
            json.dumps({"input": 1600, "output": 10, "cache_read": 1000,
                        "cache_write": 0, "reasoning": 0, "billed": 2600}),
            json.dumps({"tool_results": 2600}),
        ),
    )
    raw.commit()
    raw.close()


def test_v2_rows_rescaled_on_upgrade(db_path):
    _build_v2_db(db_path)
    conn = core.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3

    row = conn.execute("SELECT * FROM api_calls WHERE api_request_id='bug:1'").fetchone()
    assert row["actual_input"] == 100                      # 1100 - 1000
    buckets = json.loads(row["buckets_json"])
    assert abs(buckets["tool_results"] - 1100) < 5         # 2100 * 1100/2100
    assert abs(row["calib_scale"] - 2.1 * (1100 / 2100)) < 1e-6

    # zero-cache rows are already correct and must not move
    cold = conn.execute("SELECT * FROM api_calls WHERE api_request_id='cold:1'").fetchone()
    assert cold["actual_input"] == 500
    assert json.loads(cold["buckets_json"])["tool_results"] == 500

    rollup = conn.execute("SELECT * FROM session_rollups WHERE session_id='s1'").fetchone()
    totals = json.loads(rollup["totals_json"])
    assert totals["input"] == 600                          # 1600 - 1000
    assert totals["billed"] == 1600                        # 2600 - 1000
    assert abs(json.loads(rollup["buckets_json"])["tool_results"] - 1600) < 5
    conn.close()


def test_rescale_runs_once(db_path):
    _build_v2_db(db_path)
    conn = core.connect(db_path)
    conn.close()
    conn = core.connect(db_path)  # reopen: marker must prevent a second pass
    row = conn.execute("SELECT actual_input FROM api_calls WHERE api_request_id='bug:1'").fetchone()
    assert row["actual_input"] == 100
    conn.close()
