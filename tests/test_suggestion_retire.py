"""Issue #7 — suggestion lifecycle.

Findings the generator no longer emits must retire; stale shown cards must
age out. User action (done/dismissed) is never touched.
"""
import json
import time

import token_lens_core as core
import detectors


def _insert(db, fp, kind="detector", created_at=None):
    with core.write_txn(db):
        sid = core.insert_suggestion(
            db, run_id=None, fingerprint=fp, title=f"t:{fp}", evidence="ev",
            plan_md="p", category="history.assistant", est_savings_pct=10.0,
            risk="low", kind=kind,
        )
        if created_at is not None:
            db.execute("UPDATE suggestions SET created_at=? WHERE id=?",
                       (created_at, sid))
    return sid


def test_absent_fingerprint_retired(db):
    keep = _insert(db, "behavior:turn-cap")
    gone = _insert(db, "behavior:tool-results")
    with core.write_txn(db):
        n = core.retire_absent_fingerprints(
            db, kind="detector", emitted=["behavior:turn-cap"])
    assert n == 1
    assert db.execute("SELECT status FROM suggestions WHERE id=?", (keep,)).fetchone()[0] == "shown"
    assert db.execute("SELECT status FROM suggestions WHERE id=?", (gone,)).fetchone()[0] == "hidden"


def test_retire_is_kind_scoped(db):
    # distinct fingerprints per kind (one fingerprint = one visible row
    # under the supersede path, so same-fp cross-kind rows cannot coexist)
    llm_row = _insert(db, "behavior:turn-cap", kind="llm")
    det_row = _insert(db, "behavior:tool-results", kind="detector")
    with core.write_txn(db):
        core.retire_absent_fingerprints(db, kind="detector", emitted=[])
    assert db.execute("SELECT status FROM suggestions WHERE id=?", (det_row,)).fetchone()[0] == "hidden"
    assert db.execute("SELECT status FROM suggestions WHERE id=?", (llm_row,)).fetchone()[0] == "shown"


def test_user_actioned_rows_never_retired(db):
    done = _insert(db, "behavior:turn-cap")
    with core.write_txn(db):
        core.set_suggestion_status(db, done, "done")
    with core.write_txn(db):
        n = core.retire_absent_fingerprints(db, kind="detector", emitted=[])
    assert n == 0
    assert db.execute("SELECT status FROM suggestions WHERE id=?", (done,)).fetchone()[0] == "done"


def test_stale_shown_ages_out(db):
    old = _insert(db, "behavior:turn-cap", created_at=time.time() - 20 * 86400)
    fresh = _insert(db, "behavior:tool-results")
    with core.write_txn(db):
        n = core.retire_stale_shown(db, stale_after_days=14.0)
    assert n == 1
    assert db.execute("SELECT status FROM suggestions WHERE id=?", (old,)).fetchone()[0] == "hidden"
    assert db.execute("SELECT status FROM suggestions WHERE id=?", (fresh,)).fetchone()[0] == "shown"


def _seed_rollup(conn, session_id, buckets):
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
              "reasoning": 0, "billed": sum(buckets.values())}
    with core.write_txn(conn):
        conn.execute(
            "INSERT OR REPLACE INTO session_rollups (session_id, analyzed_at,"
            " analyzer_version, rules_version, precision, provenance, totals_json,"
            " buckets_json, api_calls, turns, started_ts, ended_ts)"
            " VALUES (?, ?, 1, 1, 'exact', 'recorder', ?, ?, 10, 5, ?, ?)",
            (session_id, time.time(), json.dumps(totals), json.dumps(buckets),
             time.time() - 60, time.time()),
        )


def test_run_detectors_retires_cleared_condition(db):
    # heavy tool results -> detector fires
    heavy = {"tool_results": 80_000, "history.user": 100_000}
    for i in range(3):
        _seed_rollup(db, f"s{i}", heavy)
    detectors.run_detectors(db)
    row = db.execute(
        "SELECT id, status FROM suggestions WHERE fingerprint='behavior:tool-results'"
    ).fetchone()
    assert row is not None and row["status"] == "shown"

    # condition clears (fresh window with lean tool results): re-run retires it.
    # Seed one MORE session than the first pass so the watermark differs —
    # begin_suggestion_run is UNIQUE(kind, watermark) and a same-count rerun
    # is a no-op.
    with core.write_txn(db):
        db.execute("DELETE FROM session_rollups")
    lean = {"tool_results": 10_000, "history.user": 100_000}
    for i in range(4):
        _seed_rollup(db, f"t{i}", lean)
    detectors.run_detectors(db)
    assert db.execute(
        "SELECT status FROM suggestions WHERE id=?", (row["id"],)
    ).fetchone()[0] == "hidden"
