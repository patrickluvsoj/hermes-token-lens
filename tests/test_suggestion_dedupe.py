"""Issue #5 — duplicate suggestions.

A refresh must supersede the visible card for a fingerprint instead of
stacking another copy, and the detector gate must not re-run on every
single recorded session.
"""
import json

import token_lens_core as core


def _insert(db, fp, evidence="ev", savings=10.0, kind="detector", run_id=None):
    with core.write_txn(db):
        return core.insert_suggestion(
            db, run_id=run_id, fingerprint=fp, title=f"t:{fp}",
            evidence=evidence, plan_md="p", category="history.assistant",
            est_savings_pct=savings, risk="low", kind=kind,
        )


def test_refresh_updates_visible_row_in_place(db):
    a = _insert(db, "behavior:turn-cap", evidence="first", savings=25.0, run_id=1)
    b = _insert(db, "behavior:turn-cap", evidence="second", savings=19.9, run_id=2)
    assert b == a
    rows = db.execute(
        "SELECT * FROM suggestions WHERE fingerprint='behavior:turn-cap'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["evidence"] == "second"
    assert rows[0]["est_savings_pct"] == 19.9
    assert rows[0]["run_id"] == 2
    assert rows[0]["status"] == "shown"


def test_hidden_candidate_refreshes_to_shown(db):
    a = _insert(db, "behavior:tool-results", evidence="v1", savings=15.0)
    with core.write_txn(db):
        db.execute("UPDATE suggestions SET status='hidden' WHERE id=?", (a,))
    b = _insert(db, "behavior:tool-results", evidence="v2", savings=16.0, kind="llm")
    assert b == a
    row = db.execute("SELECT * FROM suggestions WHERE id=?", (a,)).fetchone()
    assert row["status"] == "shown"      # engine re-hides if below threshold
    assert row["kind"] == "llm"


def test_dismissal_still_blocks_new_visible_rows(db):
    a = _insert(db, "behavior:turn-cap", evidence="v1", savings=10.0)
    with core.write_txn(db):
        core.set_suggestion_status(db, a, "dismissed")
    _insert(db, "behavior:turn-cap", evidence="v2", savings=12.0)  # < 2x: stays dismissed
    shown = db.execute(
        "SELECT COUNT(*) FROM suggestions WHERE fingerprint='behavior:turn-cap'"
        " AND status='shown'"
    ).fetchone()[0]
    assert shown == 0


def _add_rollup(db, session_id):
    with core.write_txn(db):
        db.execute(
            "INSERT INTO session_rollups (session_id, analyzed_at, analyzer_version,"
            " rules_version, precision, provenance, totals_json, buckets_json,"
            " api_calls, turns, started_ts, ended_ts)"
            " VALUES (?, ?, 1, 1, 'exact', 'recorder', '{}', '{}', 1, 1, 1.0, 2.0)",
            (session_id, 1.0),
        )


def test_detector_gate_waits_for_new_sessions(db):
    for i in range(3):
        _add_rollup(db, f"s{i}")
    ok, reason = core.gate_check(
        db, kind="detector", min_sessions=3, refresh_every=3)
    assert ok, reason

    # a detector run lands at watermark 3
    with core.write_txn(db):
        assert core.begin_suggestion_run(db, kind="detector") is not None

    _add_rollup(db, "s3")  # 4 observed, 1 new session
    ok, reason = core.gate_check(db, kind="detector", min_sessions=3, refresh_every=3)
    assert not ok and "new sessions" in reason

    _add_rollup(db, "s4")
    _add_rollup(db, "s5")  # 6 observed, 3 new sessions
    ok, reason = core.gate_check(db, kind="detector", min_sessions=3, refresh_every=3)
    assert ok, reason


def test_llm_watermark_does_not_block_detector(db):
    for i in range(3):
        _add_rollup(db, f"s{i}")
    with core.write_txn(db):
        core.begin_suggestion_run(db, kind="llm")
    _add_rollup(db, "s3")
    ok, _ = core.gate_check(db, kind="detector", min_sessions=3, refresh_every=3)
    assert ok  # kinds track their own watermarks
