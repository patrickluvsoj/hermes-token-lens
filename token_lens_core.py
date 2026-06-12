"""token_lens_core — shared logic for the Token Lens plugin.

Imported by BOTH processes:
  - the agent/gateway process (``__init__.py`` recorder hooks)
  - the dashboard process (``dashboard/plugin_api.py`` via sys.path insertion —
    the dashboard's importlib loader does not guarantee sibling-module
    resolution)

Everything here is self-contained: imports of hermes internals are lazy with
fallbacks, so this module works in tests without a Hermes checkout on the
path and fails open at runtime if upstream internals move.

Multi-process SQLite discipline (any process may write):

    ┌─ agent/gateway ──────────┐        ┌─ dashboard ───────────────┐
    │ recorder hooks (buffered)│  WAL   │ plugin_api reads          │
    │ rollups (bg thread)      │◄──────►│ dismiss/done writes       │
    │ suggestion runs          │ sqlite │ backfill job, sweep       │
    └──────────────────────────┘        └───────────────────────────┘

  * WAL mode + busy_timeout=5000 + synchronous=NORMAL
  * every multi-statement write opens with BEGIN IMMEDIATE (deferred
    transactions upgrade read→write mid-flight and fail SQLITE_BUSY
    ignoring busy_timeout)
  * reads stay autocommit
  * schema versioned via PRAGMA user_version; newer-than-code DBs refuse
    to open (recorder fails open, dashboard shows the mismatch card)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1

# Frozen top-level category IDs (design review D14: each owns a fixed palette
# slot in the UI; IDs never change — only mapping RULES evolve).
CATEGORY_IDS = [
    "system_prompt",
    "tool_schemas.builtin",
    "tool_schemas.mcp",
    "skill_loading",
    "memory",
    "history.user",
    "history.assistant",
    "tool_results",
    "output",
    "reasoning",
    "unattributed",
]

# Dismissed suggestions resurrect only when recomputed savings reach this
# multiple of the savings recorded at dismissal time (eng delta review D23).
DISMISS_RESURRECT_FACTOR = 2.0

_SKILLS_BLOCK_RE = re.compile(r"<available_skills>.*?</available_skills>", re.DOTALL)


# ---------------------------------------------------------------------------
# Token estimation (mirrors agent/model_metadata.py estimate_tokens_rough;
# lazy-imports the real one when running inside Hermes so drift is bounded)
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough token estimate, ceiling division so short strings never count 0."""
    if not text:
        return 0
    try:  # prefer the platform's own estimator when importable
        from agent.model_metadata import estimate_tokens_rough  # type: ignore
        return estimate_tokens_rough(text)
    except Exception:
        return (len(text) + 3) // 4


def _content_to_text(content: Any) -> str:
    """Flatten a message ``content`` field (str or content-parts list) to text.

    Image parts count as a flat ~1500-token sentinel string so a screenshot
    doesn't estimate as 250K tokens of base64 (same policy as
    estimate_messages_tokens_rough upstream).
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: List[str] = []
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type", "")
                if ptype in ("image_url", "image", "input_image"):
                    out.append(" " * 6000)  # ~1500 tokens at 4 chars/token
                else:
                    text = part.get("text") or part.get("content") or ""
                    if isinstance(text, str):
                        out.append(text)
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(out)
    return str(content)


# ---------------------------------------------------------------------------
# DB plumbing
# ---------------------------------------------------------------------------

def default_db_path() -> Path:
    home = os.environ.get("HERMES_HOME", "") or os.path.expanduser("~/.hermes")
    return Path(home) / "token_lens.db"


class DBNewerThanCode(RuntimeError):
    """The on-disk DB was written by a newer plugin version. Refuse to open."""

    def __init__(self, db_version: int):
        self.db_version = db_version
        super().__init__(
            f"token_lens.db is schema v{db_version}, this plugin understands "
            f"v{SCHEMA_VERSION}. Upgrade the plugin or remove the DB."
        )


_MIGRATIONS: Dict[int, str] = {
    # v0 -> v1: initial schema. Idempotent (IF NOT EXISTS everywhere).
    1: """
    CREATE TABLE IF NOT EXISTS api_calls (
        id INTEGER PRIMARY KEY,
        api_request_id TEXT UNIQUE NOT NULL,
        session_id TEXT,
        turn_id TEXT,
        ts REAL NOT NULL,
        model TEXT DEFAULT '',
        provider TEXT DEFAULT '',
        request_hash TEXT DEFAULT '',
        actual_input INTEGER DEFAULT 0,
        actual_output INTEGER DEFAULT 0,
        actual_cache_read INTEGER DEFAULT 0,
        actual_cache_write INTEGER DEFAULT 0,
        actual_reasoning INTEGER DEFAULT 0,
        est_section_total INTEGER DEFAULT 0,
        calib_scale REAL,
        buckets_json TEXT DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'incomplete'
            CHECK (status IN ('complete','incomplete','no_usage'))
    );
    CREATE INDEX IF NOT EXISTS idx_api_calls_session ON api_calls(session_id, ts);
    CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls(ts);

    CREATE TABLE IF NOT EXISTS refresh_requests (
        id INTEGER PRIMARY KEY,
        requested_at REAL NOT NULL,
        source TEXT NOT NULL DEFAULT 'auto' CHECK (source IN ('manual','auto')),
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','running','done','skipped')),
        started_at REAL,
        reason TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS session_rollups (
        session_id TEXT PRIMARY KEY,
        analyzed_at REAL NOT NULL,
        analyzer_version INTEGER NOT NULL,
        rules_version INTEGER NOT NULL,
        precision TEXT NOT NULL DEFAULT 'estimated'
            CHECK (precision IN ('exact','estimated')),
        provenance TEXT NOT NULL DEFAULT 'recorder'
            CHECK (provenance IN ('recorder','backfill')),
        totals_json TEXT DEFAULT '{}',
        buckets_json TEXT DEFAULT '{}',
        api_calls INTEGER DEFAULT 0,
        turns INTEGER DEFAULT 0,
        started_ts REAL,
        ended_ts REAL
    );
    CREATE INDEX IF NOT EXISTS idx_rollups_ended ON session_rollups(ended_ts);

    CREATE TABLE IF NOT EXISTS category_rules (
        version INTEGER PRIMARY KEY,
        rules_json TEXT NOT NULL,
        rationale TEXT DEFAULT '',
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS suggestions (
        id INTEGER PRIMARY KEY,
        run_id INTEGER,
        fingerprint TEXT NOT NULL,
        title TEXT NOT NULL,
        evidence TEXT DEFAULT '',
        plan_md TEXT DEFAULT '',
        category TEXT DEFAULT '',
        est_savings_pct REAL DEFAULT 0,
        risk TEXT NOT NULL DEFAULT 'low' CHECK (risk IN ('low','medium','high')),
        scores_json TEXT DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'shown'
            CHECK (status IN ('shown','hidden','dismissed','done')),
        status_changed_at REAL,
        kind TEXT NOT NULL DEFAULT 'detector' CHECK (kind IN ('detector','llm')),
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_suggestions_fingerprint
        ON suggestions(fingerprint, created_at);

    CREATE TABLE IF NOT EXISTS suggestion_runs (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        kind TEXT NOT NULL DEFAULT 'detector' CHECK (kind IN ('detector','llm')),
        sessions_in_scope INTEGER DEFAULT 0,
        new_sessions_since_last INTEGER DEFAULT 0,
        model TEXT DEFAULT '',
        rubric_version INTEGER DEFAULT 0,
        tokens_input INTEGER DEFAULT 0,
        tokens_output INTEGER DEFAULT 0,
        purpose_breakdown_json TEXT DEFAULT '{}',
        watermark TEXT,
        UNIQUE (kind, watermark)
    );

    CREATE TABLE IF NOT EXISTS rubric_versions (
        version INTEGER PRIMARY KEY,
        rubric_md TEXT NOT NULL,
        rationale TEXT DEFAULT '',
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS meta_kv (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """,
}


def connect(db_path: Optional[Path] = None, *, allow_create: bool = True) -> sqlite3.Connection:
    """Open the Token Lens DB, migrating forward if needed.

    Raises :class:`DBNewerThanCode` when the DB's ``user_version`` is ahead of
    this module's ``SCHEMA_VERSION`` (user downgraded the plugin) — callers
    decide how to fail open (recorder: no-op; dashboard: error card).
    """
    path = Path(db_path) if db_path else default_db_path()
    if not allow_create and not path.exists():
        raise FileNotFoundError(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        conn.close()
        raise DBNewerThanCode(version)
    while version < SCHEMA_VERSION:
        target = version + 1
        script = _MIGRATIONS[target]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executescript(script)
            # executescript commits; user_version set separately and cheaply.
            conn.execute(f"PRAGMA user_version={target}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = target
    # Seed rules v1 if absent (idempotent).
    row = conn.execute("SELECT COUNT(*) FROM category_rules").fetchone()
    if row[0] == 0:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO category_rules (version, rules_json, rationale, created_at) "
            "VALUES (1, ?, 'v1 defaults seeded from hermes prompt markers', ?)",
            (json.dumps(DEFAULT_RULES), time.time()),
        )
        conn.commit()


def write_txn(conn: sqlite3.Connection):
    """Context manager: BEGIN IMMEDIATE → commit / rollback."""
    class _Txn:
        def __enter__(self):
            conn.execute("BEGIN IMMEDIATE")
            return conn

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
            return False

    return _Txn()


# ---------------------------------------------------------------------------
# Category mapping rules (versioned; only RULES evolve, never category IDs)
# ---------------------------------------------------------------------------

DEFAULT_RULES: Dict[str, Any] = {
    # System-message block markers checked in order; first match attributes the
    # block. Regexes over the system prompt text.
    "system_blocks": [
        {"category": "skill_loading", "pattern": r"<available_skills>.*?</available_skills>"},
        {"category": "memory", "pattern": r"(?ms)^## (?:Relevant )?[Mm]emor(?:y|ies).*?(?=^## |\Z)"},
        {"category": "memory", "pattern": r"(?ms)^## User [Pp]rofile.*?(?=^## |\Z)"},
    ],
    # tool name prefix -> mcp server extraction. Runtime MCP tools are named
    # mcp_<server>_<tool>; the raw config target server:tool is stored
    # alongside so disable-plans address config correctly (eng review D13/T9).
    "mcp_tool_prefix": r"^mcp_([a-zA-Z0-9-]+)_",
}


def load_rules(conn: sqlite3.Connection) -> Tuple[int, Dict[str, Any]]:
    row = conn.execute(
        "SELECT version, rules_json FROM category_rules ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return 1, dict(DEFAULT_RULES)
    try:
        return int(row["version"]), json.loads(row["rules_json"])
    except Exception:
        return int(row["version"]), dict(DEFAULT_RULES)


def mcp_server_for_tool(tool_name: str, rules: Dict[str, Any]) -> Optional[str]:
    pattern = rules.get("mcp_tool_prefix") or DEFAULT_RULES["mcp_tool_prefix"]
    m = re.match(pattern, tool_name or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Decomposition: request_messages -> estimated category sections
# ---------------------------------------------------------------------------

def decompose_system_prompt(system_text: str, rules: Dict[str, Any]) -> Dict[str, int]:
    """Split a system message into marker-attributed estimated-token buckets.

    Whatever the block rules don't claim stays ``system_prompt`` (the base
    identity/guidance prompt) — NOT unattributed; unattributed is reserved for
    deltas we observed but could not classify.
    """
    buckets: Dict[str, int] = {}
    remaining = system_text or ""
    for block in rules.get("system_blocks", DEFAULT_RULES["system_blocks"]):
        try:
            pattern = re.compile(block["pattern"], re.DOTALL)
        except re.error:
            continue
        claimed = 0
        for m in pattern.finditer(remaining):
            claimed += estimate_tokens(m.group(0))
        if claimed:
            cat = block["category"]
            buckets[cat] = buckets.get(cat, 0) + claimed
            remaining = pattern.sub("", remaining)
    base = estimate_tokens(remaining)
    if base:
        buckets["system_prompt"] = buckets.get("system_prompt", 0) + base
    return buckets


def decompose_request(
    request_messages: List[Dict[str, Any]],
    *,
    rules: Dict[str, Any],
    schema_costs: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """Estimate per-category input-token sections for one API call.

    ``request_messages`` is the FULL-FIDELITY passthrough from
    ``pre_api_request`` (never the sanitized/truncated ``request`` payload —
    Constraint 9). ``schema_costs`` is the in-process tool-schema costing from
    the recorder: {"tool_schemas.builtin": N, "tool_schemas.mcp.<server>": N}.
    The sanitized payload's tools array is used for NOTHING.
    """
    buckets: Dict[str, int] = {}

    def add(cat: str, n: int) -> None:
        if n > 0:
            buckets[cat] = buckets.get(cat, 0) + n

    last_user_seen = False
    messages = list(request_messages or [])
    # Identify the FINAL user message: history.user covers carried user turns;
    # the final user message is also history.user for v1 (current-turn input
    # is conversation history from the model's point of view).
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        text = _content_to_text(msg.get("content"))
        n = estimate_tokens(text)
        if role == "system":
            for cat, tok in decompose_system_prompt(text, rules).items():
                add(cat, tok)
        elif role == "user":
            add("history.user", n)
            last_user_seen = True
        elif role == "assistant":
            add("history.assistant", n)
            # tool_calls carried in history count toward assistant history
            tc = msg.get("tool_calls")
            if tc:
                add("history.assistant", estimate_tokens(json.dumps(tc, default=str)))
        elif role == "tool":
            tool_name = msg.get("name") or msg.get("tool_name") or ""
            server = mcp_server_for_tool(str(tool_name), rules)
            add(f"tool_results.{server}" if server else "tool_results", n)
        else:
            add("unattributed", n)

    for cat, cost in (schema_costs or {}).items():
        add(cat, cost)

    _ = last_user_seen  # reserved for future current-turn split
    return buckets


def calibrate(
    buckets: Dict[str, int], actual_prompt_tokens: Optional[int]
) -> Tuple[Dict[str, float], Optional[float]]:
    """Rescale estimated input buckets so they sum exactly to billed tokens.

    Returns (calibrated buckets, scale). When usage is missing
    (``actual_prompt_tokens`` is None) or the estimate sums to zero, returns
    the raw estimates with scale None — callers store status='no_usage' /
    precision='estimated' (the divide guard from test spec D5/Failure Modes).
    """
    est_total = sum(buckets.values())
    if not actual_prompt_tokens or est_total <= 0:
        return {k: float(v) for k, v in buckets.items()}, None
    scale = actual_prompt_tokens / est_total
    return {k: v * scale for k, v in buckets.items()}, scale


# ---------------------------------------------------------------------------
# Recorder writes (called from the agent process; buffered by __init__.py)
# ---------------------------------------------------------------------------

def upsert_pre_call(
    conn: sqlite3.Connection,
    *,
    api_request_id: str,
    session_id: str,
    turn_id: str,
    ts: float,
    model: str,
    provider: str,
    request_hash: str,
    buckets: Dict[str, int],
) -> None:
    """Insert/update the pre-call row. Provider retries re-fire pre with the
    same api_request_id — upsert-in-place keeps one row per logical call."""
    conn.execute(
        """
        INSERT INTO api_calls (api_request_id, session_id, turn_id, ts, model,
                               provider, request_hash, est_section_total,
                               buckets_json, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'incomplete')
        ON CONFLICT(api_request_id) DO UPDATE SET
            ts=excluded.ts, model=excluded.model, provider=excluded.provider,
            request_hash=excluded.request_hash,
            est_section_total=excluded.est_section_total,
            buckets_json=excluded.buckets_json
        """,
        (
            api_request_id, session_id or None, turn_id, ts, model, provider,
            request_hash, sum(buckets.values()), json.dumps(buckets),
        ),
    )


def complete_post_call(
    conn: sqlite3.Connection,
    *,
    api_request_id: str,
    usage: Optional[Dict[str, Any]],
) -> None:
    """Pair the post-call usage onto the pre row and calibrate its buckets.

    Missing/empty usage → status='no_usage', calib_scale NULL, uncalibrated
    estimates kept (D5 fallback). An unpaired post (no pre row — e.g. recorder
    enabled mid-session) inserts a minimal complete row so totals stay honest.
    """
    row = conn.execute(
        "SELECT buckets_json FROM api_calls WHERE api_request_id=?",
        (api_request_id,),
    ).fetchone()
    raw_buckets: Dict[str, int] = {}
    if row is not None:
        try:
            raw_buckets = json.loads(row["buckets_json"]) or {}
        except Exception:
            raw_buckets = {}

    if not usage:
        if row is not None:
            conn.execute(
                "UPDATE api_calls SET status='no_usage', calib_scale=NULL "
                "WHERE api_request_id=?",
                (api_request_id,),
            )
        return

    actual_input = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    cache_read = int(usage.get("cache_read_tokens") or 0)
    cache_write = int(usage.get("cache_write_tokens") or 0)
    output = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    reasoning = int(usage.get("reasoning_tokens") or 0)
    # Billed prompt side = input + cache buckets (normalize_usage keeps
    # input_tokens exclusive of cache reads for Anthropic-style providers).
    billed_prompt = actual_input + cache_read + cache_write

    input_buckets = {k: v for k, v in raw_buckets.items() if k not in ("output", "reasoning")}
    calibrated, scale = calibrate(input_buckets, billed_prompt or None)
    calibrated["output"] = float(output)
    if reasoning:
        calibrated["reasoning"] = float(reasoning)

    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO api_calls (api_request_id, ts, buckets_json, status,"
            " actual_input, actual_output, actual_cache_read, actual_cache_write,"
            " actual_reasoning, calib_scale) VALUES (?, ?, ?, 'complete', ?, ?, ?, ?, ?, ?)",
            (api_request_id, time.time(), json.dumps(calibrated), actual_input,
             output, cache_read, cache_write, reasoning, scale),
        )
        return

    conn.execute(
        """
        UPDATE api_calls SET
            actual_input=?, actual_output=?, actual_cache_read=?,
            actual_cache_write=?, actual_reasoning=?, calib_scale=?,
            buckets_json=?, status='complete'
        WHERE api_request_id=?
        """,
        (actual_input, output, cache_read, cache_write, reasoning, scale,
         json.dumps(calibrated), api_request_id),
    )


# ---------------------------------------------------------------------------
# Rollups
# ---------------------------------------------------------------------------

ANALYZER_VERSION = 1


def rollup_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    session_totals: Optional[Dict[str, int]] = None,
    provenance: str = "recorder",
) -> Optional[Dict[str, Any]]:
    """Aggregate a session's complete api_calls into one rollup row.

    Idempotent: keyed by session_id; re-analysis happens when
    analyzer_version or rules_version advances (the idempotency key in the
    WHERE clause). ``session_totals`` (from core SessionDB, when available)
    drives the ±2% exact/estimated reconciliation (plan §Backfill).
    """
    rules_version, _rules = load_rules(conn)
    existing = conn.execute(
        "SELECT analyzer_version, rules_version FROM session_rollups WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if existing and existing["analyzer_version"] == ANALYZER_VERSION \
            and existing["rules_version"] == rules_version:
        return None  # already analyzed under current versions

    rows = conn.execute(
        "SELECT * FROM api_calls WHERE session_id=? AND status IN ('complete','no_usage')",
        (session_id,),
    ).fetchall()
    if not rows:
        return None

    buckets: Dict[str, float] = {}
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
              "reasoning": 0, "billed": 0}
    n_no_usage = 0
    turns = set()
    started = None
    ended = None
    for r in rows:
        if r["status"] == "no_usage":
            n_no_usage += 1
        try:
            for k, v in (json.loads(r["buckets_json"]) or {}).items():
                buckets[k] = buckets.get(k, 0.0) + float(v)
        except Exception:
            pass
        totals["input"] += r["actual_input"]
        totals["output"] += r["actual_output"]
        totals["cache_read"] += r["actual_cache_read"]
        totals["cache_write"] += r["actual_cache_write"]
        totals["reasoning"] += r["actual_reasoning"]
        if r["turn_id"]:
            turns.add(r["turn_id"])
        started = r["ts"] if started is None else min(started, r["ts"])
        ended = r["ts"] if ended is None else max(ended, r["ts"])
    totals["billed"] = (totals["input"] + totals["cache_read"]
                        + totals["cache_write"] + totals["output"])

    # Precision: exact only if recorder-observed AND no no_usage rows AND
    # (when core totals are known) recorded billed reconciles within ±2%.
    precision = "exact"
    if provenance != "recorder" or n_no_usage:
        precision = "estimated"
    elif session_totals:
        core_total = int(session_totals.get("total_tokens") or 0)
        if core_total > 0:
            drift = abs(totals["billed"] - core_total) / core_total
            if drift > 0.02:
                precision = "estimated"

    payload = {
        "session_id": session_id,
        "analyzed_at": time.time(),
        "analyzer_version": ANALYZER_VERSION,
        "rules_version": rules_version,
        "precision": precision,
        "provenance": provenance,
        "totals_json": json.dumps(totals),
        "buckets_json": json.dumps(buckets),
        "api_calls": len(rows),
        "turns": len(turns),
        "started_ts": started,
        "ended_ts": ended,
    }
    conn.execute(
        """
        INSERT INTO session_rollups (session_id, analyzed_at, analyzer_version,
            rules_version, precision, provenance, totals_json, buckets_json,
            api_calls, turns, started_ts, ended_ts)
        VALUES (:session_id, :analyzed_at, :analyzer_version, :rules_version,
                :precision, :provenance, :totals_json, :buckets_json,
                :api_calls, :turns, :started_ts, :ended_ts)
        ON CONFLICT(session_id) DO UPDATE SET
            analyzed_at=excluded.analyzed_at,
            analyzer_version=excluded.analyzer_version,
            rules_version=excluded.rules_version,
            precision=excluded.precision,
            provenance=excluded.provenance,
            totals_json=excluded.totals_json,
            buckets_json=excluded.buckets_json,
            api_calls=excluded.api_calls,
            turns=excluded.turns,
            started_ts=excluded.started_ts,
            ended_ts=excluded.ended_ts
        """,
        payload,
    )
    return payload


def sweep_unanalyzed(conn: sqlite3.Connection, *, limit: int = 50) -> int:
    """Catch-up sweep: roll up sessions whose calls exist but whose rollup is
    missing or stale (crashed / never-finalized sessions, rules bumps).

    Returns the number of sessions (re)analyzed. Callers debounce (30s) and
    run on a worker thread — never inline in a GET handler (plan §API bounds).
    """
    rules_version, _ = load_rules(conn)
    rows = conn.execute(
        """
        SELECT DISTINCT c.session_id FROM api_calls c
        LEFT JOIN session_rollups r ON r.session_id = c.session_id
        WHERE c.session_id IS NOT NULL
          AND c.status IN ('complete','no_usage')
          AND (r.session_id IS NULL
               OR r.analyzer_version != ?
               OR r.rules_version != ?)
        LIMIT ?
        """,
        (ANALYZER_VERSION, rules_version, limit),
    ).fetchall()
    count = 0
    for row in rows:
        with write_txn(conn):
            if rollup_session(conn, row["session_id"]) is not None:
                count += 1
    return count


# ---------------------------------------------------------------------------
# Suggestions: fingerprint inheritance (D6/D22/D23), done/dismiss, gates
# ---------------------------------------------------------------------------

def insert_suggestion(
    conn: sqlite3.Connection,
    *,
    run_id: Optional[int],
    fingerprint: str,
    title: str,
    evidence: str,
    plan_md: str,
    category: str,
    est_savings_pct: float,
    risk: str = "low",
    kind: str = "detector",
    scores: Optional[Dict[str, Any]] = None,
) -> int:
    """Insert a suggestion, inheriting status from its fingerprint history.

    SINGLE-STATEMENT inheritance (eng delta review D22): the status lookup is
    embedded in the INSERT ... SELECT so a concurrent dismiss in the dashboard
    process can never be missed — there is no read-then-insert window. Callers
    wrap in BEGIN IMMEDIATE (write_txn).

    The prior-status lookup considers only USER-ACTIONED rows (done/dismissed,
    latest action wins). Inheriting from "the latest row" is racy: a
    generator insert that lands just before a dismiss commits leaves a newer
    'shown' row that would mask the dismissal forever (caught by the D25
    two-thread race test). set_suggestion_status additionally cascades the
    action across the fingerprint so that already-inserted shown row is swept.

    Inheritance rules (D6/D23):
      * latest actioned row 'done'      -> inherit 'done' forever
      * latest actioned row 'dismissed' -> inherit 'dismissed' UNLESS the new
        est_savings_pct >= DISMISS_RESURRECT_FACTOR x the dismissed row's
        savings (then 'shown' with the evidence-changed note appended)
      * no actioned row -> 'shown'
    """
    cur = conn.execute(
        """
        INSERT INTO suggestions (run_id, fingerprint, title, evidence, plan_md,
            category, est_savings_pct, risk, kind, scores_json, created_at,
            status, status_changed_at)
        SELECT :run_id, :fp, :title,
               CASE
                 WHEN prior.status = 'dismissed'
                      AND :savings >= :factor * COALESCE(prior.est_savings_pct, 0)
                      AND COALESCE(prior.est_savings_pct, 0) > 0
                 THEN :evidence || char(10) ||
                      'Previously dismissed — evidence changed: −' ||
                      CAST(ROUND(prior.est_savings_pct, 1) AS TEXT) || '% → −' ||
                      CAST(ROUND(:savings, 1) AS TEXT) || '%'
                 ELSE :evidence
               END,
               :plan_md, :category, :savings, :risk, :kind, :scores, :now,
               CASE
                 WHEN prior.status = 'done' THEN 'done'
                 WHEN prior.status = 'dismissed'
                      AND NOT (:savings >= :factor * COALESCE(prior.est_savings_pct, 0)
                               AND COALESCE(prior.est_savings_pct, 0) > 0)
                 THEN 'dismissed'
                 ELSE 'shown'
               END,
               CASE
                 WHEN prior.status IN ('done','dismissed') THEN prior.status_changed_at
                 ELSE NULL
               END
        FROM (
            SELECT status, est_savings_pct, status_changed_at
            FROM (
                SELECT status, est_savings_pct, status_changed_at
                FROM suggestions
                WHERE fingerprint = :fp AND status IN ('done','dismissed')
                UNION ALL
                SELECT NULL, NULL, NULL
            )
            ORDER BY status_changed_at DESC NULLS LAST  -- latest action; sentinel last
            LIMIT 1
        ) AS prior
        """,
        {
            "run_id": run_id, "fp": fingerprint, "title": title,
            "evidence": evidence, "plan_md": plan_md, "category": category,
            "savings": float(est_savings_pct), "risk": risk, "kind": kind,
            "scores": json.dumps(scores or {}), "now": time.time(),
            "factor": DISMISS_RESURRECT_FACTOR,
        },
    )
    return int(cur.lastrowid)


def set_suggestion_status(
    conn: sqlite3.Connection, suggestion_id: int, status: str
) -> bool:
    """Set dismissed/done. Idempotent: re-setting the same status succeeds
    without touching status_changed_at (D25 idempotency test).

    Cascades across the fingerprint: dismiss/done also retires any OTHER
    currently-visible (shown/hidden) row sharing the fingerprint — closing
    the race where a generator insert landed just before this action and
    would otherwise stay visible (D22/D25)."""
    if status not in ("dismissed", "done", "shown"):
        raise ValueError(f"bad status {status!r}")
    row = conn.execute(
        "SELECT status, fingerprint FROM suggestions WHERE id=?", (suggestion_id,)
    ).fetchone()
    if row is None:
        return False
    now = time.time()
    if row["status"] != status:
        conn.execute(
            "UPDATE suggestions SET status=?, status_changed_at=? WHERE id=?",
            (status, now, suggestion_id),
        )
    if status in ("dismissed", "done"):
        conn.execute(
            "UPDATE suggestions SET status=?, status_changed_at=? "
            "WHERE fingerprint=? AND id!=? AND status IN ('shown','hidden')",
            (status, now, row["fingerprint"], suggestion_id),
        )
    return True


def recorder_observed_session_count(conn: sqlite3.Connection) -> int:
    """Sessions that count toward gates: recorder provenance only —
    backfilled sessions never satisfy gates (plan P4/P5 provenance rule)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM session_rollups WHERE provenance='recorder'"
    ).fetchone()
    return int(row[0])


def gate_check(
    conn: sqlite3.Connection,
    *,
    kind: str,
    min_sessions: int,
    refresh_every: int = 0,
) -> Tuple[bool, str]:
    """Evaluate the suggestion gates for a run of ``kind``.

    Detector runs (D9): gate only on detector_min_sessions.
    LLM runs: min_sessions AND refresh_every new sessions since the last
    LLM run's watermark.
    Returns (ok, reason-if-not).
    """
    observed = recorder_observed_session_count(conn)
    if observed < min_sessions:
        return False, f"needs {min_sessions} recorded sessions — {observed}/{min_sessions} so far"
    if kind == "llm" and refresh_every:
        row = conn.execute(
            "SELECT watermark FROM suggestion_runs WHERE kind='llm' "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row is not None and row["watermark"] is not None:
            last = int(row["watermark"])
            if observed - last < refresh_every:
                return False, (
                    f"refreshes every {refresh_every} new sessions — "
                    f"{observed - last}/{refresh_every} since last run"
                )
    return True, ""


def begin_suggestion_run(
    conn: sqlite3.Connection, *, kind: str
) -> Optional[int]:
    """Create the run row with the watermark (recorder session count).

    UNIQUE(kind, watermark) makes a double-claimed refresh unable to
    double-insert a run (plan §refresh_requests). Returns run id or None if
    the watermark already has a run of this kind.
    """
    watermark = str(recorder_observed_session_count(conn))
    try:
        cur = conn.execute(
            "INSERT INTO suggestion_runs (ts, kind, sessions_in_scope, watermark) "
            "VALUES (?, ?, ?, ?)",
            (time.time(), kind, int(watermark), watermark),
        )
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def claim_refresh_request(conn: sqlite3.Connection, request_id: int) -> bool:
    """Atomic claim: pending -> running, exactly one winner (rowcount=1)."""
    cur = conn.execute(
        "UPDATE refresh_requests SET status='running', started_at=? "
        "WHERE id=? AND status='pending'",
        (time.time(), request_id),
    )
    return cur.rowcount == 1


def reclaim_stuck_refreshes(conn: sqlite3.Connection, *, ttl_seconds: int = 1800) -> int:
    """Rows stuck 'running' past the TTL (process died mid-refresh) go back
    to 'pending' so the next drain picks them up."""
    cur = conn.execute(
        "UPDATE refresh_requests SET status='pending', started_at=NULL "
        "WHERE status='running' AND started_at < ?",
        (time.time() - ttl_seconds,),
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Acted-on observed delta (design review D10, formula pinned in D24)
# ---------------------------------------------------------------------------

OBSERVED_MIN_SESSIONS = 5


def observed_delta(
    conn: sqlite3.Connection, *, category: str, done_at: float
) -> Dict[str, Any]:
    """Per-session category average: trailing 7d before done vs all after.

    Returns {"state": "measuring", "post_sessions": n} until
    OBSERVED_MIN_SESSIONS post-done recorder sessions exist, or
    {"state": "measured", "before_avg": x, "after_avg": y, "pct": p}.
    Zero before-window sessions (done right after install) → state
    "no_baseline" — never a divide-by-zero (D25 edge).
    """
    week = 7 * 86400.0

    def _avg(where: str, params: tuple) -> Tuple[float, int]:
        rows = conn.execute(
            f"SELECT buckets_json FROM session_rollups "
            f"WHERE provenance='recorder' AND {where}", params
        ).fetchall()
        total = 0.0
        for r in rows:
            try:
                buckets = json.loads(r["buckets_json"]) or {}
            except Exception:
                continue
            # category match: exact id or parent prefix (tool_schemas.mcp
            # matches tool_schemas.mcp.<server> children)
            for k, v in buckets.items():
                if k == category or k.startswith(category + "."):
                    total += float(v)
        return (total / len(rows) if rows else 0.0), len(rows)

    before_avg, before_n = _avg(
        "ended_ts >= ? AND ended_ts < ?", (done_at - week, done_at)
    )
    after_avg, after_n = _avg("ended_ts >= ?", (done_at,))

    if after_n < OBSERVED_MIN_SESSIONS:
        return {"state": "measuring", "post_sessions": after_n,
                "needed": OBSERVED_MIN_SESSIONS}
    if before_n == 0 or before_avg <= 0:
        return {"state": "no_baseline", "after_avg": after_avg}
    pct = (after_avg - before_avg) / before_avg * 100.0
    return {
        "state": "measured",
        "before_avg": before_avg,
        "after_avg": after_avg,
        "pct": pct,
        "abs_per_session": after_avg - before_avg,
    }


# ---------------------------------------------------------------------------
# Core SessionDB access (read-only) + backfill
# ---------------------------------------------------------------------------

def core_state_db_path() -> Path:
    home = os.environ.get("HERMES_HOME", "") or os.path.expanduser("~/.hermes")
    return Path(home) / "state.db"


def open_core_db_readonly() -> Optional[sqlite3.Connection]:
    """Open hermes' state.db strictly read-only (URI mode=ro). Never takes a
    write lock on core's DB; returns None when it doesn't exist."""
    path = core_state_db_path()
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def read_core_session_totals(session_id: str) -> Optional[Dict[str, int]]:
    """Session-level token accumulators from core, for ±2% reconciliation.

    billed total = input + cache_read + cache_write + output (matches the
    rollup's billed definition and core Analytics' counting — D16)."""
    try:
        conn = open_core_db_readonly()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT input_tokens, output_tokens, cache_read_tokens,"
                " cache_write_tokens, reasoning_tokens FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        total = (int(row["input_tokens"] or 0) + int(row["cache_read_tokens"] or 0)
                 + int(row["cache_write_tokens"] or 0) + int(row["output_tokens"] or 0))
        return {"total_tokens": total} if total > 0 else None
    except Exception:
        return None


BACKFILL_CHUNK = 10


def backfill(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
    core_db: Optional[sqlite3.Connection] = None,
    progress_cb=None,
) -> Dict[str, Any]:
    """Walk core SessionDB sessions in the window and write estimated rollups.

    Resumable: sessions already rolled up are skipped, and commits land in
    chunks of BACKFILL_CHUNK so a crash resumes instead of restarting (plan
    §Dashboard-side execution bounds). Backfill provides trend context, not
    attribution-grade evidence — provenance='backfill' never satisfies gates.
    Sessions older than the message-retention window degrade to totals-only
    rows (whole-session tokens land in 'unattributed' minus what roles claim).
    """
    src = core_db or open_core_db_readonly()
    if src is None:
        return {"status": "done", "sessions": 0, "skipped": 0,
                "note": "no core state.db"}
    owns_src = core_db is None
    cutoff = time.time() - days * 86400.0
    done = 0
    skipped = 0
    try:
        sessions = src.execute(
            "SELECT id, started_at, ended_at, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, reasoning_tokens,"
            " message_count, tool_call_count, api_call_count"
            " FROM sessions WHERE started_at >= ? ORDER BY started_at",
            (cutoff,),
        ).fetchall()
        _rules_version, rules = load_rules(conn)
        chunk = 0
        for sess in sessions:
            sid = sess["id"]
            if conn.execute(
                "SELECT 1 FROM session_rollups WHERE session_id=?", (sid,)
            ).fetchone():
                skipped += 1
                continue
            billed = (int(sess["input_tokens"] or 0)
                      + int(sess["cache_read_tokens"] or 0)
                      + int(sess["cache_write_tokens"] or 0)
                      + int(sess["output_tokens"] or 0))
            if billed <= 0:
                skipped += 1
                continue
            msgs = src.execute(
                "SELECT role, content, token_count FROM messages "
                "WHERE session_id=? AND active=1 ORDER BY id",
                (sid,),
            ).fetchall()
            est: Dict[str, int] = {}
            for m in msgs:
                role = m["role"] or ""
                tok = int(m["token_count"] or 0) or estimate_tokens(m["content"] or "")
                if role == "system":
                    for cat, t in decompose_system_prompt(m["content"] or "", rules).items():
                        est[cat] = est.get(cat, 0) + t
                elif role == "user":
                    est["history.user"] = est.get("history.user", 0) + tok
                elif role == "assistant":
                    est["output"] = est.get("output", 0) + tok
                elif role == "tool":
                    est["tool_results"] = est.get("tool_results", 0) + tok
                else:
                    est["unattributed"] = est.get("unattributed", 0) + tok
            prompt_billed = (int(sess["input_tokens"] or 0)
                             + int(sess["cache_read_tokens"] or 0)
                             + int(sess["cache_write_tokens"] or 0))
            input_est = {k: v for k, v in est.items() if k != "output"}
            calibrated, _scale = calibrate(input_est, prompt_billed or None)
            if not input_est and prompt_billed:
                # totals-only session (messages pruned): honest unattributed
                calibrated = {"unattributed": float(prompt_billed)}
            calibrated["output"] = float(sess["output_tokens"] or 0)
            if sess["reasoning_tokens"]:
                calibrated["reasoning"] = float(sess["reasoning_tokens"])
            totals = {
                "input": int(sess["input_tokens"] or 0),
                "output": int(sess["output_tokens"] or 0),
                "cache_read": int(sess["cache_read_tokens"] or 0),
                "cache_write": int(sess["cache_write_tokens"] or 0),
                "reasoning": int(sess["reasoning_tokens"] or 0),
                "billed": billed,
            }
            with write_txn(conn):
                conn.execute(
                    """
                    INSERT INTO session_rollups (session_id, analyzed_at,
                        analyzer_version, rules_version, precision, provenance,
                        totals_json, buckets_json, api_calls, turns,
                        started_ts, ended_ts)
                    VALUES (?, ?, ?, ?, 'estimated', 'backfill', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO NOTHING
                    """,
                    (sid, time.time(), ANALYZER_VERSION, _rules_version,
                     json.dumps(totals), json.dumps(calibrated),
                     int(sess["api_call_count"] or 0),
                     int(sess["message_count"] or 0),
                     sess["started_at"], sess["ended_at"]),
                )
            done += 1
            chunk += 1
            if progress_cb and chunk >= BACKFILL_CHUNK:
                chunk = 0
                progress_cb(done, len(sessions))
        if progress_cb:
            progress_cb(done, len(sessions))
        return {"status": "done", "sessions": done, "skipped": skipped,
                "window_days": days}
    finally:
        if owns_src:
            try:
                src.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def prune_api_calls(conn: sqlite3.Connection, *, retention_days: int = 90) -> int:
    """Prune old api_calls rows (rollups are kept indefinitely)."""
    cur = conn.execute(
        "DELETE FROM api_calls WHERE ts < ?",
        (time.time() - retention_days * 86400.0,),
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Aggregation helpers shared by the dashboard API
# ---------------------------------------------------------------------------

def window_bounds(window: str, *, now: Optional[float] = None) -> Tuple[float, float]:
    now = now or time.time()
    if window == "24h":
        return now - 86400.0, now
    if window == "7d":
        return now - 7 * 86400.0, now
    return 0.0, now  # "session" handled by callers via latest session


def aggregate_buckets(rows: List[sqlite3.Row]) -> Tuple[Dict[str, float], Dict[str, float], int]:
    """Sum rollup rows -> (buckets, totals, estimated_share_pct)."""
    buckets: Dict[str, float] = {}
    totals = {"billed": 0.0, "input": 0.0, "output": 0.0,
              "cache_read": 0.0, "cache_write": 0.0, "reasoning": 0.0}
    est_tokens = 0.0
    for r in rows:
        try:
            b = json.loads(r["buckets_json"]) or {}
            t = json.loads(r["totals_json"]) or {}
        except Exception:
            continue
        for k, v in b.items():
            buckets[k] = buckets.get(k, 0.0) + float(v)
        for k in totals:
            totals[k] += float(t.get(k) or 0)
        if r["precision"] == "estimated":
            est_tokens += float(t.get("billed") or 0)
    est_share = int(round(est_tokens / totals["billed"] * 100)) if totals["billed"] else 0
    return buckets, totals, est_share


_LOCK = threading.Lock()
