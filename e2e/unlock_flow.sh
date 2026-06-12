#!/usr/bin/env bash
# e2e: unlock flow (design plan §Testing)
#
#   drive sessions to the gate → suggestions appear gated-then-unlocked;
#   manual refresh → exactly one suggestion run (atomic claim + watermark
#   proven end-to-end).
#
# Runs against a SCRATCH HERMES_HOME-style DB by default (TL_DB override),
# never the live one. Uses the same code paths as production (core +
# detectors), seeding recorder-provenance rollups instead of burning LLM
# tokens on 10 real sessions. The live-traffic equivalent was QA'd manually
# 2026-06-12 (see HANDOFF.md §M1 EXIT QA).
#
# Usage: ./e2e/unlock_flow.sh
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HOME/.hermes/hermes-agent/venv/bin/python"
TL_DB="${TL_DB:-$(mktemp -d)/token_lens.db}"

"$PY" - "$PLUGIN_ROOT" "$TL_DB" <<'EOF'
import json, sys, threading, time
sys.path.insert(0, sys.argv[1])
import token_lens_core as core
import detectors

db_path = sys.argv[2]
conn = core.connect(db_path)
PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ✓ " if cond else "  ✗ ") + name)
    PASS, FAIL = PASS + (1 if cond else 0), FAIL + (0 if cond else 1)

def seed_session(i):
    with core.write_txn(conn):
        conn.execute(
            "INSERT OR REPLACE INTO session_rollups (session_id, analyzed_at,"
            " analyzer_version, rules_version, precision, provenance, totals_json,"
            " buckets_json, api_calls, turns, started_ts, ended_ts)"
            " VALUES (?, ?, 1, 1, 'exact', 'recorder', ?, ?, 10, 5, ?, ?)",
            (f"e2e-{i}", time.time(),
             json.dumps({"input": 90_000, "output": 10_000, "cache_read": 0,
                         "cache_write": 0, "reasoning": 0, "billed": 100_000}),
             json.dumps({"tool_schemas.mcp.ghost-server": 20_000,
                         "history.user": 60_000, "output": 10_000}),
             time.time() - 60, time.time()),
        )

# 1. gated below the detector threshold. NOTE: detectors do not self-gate —
# the caller (the recorder's drain / session-start path) checks gate_check
# first. The contract under test is the gate, not run_detectors.
seed_session(0); seed_session(1)
ok, reason = core.gate_check(conn, kind="detector", min_sessions=3)
check("gated at 2 sessions ('" + reason + "')", not ok and "2/3" in reason)

# 2. unlock at 3
seed_session(2)
ok, _ = core.gate_check(conn, kind="detector", min_sessions=3)
check("detector gate opens at 3 sessions", ok)
inserted = detectors.run_detectors(conn)
check("detector run produced findings", inserted >= 1)
row = conn.execute("SELECT * FROM suggestions WHERE fingerprint LIKE 'mcp_disable:%'").fetchone()
check("unused-MCP finding with evidence + plan",
      row is not None and "hermes mcp disable ghost-server" in row["plan_md"]
      and "0 tool results" in row["evidence"])

# 3. manual refresh: two concurrent claimers, exactly one winner
with core.write_txn(conn):
    conn.execute("INSERT INTO refresh_requests (requested_at, source) VALUES (?, 'manual')",
                 (time.time(),))
req = conn.execute("SELECT id FROM refresh_requests ORDER BY id DESC LIMIT 1").fetchone()["id"]
wins = []
barrier = threading.Barrier(2)
def claimer():
    c = core.connect(db_path)
    try:
        barrier.wait()
        with core.write_txn(c):
            if core.claim_refresh_request(c, req):
                wins.append(1)
    finally:
        c.close()
ts = [threading.Thread(target=claimer) for _ in range(2)]
[t.start() for t in ts]; [t.join() for t in ts]
check("atomic claim: exactly one winner", len(wins) == 1)

# 4. watermark blocks a duplicate run at the same session count
again = detectors.run_detectors(conn)
check("watermark blocks duplicate run at same count", again == 0)

# 5. LLM gate independent: still closed at 3 sessions
ok, reason = core.gate_check(conn, kind="llm", min_sessions=10, refresh_every=5)
check("LLM gate still closed (" + reason + ")", not ok)

print(f"== unlock_flow: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)
EOF
