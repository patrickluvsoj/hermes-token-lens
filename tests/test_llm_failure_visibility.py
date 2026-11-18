"""Issue #6 — LLM refresh failure visibility.

A failed LLM pass used to leave its suggestion_runs row empty (model='',
0 tokens, '{}' purposes) and surface only a generic timeout string in the
UI. The run row must record why the stage failed.
"""
import json
from types import SimpleNamespace

import pytest

import token_lens_core as core
import engine


class _Boom:
    """LLM facade whose structured call raises a specific error."""

    def __init__(self, exc):
        self._exc = exc

    def complete_structured(self, **_):
        raise self._exc


def _seed_session(db):
    with core.write_txn(db):
        db.execute(
            "INSERT INTO session_rollups (session_id, analyzed_at, analyzer_version,"
            " rules_version, precision, provenance, totals_json, buckets_json,"
            " api_calls, turns, started_ts, ended_ts)"
            " VALUES ('s1', 1.0, 1, 1, 'exact', 'recorder',"
            " '{\"input\": 100, \"output\": 1, \"cache_read\": 0, \"cache_write\": 0,"
            "  \"reasoning\": 0, \"billed\": 101}',"
            " '{\"tool_results\": 60, \"history.assistant\": 40}', 3, 1, 1.0, 2.0)"
        )


def test_generation_failure_recorded_on_run_row(db):
    _seed_session(db)
    cfg = {"meta_budget_tokens": 50000, "score_threshold": 6, "evolve_rules": False}
    result = engine.run_llm_refresh(db, _Boom(TimeoutError("upstream took 90s")), cfg)
    assert result["status"] == "skipped"
    assert "TimeoutError" in result["reason"]

    row = db.execute(
        "SELECT model, tokens_input, tokens_output, purpose_breakdown_json"
        " FROM suggestion_runs WHERE kind='llm' ORDER BY id DESC"
    ).fetchone()
    purposes = json.loads(row["purpose_breakdown_json"])
    err = purposes["token-lens.suggest.error"]
    assert err["type"] == "TimeoutError"
    assert "upstream took 90s" in err["message"]
    assert row["model"] == ""      # never reached a model — but now we know why


def test_reason_names_the_exception_type(db):
    _seed_session(db)
    cfg = {"meta_budget_tokens": 50000, "score_threshold": 6, "evolve_rules": False}
    result = engine.run_llm_refresh(
        db, _Boom(RuntimeError("connection refused")), cfg)
    assert result["reason"].startswith("LLM unavailable: RuntimeError:")
