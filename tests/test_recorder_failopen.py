"""Fail-open + circuit breaker: hooks never raise, breaker trips at 5
consecutive errors and surfaces in meta_kv (-> /health)."""
import importlib.util
import json
from pathlib import Path

import pytest

import token_lens_core as core

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def recorder_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "token_lens_plugin_failopen_test", PLUGIN_ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._RECORDER = mod._Recorder()
    yield mod
    conn = mod._RECORDER._conn
    if conn is not None:
        conn.close()


def test_hook_exception_never_propagates(recorder_mod, monkeypatch):
    m = recorder_mod

    def boom(**kwargs):
        raise RuntimeError("disk full")

    wrapped = m._failopen_hook(boom)
    wrapped(api_request_id="x")  # must not raise
    assert m._RECORDER._error_streak == 1


def test_breaker_trips_at_5_and_disables(recorder_mod):
    m = recorder_mod

    def boom(**kwargs):
        raise RuntimeError("nope")

    wrapped = m._failopen_hook(boom)
    for _ in range(5):
        wrapped()
    assert m._RECORDER._breaker_tripped is True
    # further hook calls are no-ops: streak stops growing
    wrapped()
    assert m._RECORDER._error_streak == 5


def test_breaker_state_surfaced_in_meta_kv(recorder_mod, tmp_path):
    m = recorder_mod

    def boom(**kwargs):
        raise RuntimeError("nope")

    wrapped = m._failopen_hook(boom)
    for _ in range(5):
        wrapped()
    db = core.connect(Path(tmp_path) / "token_lens.db")
    row = db.execute("SELECT value FROM meta_kv WHERE key='breaker'").fetchone()
    assert row is not None
    assert json.loads(row["value"])["tripped"] is True
    db.close()


def test_success_resets_streak(recorder_mod):
    m = recorder_mod
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] <= 4:
            raise RuntimeError("transient")

    wrapped = m._failopen_hook(flaky)
    for _ in range(4):
        wrapped()
    assert m._RECORDER._error_streak == 4
    wrapped()  # succeeds -> streak resets, breaker never trips
    assert m._RECORDER._error_streak == 0
    assert m._RECORDER._breaker_tripped is False


def test_db_newer_than_code_fails_open(recorder_mod, tmp_path):
    m = recorder_mod
    # Pre-create a DB stamped with a future schema version
    db = core.connect(Path(tmp_path) / "token_lens.db")
    db.execute(f"PRAGMA user_version={core.SCHEMA_VERSION + 7}")
    db.commit()
    db.close()
    m._RECORDER = m._Recorder()
    m.on_pre_api_request(  # must not raise; recorder silently disabled
        api_request_id="x:api:1", turn_id="x", session_id="s",
        model="m", provider="p",
        request_messages=[{"role": "user", "content": "x"}], tool_count=0,
    )
    assert m._RECORDER._conn_failed is True


def test_recorder_disabled_via_config(recorder_mod):
    m = recorder_mod
    m._RECORDER._config = {"recorder_enabled": False}
    m.on_pre_api_request(
        api_request_id="x:api:1", turn_id="x", session_id="s",
        model="m", provider="p",
        request_messages=[{"role": "user", "content": "x"}], tool_count=0,
    )
    assert m._RECORDER._buffer == []
