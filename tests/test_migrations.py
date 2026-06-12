"""Open-matrix: older migrates forward idempotently; current opens clean;
NEWER DB refuses to open (recorder fails open, dashboard shows error card)."""
import sqlite3

import pytest

import token_lens_core as core


def test_fresh_db_migrates_to_current(db_path):
    conn = core.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == core.SCHEMA_VERSION
    conn.close()


def test_reopen_is_idempotent(db_path):
    core.connect(db_path).close()
    conn = core.connect(db_path)  # second open re-runs the ladder harmlessly
    assert conn.execute("PRAGMA user_version").fetchone()[0] == core.SCHEMA_VERSION
    # rules v1 seeded exactly once
    assert conn.execute("SELECT COUNT(*) FROM category_rules").fetchone()[0] == 1
    conn.close()


def test_newer_db_refuses_to_open(db_path):
    conn = core.connect(db_path)
    conn.execute(f"PRAGMA user_version={core.SCHEMA_VERSION + 5}")
    conn.commit()
    conn.close()
    with pytest.raises(core.DBNewerThanCode):
        core.connect(db_path)


def test_data_survives_reopen(db_path):
    conn = core.connect(db_path)
    with core.write_txn(conn):
        core.upsert_pre_call(
            conn, api_request_id="x:api:1", session_id="s", turn_id="x",
            ts=1.0, model="m", provider="p", request_hash="h", buckets={"a": 1},
        )
    conn.close()
    conn = core.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 1
    conn.close()


def test_wal_mode_and_busy_timeout(db_path):
    conn = core.connect(db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    conn.close()
