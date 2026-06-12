"""API endpoints: happy, empty-DB, window filters, suggestion verbs,
backfill job lifecycle, health checks, DB-newer 409."""
import importlib.util
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import token_lens_core as core

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "token_lens_api_under_test", PLUGIN_ROOT / "dashboard" / "plugin_api.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._backfill_job.clear()
    mod._backfill_job.update({"state": "idle"})
    mod._spawn_refresh_process = lambda: False  # no real subprocesses in tests
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/token-lens")
    return TestClient(app), mod, tmp_path


def _seed_session(tmp_path, session_id="s1", *, billed=1000, precision="exact",
                  provenance="recorder", buckets=None, ended_offset=0.0):
    conn = core.connect(Path(tmp_path) / "token_lens.db")
    b = buckets or {"system_prompt": 300.0, "history.user": 500.0, "output": 200.0}
    totals = {"input": billed - 200, "output": 200, "cache_read": 0,
              "cache_write": 0, "reasoning": 0, "billed": billed}
    with core.write_txn(conn):
        conn.execute(
            "INSERT OR REPLACE INTO session_rollups (session_id, analyzed_at,"
            " analyzer_version, rules_version, precision, provenance, totals_json,"
            " buckets_json, api_calls, turns, started_ts, ended_ts)"
            " VALUES (?, ?, 1, 1, ?, ?, ?, ?, 4, 2, ?, ?)",
            (session_id, time.time(), precision, provenance, json.dumps(totals),
             json.dumps(b), time.time() - 120 + ended_offset,
             time.time() - 60 + ended_offset),
        )
    conn.close()


# -- empty DB states ----------------------------------------------------------

def test_summary_empty_db(client):
    c, _m, _t = client
    r = c.get("/api/plugins/token-lens/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["total_tokens"] == 0
    assert body["sessions"] == 0
    assert body["has_any_data"] is False


def test_suggestions_empty_db_reports_gates(client):
    c, _m, _t = client
    body = c.get("/api/plugins/token-lens/suggestions").json()
    assert body["suggestions"] == []
    assert body["gates"]["detector"]["open"] is False
    assert "3" in body["gates"]["detector"]["reason"]


def test_session_detail_404(client):
    c, _m, _t = client
    assert c.get("/api/plugins/token-lens/sessions/nope").status_code == 404


# -- happy paths ---------------------------------------------------------------

def test_summary_with_data(client):
    c, _m, t = client
    _seed_session(t, "s1")
    body = c.get("/api/plugins/token-lens/summary?window=7d").json()
    assert body["total_tokens"] == 1000
    assert body["sessions"] == 1
    assert body["precision"] == "exact"


def test_summary_mixed_precision_badges_estimated(client):
    c, _m, t = client
    _seed_session(t, "s1", precision="exact")
    _seed_session(t, "s2", precision="estimated")
    body = c.get("/api/plugins/token-lens/summary").json()
    assert body["precision"] == "estimated"  # D5: exact only at 100%
    assert 0 < body["estimated_share_pct"] <= 100


def test_categories_rolls_children_to_top_level(client):
    c, _m, t = client
    _seed_session(t, "s1", buckets={
        "tool_schemas.mcp.alpha": 100.0, "tool_schemas.mcp.beta": 50.0,
        "history.user": 200.0, "output": 30.0,
    })
    body = c.get("/api/plugins/token-lens/categories").json()
    assert body["categories"]["tool_schemas.mcp"] == 150.0
    assert body["children"]["tool_schemas.mcp"]["tool_schemas.mcp.alpha"] == 100.0


def test_timeseries_7d_has_7_labeled_bars_with_zero_days(client):
    c, _m, t = client
    _seed_session(t, "s1")
    body = c.get("/api/plugins/token-lens/timeseries?window=7d").json()
    assert len(body["bars"]) == 7  # empty days render as labeled zero bars
    assert body["stack_categories"][-1] == "other"


def test_timeseries_stacks_by_category_never_model(client):
    c, _m, t = client
    _seed_session(t, "s1")
    body = c.get("/api/plugins/token-lens/timeseries?window=7d").json()
    for cat in body["stack_categories"]:
        assert cat == "other" or cat in core.CATEGORY_IDS


def test_window_validation(client):
    c, _m, _t = client
    assert c.get("/api/plugins/token-lens/summary?window=1y").status_code == 422


# -- suggestion verbs ----------------------------------------------------------

def _seed_suggestion(tmp_path):
    conn = core.connect(Path(tmp_path) / "token_lens.db")
    with core.write_txn(conn):
        sid = core.insert_suggestion(
            conn, run_id=None, fingerprint="mcp_disable:x", title="t",
            evidence="e", plan_md="1. do", category="tool_schemas.mcp.x",
            est_savings_pct=12.0,
        )
    conn.close()
    return sid


def test_dismiss_and_done_endpoints(client):
    c, _m, t = client
    sid = _seed_suggestion(t)
    assert c.post(f"/api/plugins/token-lens/suggestions/{sid}/dismiss").json()["status"] == "dismissed"
    assert c.post(f"/api/plugins/token-lens/suggestions/{sid}/done").json()["status"] == "done"
    assert c.post("/api/plugins/token-lens/suggestions/99999/done").status_code == 404


def test_dismissed_suggestion_leaves_shown_list(client):
    c, _m, t = client
    sid = _seed_suggestion(t)
    body = c.get("/api/plugins/token-lens/suggestions").json()
    assert len(body["suggestions"]) == 1
    c.post(f"/api/plugins/token-lens/suggestions/{sid}/dismiss")
    body = c.get("/api/plugins/token-lens/suggestions").json()
    assert body["suggestions"] == []


def test_done_appears_in_acted_on_with_observed(client):
    c, _m, t = client
    sid = _seed_suggestion(t)
    c.post(f"/api/plugins/token-lens/suggestions/{sid}/done")
    body = c.get("/api/plugins/token-lens/suggestions").json()
    assert len(body["acted_on"]) == 1
    assert body["acted_on"][0]["observed"]["state"] in ("measuring", "no_baseline")


def test_manual_refresh_queues_then_cooldown(client):
    c, _m, _t = client
    r = c.post("/api/plugins/token-lens/suggestions/refresh")
    assert r.status_code == 200 and r.json()["queued"] is True
    r2 = c.post("/api/plugins/token-lens/suggestions/refresh")
    assert r2.status_code == 429  # 1h cooldown


# -- backfill job ----------------------------------------------------------------

def test_backfill_job_lifecycle(client):
    c, _m, _t = client
    r = c.post("/api/plugins/token-lens/backfill?days=30").json()
    assert r["job"]["state"] in ("running", "done", "failed")
    deadline = time.time() + 5
    state = None
    while time.time() < deadline:
        state = c.get("/api/plugins/token-lens/backfill/status").json()["job"]["state"]
        if state in ("done", "failed"):
            break
        time.sleep(0.05)
    assert state == "done"  # no core state.db in tmp HERMES_HOME -> clean done


def test_backfill_duplicate_post_returns_running_job(client):
    c, m, _t = client
    with m._backfill_lock:
        m._backfill_job.update({"state": "running", "done": 3, "total": 10})
    r = c.post("/api/plugins/token-lens/backfill").json()
    assert r.get("already_running") is True


# -- health ----------------------------------------------------------------------

def test_health_reports_versions_and_recorder(client):
    c, _m, _t = client
    body = c.get("/api/plugins/token-lens/health").json()
    assert body["schema_version"] == core.SCHEMA_VERSION
    assert body["rules_version"] == 1
    assert body["recorder_detected"] is False
    assert body["breaker"] == {"tripped": False}


def test_health_unattributed_alert(client):
    c, _m, t = client
    _seed_session(t, "s1", buckets={"unattributed": 900.0, "output": 100.0})
    body = c.get("/api/plugins/token-lens/health").json()
    assert body["unattributed_alert"] is True


# -- DB newer than code ------------------------------------------------------------

def test_db_newer_returns_409(client):
    c, _m, t = client
    conn = core.connect(Path(t) / "token_lens.db")
    conn.execute(f"PRAGMA user_version={core.SCHEMA_VERSION + 3}")
    conn.commit()
    conn.close()
    r = c.get("/api/plugins/token-lens/summary")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "db_newer_than_code"


# -- by-model backfill fallback (M3-T3) -------------------------------------

def _fake_core_state_db(tmp_path):
    import sqlite3
    conn = sqlite3.connect(str(Path(tmp_path) / "state.db"))
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, started_at REAL, ended_at REAL, model TEXT,
            input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0, message_count INTEGER DEFAULT 0,
            api_call_count INTEGER DEFAULT 0
        );
    """)
    conn.execute(
        "INSERT INTO sessions (id, started_at, model, input_tokens, output_tokens,"
        " api_call_count) VALUES ('s1', ?, 'gpt-5.5', 50000, 5000, 12)",
        (time.time() - 3600,))
    conn.commit()
    conn.close()


def test_by_model_falls_back_to_core_when_no_recorder_rows(client):
    c, _m, t = client
    _fake_core_state_db(t)
    body = c.get("/api/plugins/token-lens/by-model?window=7d").json()
    assert body["estimated"] is True
    assert body["models"][0]["model"] == "gpt-5.5"
    assert body["models"][0]["input"] == 50000


def test_by_model_prefers_recorder_rows(client):
    c, _m, t = client
    _fake_core_state_db(t)
    conn = core.connect(Path(t) / "token_lens.db")
    with core.write_txn(conn):
        core.upsert_pre_call(
            conn, api_request_id="x:api:1", session_id="s", turn_id="x",
            ts=time.time(), model="claude", provider="p", request_hash="h",
            buckets={"system_prompt": 10})
        core.complete_post_call(
            conn, api_request_id="x:api:1",
            usage={"input_tokens": 100, "prompt_tokens": 100, "output_tokens": 5})
    conn.close()
    body = c.get("/api/plugins/token-lens/by-model?window=7d").json()
    assert body["estimated"] is False
    assert body["models"][0]["model"] == "claude"
