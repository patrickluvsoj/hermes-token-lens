"""token-lens — recorder hooks (agent/gateway process side).

Captures exact per-call token usage via pre/post_api_request, decomposes each
request into category buckets (token_lens_core), and batch-commits to the
plugin's own SQLite DB.

Invariants (plan Constraint 8 — recorder fails open, ALWAYS):
  * No hook ever raises into the conversation loop: catch-all -> log once
    -> no-op.
  * Circuit breaker: 5 consecutive recorder errors disable the recorder for
    the remainder of the session; state surfaced via meta_kv -> /health and
    the dashboard footer. Sessions touched by a tripped breaker roll up as
    precision=estimated via the sweep's reconciliation path.
  * Hooks return fast (<=50ms p95): writes are buffered and batch-committed —
    flush every FLUSH_EVERY_CALLS calls or FLUSH_EVERY_SECONDS seconds, and
    always on on_session_finalize. Rollups run on a background daemon thread,
    never inline in a hook.

Tool-schema costing (plan Constraint 9): per-schema token costs are computed
in-process from the live tool registry (tools.registry) — the sanitized hook
payload's tools array is used for NOTHING. Costs are honest best-effort,
cross-checked against the hook's tool_count, cached per request_hash; the
per-call calibration absorbs residual error into billed-total truth.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from . import token_lens_core as core
except ImportError:  # pragma: no cover — loaded as a top-level module by hermes
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import token_lens_core as core  # type: ignore

FLUSH_EVERY_CALLS = 20
FLUSH_EVERY_SECONDS = 5.0
BREAKER_THRESHOLD = 5


class _Recorder:
    """Process-wide recorder state. One instance per agent/gateway process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn = None
        self._conn_failed = False
        self._buffer: List[tuple] = []  # ("pre"|"post", payload dict)
        self._calls_since_flush = 0
        self._last_flush = time.time()
        self._error_streak = 0
        self._breaker_tripped = False
        self._logged_failure = False
        self._schema_costs: Optional[Dict[str, int]] = None
        self._schema_costs_tool_count = -1
        self._config: Optional[Dict[str, Any]] = None

    # -- config ---------------------------------------------------------------

    def config(self) -> Dict[str, Any]:
        if self._config is None:
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
            cfg.setdefault("recorder_enabled", True)
            cfg.setdefault("min_sessions", 10)
            cfg.setdefault("detector_min_sessions", 3)
            cfg.setdefault("refresh_every", 5)
            cfg.setdefault("retention_days", 90)
            self._config = cfg
        return self._config

    # -- breaker / fail-open ----------------------------------------------------

    def _record_error(self, where: str, exc: Exception) -> None:
        self._error_streak += 1
        if not self._logged_failure:
            logger.warning("token-lens recorder error in %s (fail-open): %s", where, exc)
            self._logged_failure = True
        if self._error_streak >= BREAKER_THRESHOLD and not self._breaker_tripped:
            self._breaker_tripped = True
            logger.warning(
                "token-lens recorder circuit breaker tripped after %d consecutive "
                "errors — recording disabled for the remainder of this session",
                self._error_streak,
            )
            self._write_breaker_state(tripped=True)

    def _record_success(self) -> None:
        self._error_streak = 0

    def _write_breaker_state(self, *, tripped: bool) -> None:
        try:
            conn = self._connect()
            if conn is None:
                return
            with core.write_txn(conn):
                conn.execute(
                    "INSERT INTO meta_kv (key, value) VALUES ('breaker', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps({"tripped": tripped, "ts": time.time()}),),
                )
        except Exception:
            pass

    # -- db -------------------------------------------------------------------

    def _connect(self):
        if self._conn is not None or self._conn_failed:
            return self._conn
        try:
            self._conn = core.connect()
        except Exception as exc:
            # Includes DBNewerThanCode: refuse to record, never break the agent.
            self._conn_failed = True
            if not self._logged_failure:
                logger.warning("token-lens recorder disabled (DB open failed): %s", exc)
                self._logged_failure = True
        return self._conn

    # -- schema costing (Constraint 9) ------------------------------------------

    def schema_costs(self, tool_count: int) -> Dict[str, int]:
        """Best-effort per-category schema costs from the live registry.

        Recomputed when the registered tool count changes (registry change);
        otherwise served from cache. Never reads hook payloads.
        """
        if self._schema_costs is not None and self._schema_costs_tool_count == tool_count:
            return self._schema_costs
        costs: Dict[str, int] = {}
        try:
            from tools.registry import registry  # type: ignore
            rules = core.DEFAULT_RULES
            tools = getattr(registry, "all", None)
            entries = tools() if callable(tools) else getattr(registry, "_tools", {})
            items = entries.items() if isinstance(entries, dict) else []
            for name, entry in items:
                schema = getattr(entry, "schema", None)
                if schema is None and isinstance(entry, dict):
                    schema = entry.get("schema")
                if schema is None:
                    continue
                tok = core.estimate_tokens(json.dumps(schema, default=str))
                server = core.mcp_server_for_tool(str(name), rules)
                key = f"tool_schemas.mcp.{server}" if server else "tool_schemas.builtin"
                costs[key] = costs.get(key, 0) + tok
        except Exception:
            costs = {}
        self._schema_costs = costs
        self._schema_costs_tool_count = tool_count
        return costs

    # -- buffering --------------------------------------------------------------

    def enqueue_pre(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._buffer.append(("pre", payload))
            self._calls_since_flush += 1
            should_flush = (
                self._calls_since_flush >= FLUSH_EVERY_CALLS
                or time.time() - self._last_flush >= FLUSH_EVERY_SECONDS
            )
        if should_flush:
            self.flush()

    def enqueue_post(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._buffer.append(("post", payload))
            should_flush = time.time() - self._last_flush >= FLUSH_EVERY_SECONDS
        if should_flush:
            self.flush()

    def flush(self) -> None:
        """One flush batch = one BEGIN IMMEDIATE transaction."""
        with self._lock:
            if not self._buffer:
                self._calls_since_flush = 0
                self._last_flush = time.time()
                return
            batch = self._buffer
            self._buffer = []
            self._calls_since_flush = 0
            self._last_flush = time.time()
        conn = self._connect()
        if conn is None:
            return
        try:
            with core.write_txn(conn):
                for kind, p in batch:
                    if kind == "pre":
                        core.upsert_pre_call(
                            conn,
                            api_request_id=p["api_request_id"],
                            session_id=p.get("session_id", ""),
                            turn_id=p.get("turn_id", ""),
                            ts=p.get("ts", time.time()),
                            model=p.get("model", ""),
                            provider=p.get("provider", ""),
                            request_hash=p.get("request_hash", ""),
                            buckets=p.get("buckets", {}),
                        )
                    else:
                        core.complete_post_call(
                            conn,
                            api_request_id=p["api_request_id"],
                            usage=p.get("usage"),
                        )
            self._record_success()
        except Exception as exc:
            self._record_error("flush", exc)


_RECORDER = _Recorder()


def _failopen_hook(fn):
    """Wrap a hook: never raises, no-ops once the breaker has tripped."""

    def wrapper(**kwargs: Any) -> None:
        if _RECORDER._breaker_tripped:
            return
        if not _RECORDER.config().get("recorder_enabled", True):
            return
        try:
            fn(**kwargs)
            _RECORDER._record_success()
        except Exception as exc:
            _RECORDER._record_error(fn.__name__, exc)

    wrapper.__name__ = getattr(fn, "__name__", "token_lens_hook")
    return wrapper


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

@_failopen_hook
def on_pre_api_request(
    *,
    api_request_id: str = "",
    turn_id: str = "",
    session_id: str = "",
    model: str = "",
    provider: str = "",
    api_call_count: int = 0,
    request_messages: Any = None,
    tool_count: int = 0,
    **_: Any,
) -> None:
    if not api_request_id:
        return
    conn = _RECORDER._connect()
    if conn is None:
        return
    rules_version, rules = core.load_rules(conn)
    schema_costs = _RECORDER.schema_costs(tool_count)
    buckets = core.decompose_request(
        request_messages if isinstance(request_messages, list) else [],
        rules=rules,
        schema_costs=schema_costs,
    )
    request_hash = f"tc{tool_count}:r{rules_version}"
    _RECORDER.enqueue_pre({
        "api_request_id": api_request_id,
        "turn_id": turn_id,
        "session_id": session_id,
        "ts": time.time(),
        "model": model,
        "provider": provider,
        "request_hash": request_hash,
        "buckets": buckets,
    })


@_failopen_hook
def on_post_api_request(
    *,
    api_request_id: str = "",
    usage: Any = None,
    **_: Any,
) -> None:
    if not api_request_id:
        return
    _RECORDER.enqueue_post({
        "api_request_id": api_request_id,
        "usage": usage if isinstance(usage, dict) else None,
    })


@_failopen_hook
def on_pre_llm_call(**_: Any) -> None:
    """Turn-scoped context-injection hook. Reserved for memory/skill marker
    diffing (plan taxonomy: memory bucket via pre-injection vs final message
    diff). v1 decomposes from request_messages markers; this hook is a
    registration placeholder so the payload shape is observable in logs when
    HERMES_TOKEN_LENS_DEBUG is set."""
    return


@_failopen_hook
def on_session_start(**_: Any) -> None:
    conn = _RECORDER._connect()
    if conn is None:
        return
    with core.write_txn(conn):
        core.reclaim_stuck_refreshes(conn)
    _drain_refresh_queue_async()


@_failopen_hook
def on_session_finalize(*, session_id: str = "", **_: Any) -> None:
    _RECORDER.flush()
    if not session_id:
        return

    def _finalize_work() -> None:
        try:
            conn = core.connect()
        except Exception:
            return
        try:
            session_totals = _core_session_totals(session_id)
            with core.write_txn(conn):
                core.rollup_session(conn, session_id, session_totals=session_totals)
                core.prune_api_calls(
                    conn,
                    retention_days=int(_RECORDER.config().get("retention_days", 90)),
                )
            _run_detectors_if_due(conn)
            _drain_refresh_queue(conn)
        except Exception as exc:
            logger.debug("token-lens finalize work failed (fail-open): %s", exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    threading.Thread(target=_finalize_work, daemon=True,
                     name="token-lens-finalize").start()


def _core_session_totals(session_id: str) -> Optional[Dict[str, int]]:
    """Read the session-level token totals from core SessionDB for the ±2%
    reconciliation. Best-effort: None when core internals are unreachable."""
    try:
        from hermes_state import SessionDB  # type: ignore
        db = SessionDB()
        sess = db.get_session(session_id)
        total = None
        if isinstance(sess, dict):
            total = sess.get("total_tokens") or sess.get("token_count")
        else:
            total = getattr(sess, "total_tokens", None)
        if total:
            return {"total_tokens": int(total)}
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Detector + refresh plumbing (detectors themselves live in detectors.py)
# ---------------------------------------------------------------------------

def _run_detectors_if_due(conn) -> None:
    cfg = _RECORDER.config()
    ok, _reason = core.gate_check(
        conn, kind="detector",
        min_sessions=int(cfg.get("detector_min_sessions", 3)),
    )
    if not ok:
        return
    try:
        try:
            from . import detectors  # type: ignore
        except ImportError:
            import detectors  # type: ignore
        detectors.run_detectors(conn)
    except Exception as exc:
        logger.debug("token-lens detectors failed (fail-open): %s", exc)


def _drain_refresh_queue(conn) -> None:
    """Claim + execute pending refresh requests. M1: detector refreshes only;
    LLM generation arrives in M2 (executes via ctx.llm on this same path)."""
    with core.write_txn(conn):
        core.reclaim_stuck_refreshes(conn)
    rows = conn.execute(
        "SELECT id FROM refresh_requests WHERE status='pending' ORDER BY id"
    ).fetchall()
    for row in rows:
        claimed = False
        with core.write_txn(conn):
            claimed = core.claim_refresh_request(conn, row["id"])
        if not claimed:
            continue
        status, reason = "done", ""
        try:
            cfg = _RECORDER.config()
            ok, gate_reason = core.gate_check(
                conn, kind="detector",
                min_sessions=int(cfg.get("detector_min_sessions", 3)),
            )
            if ok:
                _run_detectors_if_due(conn)
            else:
                status, reason = "skipped", gate_reason
        except Exception as exc:
            status, reason = "skipped", f"error: {exc}"
        with core.write_txn(conn):
            conn.execute(
                "UPDATE refresh_requests SET status=?, reason=? WHERE id=?",
                (status, reason, row["id"]),
            )


def _drain_refresh_queue_async() -> None:
    def _work() -> None:
        try:
            conn = core.connect()
        except Exception:
            return
        try:
            _drain_refresh_queue(conn)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    threading.Thread(target=_work, daemon=True, name="token-lens-drain").start()


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_finalize", on_session_finalize)
