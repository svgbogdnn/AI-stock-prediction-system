#!/usr/bin/env bash
# Full project self-check (including MCP block)

set -u

FAIL_COUNT=0
WARN_COUNT=0

pass() {
  echo "  [PASS] $1"
}

warn() {
  echo "  [WARN] $1"
  WARN_COUNT=$((WARN_COUNT + 1))
}

fail() {
  echo "  [FAIL] $1"
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

section() {
  echo
  echo "== $1 =="
}

echo "Stock Prediction System - Block Check"
echo "====================================="

section "1) Python"
if command -v python3 >/dev/null 2>&1; then
  PY_VER=$(python3 - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
)
  if python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
  then
    pass "python3 version: $PY_VER (>= 3.11)"
  else
    fail "python3 version is $PY_VER (need >= 3.11)"
  fi
else
  fail "python3 not found in PATH"
fi

section "2) .env and keys"
if [ -f ".env" ]; then
  pass ".env exists"
  grep -q '^GOOGLE_API_KEY=' .env && pass "GOOGLE_API_KEY found" || warn "GOOGLE_API_KEY missing"
  grep -q '^POLYGON_API_KEY=' .env && pass "POLYGON_API_KEY found" || warn "POLYGON_API_KEY missing"
  grep -q '^FRED_API_KEY=' .env && pass "FRED_API_KEY found" || warn "FRED_API_KEY missing (optional)"
  grep -q '^NEWS_API_KEY=' .env && pass "NEWS_API_KEY found" || warn "NEWS_API_KEY missing (optional)"
else
  fail ".env not found"
fi

section "3) Python dependencies"
PY_BIN="python3"
if [ -x "venv/bin/python" ]; then
  PY_BIN="venv/bin/python"
  pass "using venv/bin/python"
else
  warn "venv/bin/python not found, using system python3"
fi

if "$PY_BIN" - <<'PY'
import importlib
mods = [
    "fastapi", "uvicorn", "requests", "aiohttp", "pydantic",
    "google.genai", "google.adk", "pandas", "numpy", "bs4"
]
for m in mods:
    importlib.import_module(m)
print("ok")
PY
then
  pass "core modules import successfully"
else
  fail "some Python dependencies are missing"
fi

section "4) Data tools block"
for f in \
  "tools/polygon_fetcher.py" \
  "tools/fred_fetcher.py" \
  "tools/news_fetcher.py" \
  "tools/sec_edgar_fetcher.py"; do
  [ -f "$f" ] && pass "$f exists" || fail "$f missing"
done

section "5) A2A agents block"
AGENT_COUNT=$(ls -1 agents/*_server.py 2>/dev/null | wc -l | tr -d ' ')
if [ "$AGENT_COUNT" -ge 6 ]; then
  pass "agent server files found: $AGENT_COUNT"
else
  fail "expected >= 6 agent server files, found: $AGENT_COUNT"
fi

for f in \
  "agents/fundamental_analyst_server.py" \
  "agents/technical_analyst_server.py" \
  "agents/news_sentiment_analyst_server.py" \
  "agents/macro_analyst_server.py" \
  "agents/regulatory_analyst_server.py" \
  "agents/predictor_agent_server.py"; do
  [ -f "$f" ] && pass "$f exists" || fail "$f missing"
done

section "6) MCP block"
for f in \
  "mcp_context_hub/server.py" \
  "mcp_context_hub/client.py" \
  "mcp_context_hub/__init__.py"; do
  [ -f "$f" ] && pass "$f exists" || fail "$f missing"
done

if grep -q "mcp_context_hub" "agents/kaggle_orchestrator.py"; then
  pass "kaggle orchestrator references MCP"
else
  warn "kaggle orchestrator does not reference MCP"
fi

if command -v curl >/dev/null 2>&1; then
  if curl -fsS "http://localhost:8010/health" >/dev/null 2>&1; then
    pass "MCP runtime health endpoint is reachable (:8010)"
  else
    warn "MCP runtime endpoint not reachable (:8010). Start system first if needed."
  fi
else
  warn "curl not found, skipping MCP runtime health check"
fi

section "7) Backend block"
[ -f "frontend_api.py" ] && pass "frontend_api.py exists" || fail "frontend_api.py missing"
[ -f "start_full_system.py" ] && pass "start_full_system.py exists" || fail "start_full_system.py missing"

if command -v curl >/dev/null 2>&1; then
  if curl -fsS "http://localhost:8000/health" >/dev/null 2>&1; then
    pass "backend health endpoint is reachable (:8000)"
  else
    warn "backend health endpoint not reachable (:8000). Start system first if needed."
  fi
fi

section "8) Frontend block"
if [ -f "frontend/package.json" ]; then
  pass "frontend/package.json exists"
else
  fail "frontend/package.json missing"
fi

if [ -d "frontend/node_modules" ]; then
  pass "frontend/node_modules exists"
else
  warn "frontend/node_modules missing (run: cd frontend && npm install)"
fi

section "9) Notebook + docs"
[ -f "notebooks/kaggle_submission_complete.ipynb" ] && pass "kaggle notebook exists" || warn "kaggle notebook missing"
[ -s "README.md" ] && pass "README.md exists and not empty" || fail "README.md missing or empty"

echo
echo "====================================="
echo "Check complete"
echo "FAIL: $FAIL_COUNT"
echo "WARN: $WARN_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "Status: FAILED"
  exit 1
fi

echo "Status: PASSED"
exit 0
