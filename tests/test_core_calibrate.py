"""Calibration: estimated buckets rescaled to sum exactly to billed tokens."""
import json

import token_lens_core as core


def test_buckets_sum_exactly_to_billed():
    buckets = {"system_prompt": 1000, "history.user": 500, "tool_results": 1500}
    calibrated, scale = core.calibrate(buckets, 4500)
    assert scale == 4500 / 3000
    assert abs(sum(calibrated.values()) - 4500) < 1e-6


def test_zero_estimate_divide_guard():
    calibrated, scale = core.calibrate({}, 4500)
    assert scale is None
    assert calibrated == {}


def test_missing_usage_returns_uncalibrated():
    buckets = {"system_prompt": 1000}
    calibrated, scale = core.calibrate(buckets, None)
    assert scale is None
    assert calibrated == {"system_prompt": 1000.0}


def test_post_call_no_usage_marks_status(db):
    with core.write_txn(db):
        core.upsert_pre_call(
            db, api_request_id="t1:api:1", session_id="s1", turn_id="t1",
            ts=1.0, model="m", provider="p", request_hash="h",
            buckets={"system_prompt": 100},
        )
        core.complete_post_call(db, api_request_id="t1:api:1", usage=None)
    row = db.execute("SELECT * FROM api_calls WHERE api_request_id='t1:api:1'").fetchone()
    assert row["status"] == "no_usage"
    assert row["calib_scale"] is None


def test_post_call_calibrates_and_stores_scale(db):
    with core.write_txn(db):
        core.upsert_pre_call(
            db, api_request_id="t1:api:2", session_id="s1", turn_id="t1",
            ts=1.0, model="m", provider="p", request_hash="h",
            buckets={"system_prompt": 800, "history.user": 200},
        )
        core.complete_post_call(
            db, api_request_id="t1:api:2",
            usage={"input_tokens": 1000, "cache_read_tokens": 500,
                   "output_tokens": 50, "prompt_tokens": 1000},
        )
    row = db.execute("SELECT * FROM api_calls WHERE api_request_id='t1:api:2'").fetchone()
    assert row["status"] == "complete"
    buckets = json.loads(row["buckets_json"])
    input_sum = buckets["system_prompt"] + buckets["history.user"]
    assert abs(input_sum - 1500) < 1e-6  # input + cache_read = billed prompt
    assert buckets["output"] == 50.0
    assert row["calib_scale"] == 1500 / 1000


def test_drift_metric_recorded_in_scale_column(db):
    with core.write_txn(db):
        core.upsert_pre_call(
            db, api_request_id="t1:api:3", session_id="s1", turn_id="t1",
            ts=1.0, model="m", provider="p", request_hash="h",
            buckets={"system_prompt": 2000},
        )
        core.complete_post_call(
            db, api_request_id="t1:api:3",
            usage={"input_tokens": 1700, "prompt_tokens": 1700},
        )
    row = db.execute("SELECT calib_scale FROM api_calls WHERE api_request_id='t1:api:3'").fetchone()
    assert abs(row["calib_scale"] - 0.85) < 1e-9
