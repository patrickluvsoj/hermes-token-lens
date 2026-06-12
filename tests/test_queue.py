"""Refresh queue + gates: atomic claim, TTL reclaim, watermark uniqueness,
split detector/LLM gates (D9/D25)."""
import threading
import time

import token_lens_core as core


def _seed_sessions(conn, n, provenance="recorder"):
    with core.write_txn(conn):
        for i in range(n):
            conn.execute(
                "INSERT OR REPLACE INTO session_rollups (session_id, analyzed_at,"
                " analyzer_version, rules_version, precision, provenance,"
                " totals_json, buckets_json, api_calls, turns, started_ts, ended_ts)"
                " VALUES (?, ?, 1, 1, 'exact', ?, '{}', '{}', 1, 1, ?, ?)",
                (f"{provenance}-{i}", time.time(), provenance, time.time(), time.time()),
            )


# -- atomic claim -------------------------------------------------------------

def test_claim_exactly_one_winner_across_threads(db_path):
    setup = core.connect(db_path)
    with core.write_txn(setup):
        setup.execute(
            "INSERT INTO refresh_requests (requested_at, source) VALUES (?, 'manual')",
            (time.time(),),
        )
    req_id = setup.execute("SELECT id FROM refresh_requests").fetchone()["id"]
    wins = []
    barrier = threading.Barrier(2)

    def claimer():
        conn = core.connect(db_path)
        try:
            barrier.wait()
            with core.write_txn(conn):
                if core.claim_refresh_request(conn, req_id):
                    wins.append(1)
        finally:
            conn.close()

    threads = [threading.Thread(target=claimer) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1
    setup.close()


def test_ttl_reclaims_dead_running_rows(db):
    with core.write_txn(db):
        db.execute(
            "INSERT INTO refresh_requests (requested_at, source, status, started_at)"
            " VALUES (?, 'auto', 'running', ?)",
            (time.time(), time.time() - 3600),  # started an hour ago
        )
        assert core.reclaim_stuck_refreshes(db, ttl_seconds=1800) == 1
    row = db.execute("SELECT status FROM refresh_requests").fetchone()
    assert row["status"] == "pending"


def test_ttl_leaves_fresh_running_rows(db):
    with core.write_txn(db):
        db.execute(
            "INSERT INTO refresh_requests (requested_at, source, status, started_at)"
            " VALUES (?, 'auto', 'running', ?)",
            (time.time(), time.time() - 60),
        )
        assert core.reclaim_stuck_refreshes(db, ttl_seconds=1800) == 0


# -- watermark ----------------------------------------------------------------

def test_watermark_blocks_double_insert(db):
    _seed_sessions(db, 10)
    with core.write_txn(db):
        run1 = core.begin_suggestion_run(db, kind="llm")
    with core.write_txn(db):
        run2 = core.begin_suggestion_run(db, kind="llm")
    assert run1 is not None
    assert run2 is None  # same watermark, UNIQUE blocked it


def test_detector_and_llm_watermarks_independent(db):
    _seed_sessions(db, 5)
    with core.write_txn(db):
        assert core.begin_suggestion_run(db, kind="detector") is not None
    with core.write_txn(db):
        assert core.begin_suggestion_run(db, kind="llm") is not None


# -- split gates (D9) ----------------------------------------------------------

def test_detector_gate_fires_at_exactly_3(db):
    _seed_sessions(db, 2)
    ok, reason = core.gate_check(db, kind="detector", min_sessions=3)
    assert not ok and "2/3" in reason
    _seed_sessions(db, 3)  # replaces same ids; add one more below
    with core.write_txn(db):
        db.execute(
            "INSERT INTO session_rollups (session_id, analyzed_at, analyzer_version,"
            " rules_version, precision, provenance, totals_json, buckets_json,"
            " api_calls, turns) VALUES ('extra', ?, 1, 1, 'exact', 'recorder', '{}', '{}', 1, 1)",
            (time.time(),),
        )
    ok, _ = core.gate_check(db, kind="detector", min_sessions=3)
    assert ok


def test_llm_gate_needs_10(db):
    _seed_sessions(db, 9)
    ok, reason = core.gate_check(db, kind="llm", min_sessions=10, refresh_every=5)
    assert not ok and "9/10" in reason


def test_backfill_sessions_never_satisfy_gates(db):
    _seed_sessions(db, 30, provenance="backfill")
    ok, _ = core.gate_check(db, kind="detector", min_sessions=3)
    assert not ok  # 30 backfilled sessions cannot unlock anything


def test_llm_refresh_every_counts_from_watermark(db):
    _seed_sessions(db, 10)
    with core.write_txn(db):
        core.begin_suggestion_run(db, kind="llm")  # watermark=10
    ok, reason = core.gate_check(db, kind="llm", min_sessions=10, refresh_every=5)
    assert not ok and "0/5" in reason
    _seed_sessions(db, 15)  # now 15 recorder sessions
    ok, _ = core.gate_check(db, kind="llm", min_sessions=10, refresh_every=5)
    assert ok


def test_detector_runs_dont_poison_llm_gate(db):
    _seed_sessions(db, 12)
    with core.write_txn(db):
        core.begin_suggestion_run(db, kind="detector")  # detector watermark=12
    # LLM gate: no LLM run yet -> refresh_every doesn't block
    ok, _ = core.gate_check(db, kind="llm", min_sessions=10, refresh_every=5)
    assert ok
