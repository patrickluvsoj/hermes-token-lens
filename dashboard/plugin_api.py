"""Token Lens dashboard plugin — backend API routes.

Mounted at /api/plugins/token-lens/ by the dashboard plugin system (routes
mount at dashboard startup only — installs require a dashboard restart).
HTTP routes ride the dashboard's session-token auth middleware like core
API routes.

Process model: this file runs in the DASHBOARD process. It shares state
with the agent-side recorder only through the plugin's SQLite DB. The
catch-up sweep (crashed/never-finalized sessions) is debounced to 30s and
runs on a worker thread — GET handlers schedule it and return immediately,
never paying sweep latency (plan §Dashboard-side execution bounds).

The dashboard's importlib loader does not guarantee sibling-module
resolution, so token_lens_core is imported via an explicit sys.path
insertion of the plugin root.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

import token_lens_core as core  # noqa: E402

router = APIRouter()

MANUAL_REFRESH_COOLDOWN = 3600.0  # 1 hour (plan §Suggestion engine)
SWEEP_DEBOUNCE = 30.0
MAX_SUGGESTIONS_SHOWN = 5

_sweep_lock = threading.Lock()
_last_sweep = 0.0


class _DbNewer(HTTPException):
    def __init__(self, exc: core.DBNewerThanCode):
        super().__init__(status_code=409, detail={
            "error": "db_newer_than_code",
            "db_version": exc.db_version,
            "code_version": core.SCHEMA_VERSION,
            "message": str(exc),
        })


def _conn() -> sqlite3.Connection:
    try:
        return core.connect()
    except core.DBNewerThanCode as exc:
        raise _DbNewer(exc)


def _schedule_sweep() -> None:
    global _last_sweep
    now = time.time()
    with _sweep_lock:
        if now - _last_sweep < SWEEP_DEBOUNCE:
            return
        _last_sweep = now

    def _work() -> None:
        try:
            conn = core.connect()
        except Exception:
            return
        try:
            core.sweep_unanalyzed(conn)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    threading.Thread(target=_work, daemon=True, name="token-lens-sweep").start()


def _config() -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config  # type: ignore
        raw = load_config() or {}
        entries = (((raw.get("plugins") or {}).get("entries") or {})
                   .get("token-lens") or {})
        if isinstance(entries, dict):
            cfg = dict(entries)
    except Exception:
        cfg = {}
    cfg.setdefault("min_sessions", 10)
    cfg.setdefault("detector_min_sessions", 3)
    cfg.setdefault("refresh_every", 5)
    cfg.setdefault("max_suggestions_shown", MAX_SUGGESTIONS_SHOWN)
    cfg.setdefault("backfill_window_days", 30)
    return cfg


def _window_rollups(conn, window: str) -> List[sqlite3.Row]:
    if window == "session":
        latest = conn.execute(
            "SELECT session_id FROM session_rollups WHERE provenance='recorder' "
            "ORDER BY ended_ts DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            return []
        return conn.execute(
            "SELECT * FROM session_rollups WHERE session_id=?",
            (latest["session_id"],),
        ).fetchall()
    start, end = core.window_bounds(window)
    return conn.execute(
        "SELECT * FROM session_rollups WHERE ended_ts >= ? AND ended_ts <= ?",
        (start, end),
    ).fetchall()


def _top_level(buckets: Dict[str, float]) -> Dict[str, float]:
    """Roll dynamic children up to frozen top-level IDs (design review D14)."""
    out: Dict[str, float] = {}
    for k, v in buckets.items():
        if k.startswith("tool_schemas.mcp"):
            top = "tool_schemas.mcp"
        elif k.startswith("tool_schemas"):
            top = "tool_schemas.builtin"
        elif k.startswith("tool_results"):
            top = "tool_results"
        elif k.startswith("history.user"):
            top = "history.user"
        elif k.startswith("history.assistant"):
            top = "history.assistant"
        else:
            top = k if k in core.CATEGORY_IDS else "unattributed"
        out[top] = out.get(top, 0.0) + v
    return out


def _children(buckets: Dict[str, float], parent: str) -> Dict[str, float]:
    return {
        k: v for k, v in buckets.items()
        if k.startswith(parent + ".") and k != parent
    }


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------

@router.get("/summary")
def summary(window: str = Query("7d", pattern="^(session|24h|7d)$")) -> Dict[str, Any]:
    conn = _conn()
    try:
        _schedule_sweep()
        rows = _window_rollups(conn, window)
        buckets, totals, est_share = core.aggregate_buckets(rows)
        api_calls = sum(r["api_calls"] for r in rows)
        prompt_side = totals["input"] + totals["cache_read"] + totals["cache_write"]
        cache_hit = (totals["cache_read"] / prompt_side) if prompt_side else 0.0
        return {
            "window": window,
            # D16: total = input + cache + output, matching core Analytics.
            "total_tokens": totals["billed"],
            "api_calls": api_calls,
            "cache_hit_rate": round(cache_hit, 4),
            "sessions": len(rows),
            # D5 partial rule: badge exact only when 100% of window is exact.
            "precision": "exact" if est_share == 0 and rows else "estimated",
            "estimated_share_pct": est_share,
            "totals": totals,
            "has_any_data": _has_any_data(conn),
        }
    finally:
        conn.close()


def _has_any_data(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM api_calls LIMIT 1").fetchone()
                or conn.execute("SELECT 1 FROM session_rollups LIMIT 1").fetchone())


@router.get("/categories")
def categories(window: str = Query("7d", pattern="^(session|24h|7d)$")) -> Dict[str, Any]:
    conn = _conn()
    try:
        _schedule_sweep()
        rows = _window_rollups(conn, window)
        buckets, totals, est_share = core.aggregate_buckets(rows)
        top = _top_level(buckets)
        children = {
            parent: _children(buckets, parent)
            for parent in ("tool_schemas.mcp", "tool_results")
            if _children(buckets, parent)
        }
        return {
            "window": window,
            "total": totals["billed"],
            "categories": top,
            "children": children,
            "estimated_share_pct": est_share,
            "unattributed_share_pct": (
                round(top.get("unattributed", 0) / totals["billed"] * 100, 1)
                if totals["billed"] else 0.0
            ),
        }
    finally:
        conn.close()


@router.get("/timeseries")
def timeseries(window: str = Query("7d", pattern="^(24h|7d)$")) -> Dict[str, Any]:
    """Stacked by token CATEGORY only — never by model (board feedback D20).
    7d: one bar per day; 24h: one bar per session. Stacks render the top-5
    top-level categories by window total + 'other' (D14)."""
    conn = _conn()
    try:
        _schedule_sweep()
        rows = _window_rollups(conn, window)
        all_buckets, _totals, _ = core.aggregate_buckets(rows)
        top5 = [k for k, _v in sorted(
            _top_level(all_buckets).items(), key=lambda kv: -kv[1]
        )[:5]]

        bars: List[Dict[str, Any]] = []
        if window == "24h":
            for r in sorted(rows, key=lambda r: r["ended_ts"] or 0):
                try:
                    b = _top_level(json.loads(r["buckets_json"]) or {})
                except Exception:
                    b = {}
                bars.append(_bar(r["session_id"], b, top5,
                                 estimated=r["precision"] == "estimated",
                                 ts=r["ended_ts"]))
        else:
            days: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                ts = r["ended_ts"] or r["analyzed_at"]
                day = time.strftime("%Y-%m-%d", time.localtime(ts))
                slot = days.setdefault(day, {"buckets": {}, "estimated": False})
                try:
                    for k, v in _top_level(json.loads(r["buckets_json"]) or {}).items():
                        slot["buckets"][k] = slot["buckets"].get(k, 0.0) + v
                except Exception:
                    pass
                if r["precision"] == "estimated":
                    slot["estimated"] = True
            # labeled zero-height bars for empty days (D5 sparse-state rule)
            now = time.time()
            for offset in range(6, -1, -1):
                day = time.strftime("%Y-%m-%d", time.localtime(now - offset * 86400))
                slot = days.get(day, {"buckets": {}, "estimated": False})
                bars.append(_bar(day, slot["buckets"], top5,
                                 estimated=slot["estimated"], ts=None))
        return {"window": window, "stack_categories": top5 + ["other"], "bars": bars}
    finally:
        conn.close()


def _bar(label: str, buckets: Dict[str, float], top5: List[str],
         *, estimated: bool, ts: Optional[float]) -> Dict[str, Any]:
    segs = {k: buckets.get(k, 0.0) for k in top5}
    segs["other"] = sum(v for k, v in buckets.items() if k not in top5)
    return {"label": label, "segments": segs, "estimated": estimated, "ts": ts}


@router.get("/by-model")
def by_model(window: str = Query("7d", pattern="^(session|24h|7d)$")) -> Dict[str, Any]:
    conn = _conn()
    try:
        start, end = core.window_bounds(window)
        rows = conn.execute(
            """
            SELECT model,
                   SUM(actual_input + actual_cache_read + actual_cache_write) AS input,
                   SUM(actual_output) AS output,
                   COUNT(*) AS calls
            FROM api_calls
            WHERE ts >= ? AND ts <= ? AND status='complete' AND model != ''
            GROUP BY model ORDER BY input + output DESC
            """,
            (start, end),
        ).fetchall()
        if rows:
            return {"window": window, "models": [dict(r) for r in rows],
                    "estimated": False}
        # M3-T3: backfill-only windows have no recorder api_calls — fall back
        # to core state.db session accumulators so the table matches the
        # charts after a 30-day import; badged estimated in the UI.
        models = _by_model_from_core(start, end)
        return {"window": window, "models": models, "estimated": bool(models)}
    finally:
        conn.close()


def _by_model_from_core(start: float, end: float) -> list:
    try:
        src = core.open_core_db_readonly()
        if src is None:
            return []
        try:
            rows = src.execute(
                """
                SELECT model,
                       SUM(input_tokens + cache_read_tokens + cache_write_tokens) AS input,
                       SUM(output_tokens) AS output,
                       SUM(api_call_count) AS calls
                FROM sessions
                WHERE started_at >= ? AND started_at <= ? AND model != ''
                      AND model IS NOT NULL
                GROUP BY model ORDER BY input + output DESC
                """,
                (start, end),
            ).fetchall()
            return [dict(r) for r in rows if (r["input"] or 0) + (r["output"] or 0) > 0]
        finally:
            src.close()
    except Exception:
        return []


@router.get("/sessions/{session_id}")
def session_detail(session_id: str) -> Dict[str, Any]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM session_rollups WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no rollup for session")
        buckets = json.loads(row["buckets_json"] or "{}")
        return {
            "session_id": session_id,
            "precision": row["precision"],
            "provenance": row["provenance"],
            "totals": json.loads(row["totals_json"] or "{}"),
            "categories": _top_level(buckets),
            "children": {
                p: _children(buckets, p)
                for p in ("tool_schemas.mcp", "tool_results")
                if _children(buckets, p)
            },
            "api_calls": row["api_calls"],
            "turns": row["turns"],
            "started_ts": row["started_ts"],
            "ended_ts": row["ended_ts"],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

@router.get("/suggestions")
def suggestions() -> Dict[str, Any]:
    conn = _conn()
    try:
        _schedule_sweep()
        cfg = _config()
        # latest row per fingerprint
        rows = conn.execute(
            """
            SELECT s.* FROM suggestions s
            JOIN (SELECT fingerprint, MAX(created_at) AS mc
                  FROM suggestions GROUP BY fingerprint) latest
              ON s.fingerprint = latest.fingerprint AND s.created_at = latest.mc
            ORDER BY s.est_savings_pct DESC
            """
        ).fetchall()
        shown = [dict(r) for r in rows if r["status"] == "shown"]
        hidden_count = sum(1 for r in rows if r["status"] == "hidden")
        acted: List[Dict[str, Any]] = []
        for r in rows:
            if r["status"] != "done":
                continue
            d = dict(r)
            d["observed"] = core.observed_delta(
                conn, category=r["category"], done_at=r["status_changed_at"] or r["created_at"]
            )
            acted.append(d)

        det_ok, det_reason = core.gate_check(
            conn, kind="detector",
            min_sessions=int(cfg["detector_min_sessions"]),
        )
        llm_ok, llm_reason = core.gate_check(
            conn, kind="llm", min_sessions=int(cfg["min_sessions"]),
            refresh_every=int(cfg["refresh_every"]),
        )
        refresh_row = conn.execute(
            "SELECT * FROM refresh_requests ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "suggestions": shown[: int(cfg["max_suggestions_shown"])],
            "hidden_count": hidden_count,
            "acted_on": acted,
            "gates": {
                "detector": {"open": det_ok, "reason": det_reason,
                             "min_sessions": int(cfg["detector_min_sessions"])},
                "llm": {"open": llm_ok, "reason": llm_reason,
                        "min_sessions": int(cfg["min_sessions"])},
                "observed_sessions": core.recorder_observed_session_count(conn),
            },
            "refresh": dict(refresh_row) if refresh_row else None,
            "rubric_version": conn.execute(
                "SELECT MAX(version) FROM rubric_versions"
            ).fetchone()[0],
        }
    finally:
        conn.close()


@router.post("/suggestions/refresh")
def refresh() -> Dict[str, Any]:
    """Enqueue a manual refresh. Execution is agent-side (the drain runs at
    the next session activity); manual requests bypass refresh_every but
    keep min_sessions and a 1-hour cooldown."""
    conn = _conn()
    try:
        last = conn.execute(
            "SELECT requested_at FROM refresh_requests WHERE source='manual' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last and time.time() - last["requested_at"] < MANUAL_REFRESH_COOLDOWN:
            wait = int(MANUAL_REFRESH_COOLDOWN - (time.time() - last["requested_at"]))
            raise HTTPException(status_code=429, detail={
                "error": "cooldown", "retry_after_s": wait,
                "message": f"manual refresh cooldown — try again in {wait // 60 + 1} min",
            })
        with core.write_txn(conn):
            cur = conn.execute(
                "INSERT INTO refresh_requests (requested_at, source) VALUES (?, 'manual')",
                (time.time(),),
            )
        spawned = _spawn_refresh_process()
        return {
            "queued": True,
            "request_id": cur.lastrowid,
            "spawned": spawned,
            "message": ("refresh running — results in ~1 min" if spawned
                        else "refresh queued — runs at next session activity"),
        }
    finally:
        conn.close()


def _spawn_refresh_process() -> bool:
    """Spawn `hermes token-lens refresh` detached (mirrors web_server's
    `_spawn_hermes_action` pattern — that helper is name-gated to a fixed
    dict, so plugins replicate it). The spawned process is a real Hermes
    process: loads the plugin, atomically claims the queue row, honors all
    gates, executes via ctx.llm with the trust gate intact, exits. Failure
    is non-fatal — the agent-side drain executes the queued row at the next
    session activity (plan §Suggestion engine)."""
    import os
    import subprocess
    try:
        log_dir = core.default_db_path().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(log_dir / "token-lens-refresh.log", "ab", buffering=0)
        log_file.write(
            f"\n=== refresh spawned {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode()
        )
        subprocess.Popen(
            [sys.executable, "-m", "hermes_cli.main", "token-lens", "refresh"],
            stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
            env={**os.environ, "HERMES_NONINTERACTIVE": "1"},
            start_new_session=True,
        )
        return True
    except Exception:
        return False


@router.post("/suggestions/{suggestion_id}/dismiss")
def dismiss(suggestion_id: int) -> Dict[str, Any]:
    return _set_status(suggestion_id, "dismissed")


@router.post("/suggestions/{suggestion_id}/done")
def done(suggestion_id: int) -> Dict[str, Any]:
    return _set_status(suggestion_id, "done")


def _set_status(suggestion_id: int, status: str) -> Dict[str, Any]:
    conn = _conn()
    try:
        with core.write_txn(conn):
            ok = core.set_suggestion_status(conn, suggestion_id, status)
        if not ok:
            raise HTTPException(status_code=404, detail="unknown suggestion id")
        return {"id": suggestion_id, "status": status}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Backfill (background job; chunked commits resume after a crash)
# ---------------------------------------------------------------------------

_backfill_lock = threading.Lock()
_backfill_job: Dict[str, Any] = {"state": "idle"}


@router.post("/backfill")
def start_backfill(days: int = Query(0, ge=0, le=365)) -> Dict[str, Any]:
    cfg = _config()
    days = days or int(cfg["backfill_window_days"])
    with _backfill_lock:
        if _backfill_job.get("state") == "running":
            return {"job": dict(_backfill_job), "already_running": True}
        _backfill_job.clear()
        _backfill_job.update({
            "state": "running", "days": days, "done": 0, "total": None,
            "started_at": time.time(),
        })

    def _work() -> None:
        try:
            conn = core.connect()
        except Exception as exc:
            with _backfill_lock:
                _backfill_job.update({"state": "failed", "error": str(exc)})
            return
        try:
            def progress(done: int, total: int) -> None:
                with _backfill_lock:
                    _backfill_job.update({"done": done, "total": total})

            result = core.backfill(conn, days=days, progress_cb=progress)
            with _backfill_lock:
                _backfill_job.update({"state": "done", **result})
        except Exception as exc:
            with _backfill_lock:
                _backfill_job.update({"state": "failed", "error": str(exc)})
        finally:
            try:
                conn.close()
            except Exception:
                pass

    threading.Thread(target=_work, daemon=True, name="token-lens-backfill").start()
    return {"job": dict(_backfill_job)}


@router.get("/backfill/status")
def backfill_status() -> Dict[str, Any]:
    with _backfill_lock:
        return {"job": dict(_backfill_job)}


# ---------------------------------------------------------------------------
# Meta + health
# ---------------------------------------------------------------------------

@router.get("/meta")
def meta() -> Dict[str, Any]:
    conn = _conn()
    try:
        week_ago = time.time() - 7 * 86400
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens_input),0) AS ti,"
            " COALESCE(SUM(tokens_output),0) AS toq, COUNT(*) AS runs"
            " FROM suggestion_runs WHERE ts >= ?",
            (week_ago,),
        ).fetchone()
        rules_version, _ = core.load_rules(conn)
        rubric_row = conn.execute(
            "SELECT MAX(version) FROM rubric_versions"
        ).fetchone()
        return {
            "overhead_tokens_week": int(row["ti"] + row["toq"]),
            "runs_week": row["runs"],
            "analyzer_version": core.ANALYZER_VERSION,
            "rules_version": rules_version,
            "rubric_version": rubric_row[0],
        }
    finally:
        conn.close()


@router.get("/health")
def health() -> Dict[str, Any]:
    conn = _conn()
    try:
        _schedule_sweep()
        rules_version, _ = core.load_rules(conn)
        incomplete = conn.execute(
            "SELECT COUNT(*) FROM api_calls WHERE status='incomplete'"
        ).fetchone()[0]
        no_usage = conn.execute(
            "SELECT COUNT(*) FROM api_calls WHERE status='no_usage'"
        ).fetchone()[0]
        scales = [r[0] for r in conn.execute(
            "SELECT calib_scale FROM api_calls WHERE calib_scale IS NOT NULL "
            "ORDER BY ts DESC LIMIT 500"
        ).fetchall()]
        median_scale = sorted(scales)[len(scales) // 2] if scales else None
        drift_alert = bool(median_scale and abs(median_scale - 1.0) > 0.25)

        breaker = {"tripped": False}
        row = conn.execute("SELECT value FROM meta_kv WHERE key='breaker'").fetchone()
        if row:
            try:
                breaker = json.loads(row["value"])
            except Exception:
                pass

        # unattributed share alert (>10% — plan taxonomy)
        rows = _window_rollups(conn, "7d")
        buckets, totals, _ = core.aggregate_buckets(rows)
        top = _top_level(buckets)
        unattributed_share = (
            top.get("unattributed", 0) / totals["billed"] * 100
            if totals["billed"] else 0.0
        )

        # recorder-not-detected (design review D5): zero recorded calls but
        # core has sessions newer than plugin install -> hooks not loaded.
        recorder_detected = bool(conn.execute(
            "SELECT 1 FROM api_calls LIMIT 1"
        ).fetchone())
        recorder_warning = None
        if not recorder_detected:
            install_row = conn.execute(
                "SELECT value FROM meta_kv WHERE key='install_ts'"
            ).fetchone()
            install_ts = float(install_row["value"]) if install_row else 0.0
            try:
                src = core.open_core_db_readonly()
                if src is not None:
                    try:
                        newer = src.execute(
                            "SELECT COUNT(*) FROM sessions WHERE started_at > ?",
                            (install_ts,),
                        ).fetchone()[0]
                    finally:
                        src.close()
                    if newer > 0:
                        recorder_warning = (
                            "Token Lens isn't recording — restart your "
                            "gateway/CLI session (hooks load at process start)."
                        )
            except Exception:
                pass

        unanalyzed = conn.execute(
            """
            SELECT COUNT(DISTINCT c.session_id) FROM api_calls c
            LEFT JOIN session_rollups r ON r.session_id = c.session_id
            WHERE c.session_id IS NOT NULL AND r.session_id IS NULL
            """
        ).fetchone()[0]

        return {
            "schema_version": core.SCHEMA_VERSION,
            "analyzer_version": core.ANALYZER_VERSION,
            "rules_version": rules_version,
            "incomplete_calls": incomplete,
            "no_usage_calls": no_usage,
            "calib_median_scale": median_scale,
            "calib_drift_alert": drift_alert,
            "breaker": breaker,
            "unattributed_share_pct": round(unattributed_share, 1),
            "unattributed_alert": unattributed_share > 10.0,
            "unanalyzed_sessions": unanalyzed,
            "recorder_detected": recorder_detected,
            "recorder_warning": recorder_warning,
        }
    finally:
        conn.close()
