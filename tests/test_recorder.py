"""Recorder: pre→post pairing, retry re-fire upsert, buffered flush,
incomplete exclusion, schema-cost caching."""
import importlib.util
import time
from pathlib import Path

import pytest

import token_lens_core as core

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def recorder_mod(tmp_path, monkeypatch):
    """Load the plugin __init__.py as a standalone module against a tmp DB."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "token_lens_plugin_under_test", PLUGIN_ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._RECORDER = mod._Recorder()  # fresh state per test
    yield mod
    conn = mod._RECORDER._conn
    if conn is not None:
        conn.close()


def _db(tmp_path):
    return core.connect(Path(tmp_path) / "token_lens.db")


def test_pre_post_pairing(recorder_mod, tmp_path):
    m = recorder_mod
    m.on_pre_api_request(
        api_request_id="t1:api:1", turn_id="t1", session_id="s1",
        model="claude", provider="anthropic",
        request_messages=[{"role": "user", "content": "hello " * 100}],
        tool_count=0,
    )
    m.on_post_api_request(
        api_request_id="t1:api:1",
        usage={"input_tokens": 500, "prompt_tokens": 500, "output_tokens": 20},
    )
    m._RECORDER.flush()
    db = _db(tmp_path)
    row = db.execute("SELECT * FROM api_calls WHERE api_request_id='t1:api:1'").fetchone()
    assert row["status"] == "complete"
    assert row["actual_input"] == 500
    assert row["session_id"] == "s1"
    db.close()


def test_retry_refire_updates_same_row(recorder_mod, tmp_path):
    m = recorder_mod
    for attempt in range(3):  # provider retries re-fire pre with same id
        m.on_pre_api_request(
            api_request_id="t1:api:1", turn_id="t1", session_id="s1",
            model="claude", provider="anthropic",
            request_messages=[{"role": "user", "content": f"try {attempt}"}],
            tool_count=0,
        )
    m._RECORDER.flush()
    db = _db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 1
    db.close()


def test_unpaired_pre_stays_incomplete_and_excluded(recorder_mod, tmp_path):
    m = recorder_mod
    m.on_pre_api_request(
        api_request_id="t9:api:1", turn_id="t9", session_id="s9",
        model="m", provider="p",
        request_messages=[{"role": "user", "content": "x"}], tool_count=0,
    )
    m._RECORDER.flush()
    db = _db(tmp_path)
    row = db.execute("SELECT status FROM api_calls").fetchone()
    assert row["status"] == "incomplete"
    with core.write_txn(db):
        assert core.rollup_session(db, "s9") is None  # excluded from rollups
    db.close()


def test_flush_triggers_at_20_calls(recorder_mod, tmp_path):
    m = recorder_mod
    for i in range(20):
        m.on_pre_api_request(
            api_request_id=f"t:api:{i}", turn_id="t", session_id="s",
            model="m", provider="p",
            request_messages=[{"role": "user", "content": "x"}], tool_count=0,
        )
    # 20th call flushed the buffer without an explicit flush()
    db = _db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 20
    db.close()


def test_flush_triggers_after_5_seconds(recorder_mod, tmp_path, monkeypatch):
    m = recorder_mod
    m.on_pre_api_request(
        api_request_id="t:api:1", turn_id="t", session_id="s",
        model="m", provider="p",
        request_messages=[{"role": "user", "content": "x"}], tool_count=0,
    )
    m._RECORDER._last_flush = time.time() - 6.0  # pretend 6s passed
    m.on_pre_api_request(
        api_request_id="t:api:2", turn_id="t", session_id="s",
        model="m", provider="p",
        request_messages=[{"role": "user", "content": "x"}], tool_count=0,
    )
    db = _db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 2
    db.close()


def test_finalize_flushes_and_rolls_up(recorder_mod, tmp_path):
    m = recorder_mod
    m.on_pre_api_request(
        api_request_id="t:api:1", turn_id="t", session_id="sF",
        model="m", provider="p",
        request_messages=[{"role": "user", "content": "x " * 50}], tool_count=0,
    )
    m.on_post_api_request(
        api_request_id="t:api:1",
        usage={"input_tokens": 100, "prompt_tokens": 100, "output_tokens": 5},
    )
    m.on_session_finalize(session_id="sF")
    deadline = time.time() + 5
    db = _db(tmp_path)
    rolled = False
    while time.time() < deadline:  # finalize work runs on a daemon thread
        if db.execute("SELECT COUNT(*) FROM session_rollups WHERE session_id='sF'").fetchone()[0]:
            rolled = True
            break
        time.sleep(0.05)
    assert rolled
    db.close()


def test_schema_costs_cached_until_tool_count_changes(recorder_mod):
    m = recorder_mod
    r = m._RECORDER
    r._schema_costs = {"tool_schemas.builtin": 123}
    r._schema_costs_tool_count = 5
    assert r.schema_costs(5) == {"tool_schemas.builtin": 123}  # cache hit
    # different count -> recompute (registry unavailable in tests -> {})
    assert r.schema_costs(6) == {}
