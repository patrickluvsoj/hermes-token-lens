#!/usr/bin/env bash
# e2e: install flow (design plan §Testing)
#
#   clone → enable → restart dashboard + gateway → tab + card present via
#   curl + /api/dashboard/plugins; one chat session → /summary shows
#   calibrated buckets summing to billed tokens.
#
# Usage:
#   TL_TOKEN=<dashboard session token> [TL_BASE=http://127.0.0.1:9119] \
#   [TL_RUN_SESSION=1] ./e2e/install_flow.sh
#
# TL_RUN_SESSION=1 runs a real `hermes -z` one-shot (costs a few thousand
# LLM tokens) to verify the live recorder path; without it the script
# verifies the install/discovery/API legs only.
set -euo pipefail

TL_BASE="${TL_BASE:-http://127.0.0.1:9119}"
TL_TOKEN="${TL_TOKEN:?set TL_TOKEN to the dashboard session token}"
AUTH=(-H "Authorization: Bearer $TL_TOKEN")
PASS=0; FAIL=0
ok()   { echo "  ✓ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ✗ $1"; FAIL=$((FAIL+1)); }
PY="$HOME/.hermes/hermes-agent/venv/bin/python"

echo "== install_flow against $TL_BASE =="

# 1. plugin enabled
if hermes plugins list 2>/dev/null | grep -A1 "token-lens" | grep -qiE "enabled"; then
  ok "plugin enabled"
else
  # `hermes plugins list` table wraps; fall back to config check
  if "$PY" -c "
from hermes_cli.config import load_config
import sys
en = (load_config() or {}).get('plugins', {}).get('enabled', []) or []
sys.exit(0 if any('token-lens' in str(e) for e in en) else 1)
" 2>/dev/null; then ok "plugin enabled (config)"; else bad "plugin not enabled — run: hermes plugins enable token-lens"; fi
fi

# 2. dashboard discovery: tab + slot + api
DISC=$(curl -sf -m 8 "${AUTH[@]}" "$TL_BASE/api/dashboard/plugins")
echo "$DISC" | "$PY" -c "
import json,sys
plugins = json.load(sys.stdin)
tl = next((p for p in plugins if p['name']=='token-lens'), None)
assert tl, 'token-lens not discovered'
assert tl['has_api'], 'api not mounted-eligible'
assert 'sessions:top' in tl['slots'], 'sessions:top slot missing'
assert tl['tab']['path'] == '/token-lens', 'tab path wrong'
" && ok "discovered: tab /token-lens, slot sessions:top, has_api" || bad "discovery"

# 3. bundle served (NOT the SPA fallback)
SIZE=$(curl -sf -m 8 -o /dev/null -w "%{size_download}" "${AUTH[@]}" \
  "$TL_BASE/dashboard-plugins/token-lens/dist/index.js")
[ "$SIZE" -gt 10000 ] && ok "bundle served (${SIZE}B)" || bad "bundle too small (${SIZE}B — SPA fallback?)"

# 4. API mounted + authenticated
curl -sf -m 8 "${AUTH[@]}" "$TL_BASE/api/plugins/token-lens/health" >/dev/null \
  && ok "API mounted (/health 200)" || bad "API not mounted — restart the dashboard"
CODE=$(curl -s -m 8 -o /dev/null -w "%{http_code}" "$TL_BASE/api/plugins/token-lens/health")
[ "$CODE" = "401" ] && ok "API rejects unauthenticated (401)" || bad "expected 401 unauthenticated, got $CODE"

# 5. live recorder leg (optional)
if [ "${TL_RUN_SESSION:-0}" = "1" ]; then
  echo "  running one-shot session (costs LLM tokens)..."
  hermes -z "Reply with exactly: e2e-ping" >/dev/null
  curl -sf -m 8 "${AUTH[@]}" "$TL_BASE/api/plugins/token-lens/summary" >/dev/null  # schedule sweep
  sleep 35  # sweep debounce
  curl -sf -m 8 "${AUTH[@]}" "$TL_BASE/api/plugins/token-lens/summary" >/dev/null
  sleep 3
  "$PY" - <<'EOF' && ok "calibrated buckets sum to billed prompt on newest call" || bad "calibration mismatch"
import json, os, sqlite3, sys
db = sqlite3.connect(os.path.expanduser("~/.hermes/token_lens.db"))
db.row_factory = sqlite3.Row
r = db.execute("SELECT * FROM api_calls WHERE status='complete' ORDER BY ts DESC LIMIT 1").fetchone()
assert r is not None, "no complete api_calls row — recorder not loaded?"
b = json.loads(r["buckets_json"])
input_sum = sum(v for k, v in b.items() if k not in ("output", "reasoning"))
billed = r["actual_input"] + r["actual_cache_read"] + r["actual_cache_write"]
assert abs(input_sum - billed) < 1.0, f"{input_sum} != {billed}"
EOF
else
  echo "  (skipping live-session leg; set TL_RUN_SESSION=1 to include it)"
fi

echo "== install_flow: $PASS passed, $FAIL failed =="
exit $((FAIL > 0))
