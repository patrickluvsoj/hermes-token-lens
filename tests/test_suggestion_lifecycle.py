"""Suggestion lifecycle (eng delta review D25): fingerprint inheritance incl.
the two-thread dismiss/insert race (D22), done/dismiss idempotency, observed
delta math + edges (D24)."""
import json
import threading
import time

import token_lens_core as core


def _insert(conn, fp="mcp_disable:browser-tools", savings=18.0, **kw):
    with core.write_txn(conn):
        return core.insert_suggestion(
            conn, run_id=None, fingerprint=fp, title="t", evidence="e",
            plan_md="p", category="tool_schemas.mcp.browser-tools",
            est_savings_pct=savings, **kw,
        )


def _status(conn, sid):
    return conn.execute("SELECT status FROM suggestions WHERE id=?", (sid,)).fetchone()["status"]


# -- inheritance ------------------------------------------------------------

def test_first_insert_is_shown(db):
    sid = _insert(db)
    assert _status(db, sid) == "shown"


def test_done_inherited_forever(db):
    sid = _insert(db)
    with core.write_txn(db):
        core.set_suggestion_status(db, sid, "done")
    sid2 = _insert(db, savings=99.0)  # even huge savings never resurrect done
    assert _status(db, sid2) == "done"


def test_dismissed_inherited_below_2x(db):
    sid = _insert(db, savings=10.0)
    with core.write_txn(db):
        core.set_suggestion_status(db, sid, "dismissed")
    sid2 = _insert(db, savings=15.0)  # 1.5x < 2x
    assert _status(db, sid2) == "dismissed"


def test_dismissed_resurrects_at_2x_with_label(db):
    sid = _insert(db, savings=9.0)
    with core.write_txn(db):
        core.set_suggestion_status(db, sid, "dismissed")
    sid2 = _insert(db, savings=21.0)  # >= 2x
    assert _status(db, sid2) == "shown"
    evidence = db.execute(
        "SELECT evidence FROM suggestions WHERE id=?", (sid2,)
    ).fetchone()["evidence"]
    assert "Previously dismissed" in evidence
    assert "9" in evidence and "21" in evidence


def test_different_fingerprints_independent(db):
    sid = _insert(db, fp="a")
    with core.write_txn(db):
        core.set_suggestion_status(db, sid, "dismissed")
    sid2 = _insert(db, fp="b")
    assert _status(db, sid2) == "shown"


# -- race (D22): concurrent dismiss + insert, dismissed always wins ----------

def test_concurrent_dismiss_and_insert_race(db_path):
    """100 rounds: writer A dismisses the latest row while writer B inserts a
    regenerated row with the same fingerprint. Whatever the interleaving, the
    new row must NEVER be 'shown' once the dismiss has committed — checked by
    asserting at the end that the newest row is dismissed (the dismiss always
    commits before the verdict read)."""
    fp = "race:fp"
    setup = core.connect(db_path)
    for round_no in range(100):
        sid = None
        with core.write_txn(setup):
            sid = core.insert_suggestion(
                setup, run_id=None, fingerprint=fp, title="t", evidence="e",
                plan_md="p", category="c", est_savings_pct=10.0,
            )
            # ensure baseline shown row exists; dismiss happens in thread A
        barrier = threading.Barrier(2)
        errors = []

        def dismisser():
            conn = core.connect(db_path)
            try:
                barrier.wait()
                with core.write_txn(conn):
                    core.set_suggestion_status(conn, sid, "dismissed")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                conn.close()

        def inserter():
            conn = core.connect(db_path)
            try:
                barrier.wait()
                with core.write_txn(conn):
                    core.insert_suggestion(
                        conn, run_id=None, fingerprint=fp, title="t",
                        evidence="e", plan_md="p", category="c",
                        est_savings_pct=10.0,  # 1.0x — never resurrects
                    )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                conn.close()

        t1 = threading.Thread(target=dismisser)
        t2 = threading.Thread(target=inserter)
        t1.start(); t2.start(); t1.join(); t2.join()
        assert not errors, errors

        # Post-condition: after the dismiss committed, ANY regenerated row
        # inserted AFTER it must be dismissed. Insert one more now (serially)
        # to prove inheritance sees the dismissal regardless of the race.
        with core.write_txn(setup):
            verdict_id = core.insert_suggestion(
                setup, run_id=None, fingerprint=fp, title="t", evidence="e",
                plan_md="p", category="c", est_savings_pct=10.0,
            )
        status = setup.execute(
            "SELECT status FROM suggestions WHERE id=?", (verdict_id,)
        ).fetchone()["status"]
        assert status == "dismissed", f"round {round_no}: resurrection leaked"
        # clean slate per round
        with core.write_txn(setup):
            setup.execute("DELETE FROM suggestions WHERE fingerprint=?", (fp,))
    setup.close()


# -- /done endpoint semantics -------------------------------------------------

def test_set_status_unknown_id_returns_false(db):
    with core.write_txn(db):
        assert core.set_suggestion_status(db, 99999, "done") is False


def test_done_idempotent_keeps_timestamp(db):
    sid = _insert(db)
    with core.write_txn(db):
        core.set_suggestion_status(db, sid, "done")
    ts1 = db.execute(
        "SELECT status_changed_at FROM suggestions WHERE id=?", (sid,)
    ).fetchone()["status_changed_at"]
    time.sleep(0.01)
    with core.write_txn(db):
        assert core.set_suggestion_status(db, sid, "done") is True
    ts2 = db.execute(
        "SELECT status_changed_at FROM suggestions WHERE id=?", (sid,)
    ).fetchone()["status_changed_at"]
    assert ts1 == ts2


# -- observed delta (D24) -----------------------------------------------------

def _rollup(conn, session_id, ended_ts, category_tokens, provenance="recorder"):
    with core.write_txn(conn):
        conn.execute(
            "INSERT INTO session_rollups (session_id, analyzed_at, analyzer_version,"
            " rules_version, precision, provenance, totals_json, buckets_json,"
            " api_calls, turns, started_ts, ended_ts)"
            " VALUES (?, ?, 1, 1, 'exact', ?, '{}', ?, 1, 1, ?, ?)",
            (session_id, time.time(), provenance,
             json.dumps({"tool_schemas.mcp.browser-tools": category_tokens}),
             ended_ts - 60, ended_ts),
        )


def test_observed_measuring_until_5_post_sessions(db):
    done_at = 1_000_000.0
    for i in range(3):
        _rollup(db, f"post{i}", done_at + 100 + i, 500)
    result = core.observed_delta(db, category="tool_schemas.mcp", done_at=done_at)
    assert result["state"] == "measuring"
    assert result["post_sessions"] == 3


def test_observed_measured_per_session_average(db):
    done_at = 1_000_000.0
    for i in range(4):
        _rollup(db, f"pre{i}", done_at - 1000 - i, 1000)  # before avg 1000
    for i in range(5):
        _rollup(db, f"post{i}", done_at + 100 + i, 800)   # after avg 800
    result = core.observed_delta(
        db, category="tool_schemas.mcp.browser-tools", done_at=done_at
    )
    assert result["state"] == "measured"
    assert abs(result["pct"] - (-20.0)) < 1e-6
    assert abs(result["abs_per_session"] - (-200.0)) < 1e-6


def test_observed_no_baseline_never_divides_by_zero(db):
    done_at = 1_000_000.0
    for i in range(5):
        _rollup(db, f"post{i}", done_at + 100 + i, 800)
    result = core.observed_delta(db, category="tool_schemas.mcp", done_at=done_at)
    assert result["state"] == "no_baseline"


def test_observed_parent_category_matches_children(db):
    done_at = 1_000_000.0
    _rollup(db, "pre0", done_at - 500, 1000)
    for i in range(5):
        _rollup(db, f"post{i}", done_at + 100 + i, 900)
    result = core.observed_delta(db, category="tool_schemas.mcp", done_at=done_at)
    assert result["state"] == "measured"  # parent prefix matched the child key
