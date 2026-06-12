"""M3-T2: self-correcting estimator ratio — learning, convergence, clamping."""
import time

import token_lens_core as core


def _seed_calls(conn, model, scale, n=20):
    with core.write_txn(conn):
        for i in range(n):
            conn.execute(
                "INSERT INTO api_calls (api_request_id, ts, model, status,"
                " calib_scale, buckets_json) VALUES (?, ?, ?, 'complete', ?, '{}')",
                (f"{model}-{scale}-{i}-{time.time()}", time.time(), model, scale),
            )


def test_default_ratio_is_1(db):
    assert core.learned_est_ratio(db, "claude") == 1.0


def test_ratio_learns_from_median_scale(db):
    _seed_calls(db, "gpt-5.5", 0.48)  # estimator over-counts ~2x
    with core.write_txn(db):
        updated = core.update_est_ratios(db)
    assert abs(updated["gpt-5.5"] - 0.48) < 1e-9
    assert core.learned_est_ratio(db, "gpt-5.5") == updated["gpt-5.5"]


def test_multiplicative_convergence(db):
    """After correction converges, calib_scale ≈ 1.0 — the update must be a
    no-op (multiplying by ~1), NOT a reset to 1.0 (which would erase the
    learned correction)."""
    _seed_calls(db, "m", 0.5)
    with core.write_txn(db):
        core.update_est_ratios(db)
    # next window: corrected estimates -> scales now ~1.0
    with core.write_txn(db):
        db.execute("DELETE FROM api_calls")
    _seed_calls(db, "m", 1.0)
    with core.write_txn(db):
        updated = core.update_est_ratios(db)
    assert abs(updated["m"] - 0.5) < 1e-9  # ratio preserved, not erased


def test_ratio_clamped(db):
    _seed_calls(db, "weird", 0.01)
    with core.write_txn(db):
        core.update_est_ratios(db)
    assert core.learned_est_ratio(db, "weird") == core.EST_RATIO_MIN
    _seed_calls(db, "weird2", 100.0)
    with core.write_txn(db):
        core.update_est_ratios(db)
    assert core.learned_est_ratio(db, "weird2") == core.EST_RATIO_MAX


def test_too_few_calls_no_update(db):
    _seed_calls(db, "sparse", 0.5, n=core.EST_RATIO_MIN_CALLS - 1)
    with core.write_txn(db):
        assert "sparse" not in core.update_est_ratios(db)
    assert core.learned_est_ratio(db, "sparse") == 1.0


def test_apply_ratio_scales_buckets_uniformly():
    buckets = {"system_prompt": 1000, "history.user": 500}
    scaled = core.apply_est_ratio(buckets, 0.5)
    assert scaled == {"system_prompt": 500, "history.user": 250}
    assert core.apply_est_ratio(buckets, 1.0) is buckets  # no-op fast path


def test_corrected_estimates_calibrate_near_1(db):
    """End-to-end: with the ratio applied, a new call's calib_scale ≈ 1."""
    _seed_calls(db, "m2", 0.5)
    with core.write_txn(db):
        core.update_est_ratios(db)
    ratio = core.learned_est_ratio(db, "m2")
    raw = {"system_prompt": 2000}              # estimator says 2000
    corrected = core.apply_est_ratio(raw, ratio)  # -> 1000
    with core.write_txn(db):
        core.upsert_pre_call(
            db, api_request_id="t:api:99", session_id="s", turn_id="t",
            ts=time.time(), model="m2", provider="p", request_hash="h",
            buckets=corrected,
        )
        core.complete_post_call(
            db, api_request_id="t:api:99",
            usage={"input_tokens": 1000, "prompt_tokens": 1000, "output_tokens": 1},
        )
    row = db.execute(
        "SELECT calib_scale FROM api_calls WHERE api_request_id='t:api:99'"
    ).fetchone()
    assert abs(row["calib_scale"] - 1.0) < 0.01
