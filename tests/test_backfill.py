"""Backfill: estimated rollups from a core-shaped state.db, chunked resume,
totals-only degradation, provenance never satisfies gates."""
import json
import sqlite3
import time

import token_lens_core as core


def _fake_core_db(tmp_path, sessions):
    """Build a minimal core-shaped state.db: sessions + messages tables."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, started_at REAL, ended_at REAL,
            input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0, message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0, api_call_count INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
            content TEXT, token_count INTEGER, active INTEGER DEFAULT 1
        );
    """)
    now = time.time()
    for s in sessions:
        conn.execute(
            "INSERT INTO sessions (id, started_at, ended_at, input_tokens,"
            " output_tokens, cache_read_tokens, message_count, api_call_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (s["id"], now - s.get("age", 3600), now - s.get("age", 3600) + 60,
             s.get("input", 1000), s.get("output", 100), s.get("cache", 0),
             len(s.get("messages", [])), s.get("calls", 3)),
        )
        for role, content, tok in s.get("messages", []):
            conn.execute(
                "INSERT INTO messages (session_id, role, content, token_count)"
                " VALUES (?, ?, ?, ?)", (s["id"], role, content, tok),
            )
    conn.commit()
    return conn


def test_backfill_writes_estimated_rollups(db, tmp_path):
    src = _fake_core_db(tmp_path, [{
        "id": "old1",
        "messages": [("system", "base prompt", 50), ("user", "hi", 10),
                     ("assistant", "hello", 20), ("tool", "result", 30)],
    }])
    result = core.backfill(db, days=30, core_db=src)
    assert result["sessions"] == 1
    row = db.execute("SELECT * FROM session_rollups WHERE session_id='old1'").fetchone()
    assert row["precision"] == "estimated"
    assert row["provenance"] == "backfill"
    buckets = json.loads(row["buckets_json"])
    # input buckets calibrated to billed prompt (1000)
    input_sum = sum(v for k, v in buckets.items() if k != "output")
    assert abs(input_sum - 1000) < 1e-6
    src.close()


def test_backfill_skips_already_rolled_up(db, tmp_path):
    src = _fake_core_db(tmp_path, [{"id": "old2", "messages": [("user", "x", 10)]}])
    assert core.backfill(db, days=30, core_db=src)["sessions"] == 1
    again = core.backfill(db, days=30, core_db=src)
    assert again["sessions"] == 0 and again["skipped"] == 1  # resumable
    src.close()


def test_backfill_totals_only_session_degrades_to_unattributed(db, tmp_path):
    src = _fake_core_db(tmp_path, [{"id": "pruned", "messages": []}])
    core.backfill(db, days=30, core_db=src)
    row = db.execute("SELECT buckets_json FROM session_rollups WHERE session_id='pruned'").fetchone()
    buckets = json.loads(row["buckets_json"])
    assert buckets.get("unattributed", 0) == 1000.0
    src.close()


def test_backfill_window_excludes_old_sessions(db, tmp_path):
    src = _fake_core_db(tmp_path, [
        {"id": "recent", "messages": [("user", "x", 10)]},
        {"id": "ancient", "age": 90 * 86400, "messages": [("user", "x", 10)]},
    ])
    result = core.backfill(db, days=30, core_db=src)
    assert result["sessions"] == 1
    assert db.execute("SELECT COUNT(*) FROM session_rollups WHERE session_id='ancient'").fetchone()[0] == 0
    src.close()


def test_backfill_never_satisfies_gates(db, tmp_path):
    sessions = [{"id": f"bf{i}", "messages": [("user", "x", 10)]} for i in range(20)]
    src = _fake_core_db(tmp_path, sessions)
    core.backfill(db, days=30, core_db=src)
    ok, _ = core.gate_check(db, kind="detector", min_sessions=3)
    assert not ok
    src.close()


def test_backfill_progress_callback_chunks(db, tmp_path):
    sessions = [{"id": f"c{i}", "messages": [("user", "x", 10)]} for i in range(25)]
    src = _fake_core_db(tmp_path, sessions)
    ticks = []
    core.backfill(db, days=30, core_db=src, progress_cb=lambda d, t: ticks.append((d, t)))
    assert len(ticks) >= 2  # chunked progress, plus the final tick
    assert ticks[-1][0] == 25
    src.close()
