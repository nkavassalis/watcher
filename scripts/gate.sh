#!/usr/bin/env bash
# gate.sh — the REAL-project push gate. Copy into <project>/scripts/gate.sh.
# The pre-push hook runs this; run it yourself before you hand anything back.
# Every step must pass. A step that cannot run must SKIP loudly, not silently.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
fail=0
step() { printf '\n=== %s ===\n' "$*"; }

step "unit / hermetic tier"
"$PY" -m pytest || fail=1

step "browser tier (Playwright + Chromium)"
# A SKIP is not a pass (house rule) — so the gate enforces it, not just the docs.
# Until 2026-11-10 a box without Playwright auto-skipped the whole tier and the gate,
# and therefore the pre-push hook, printed PASS. GATE_ALLOW_UI_SKIP=1 only when Nick
# agreed to ship without the browser tier; say so in the handoff.
ui_out=$("$PY" -m pytest -m ui -rs 2>&1); ui_rc=$?
printf '%s\n' "$ui_out"
if [ $ui_rc = 5 ]; then
  echo "NOTE — no ui-marked tests collected. Fine for a non-web project; for a web app this is a FAIL."
elif [ $ui_rc != 0 ]; then
  fail=1
elif printf '%s' "$ui_out" | grep -qE '[0-9]+ skipped'; then
  if [ "${GATE_ALLOW_UI_SKIP:-0}" = 1 ]; then echo "SKIPPED — allowed by GATE_ALLOW_UI_SKIP=1 (report it)"
  else echo "FAIL — browser tests SKIPPED (see reasons above). Install playwright + chromium, or GATE_ALLOW_UI_SKIP=1 if Nick agreed."; fail=1; fi
fi

step "lint"
if [ -x .venv/bin/ruff ]; then .venv/bin/ruff check . || fail=1
elif [ -f ruff.toml ] || grep -q '^\[tool.ruff' pyproject.toml 2>/dev/null; then
  ruff check . || fail=1
elif command -v ruff >/dev/null; then
  ruff check . || fail=1
else
  echo "SKIP — ruff not installed (add requirements-dev.txt to the venv)"
fi

step "syntax"
"$PY" -m compileall -q . -x '\.venv' || fail=1

step "secret scan (staged + every commit not yet on origin/main — all history before the first push)"
scripts/secret-scan.sh --staged || fail=1
if git rev-parse --verify -q origin/main >/dev/null; then scripts/secret-scan.sh --range origin/main..HEAD || fail=1
else scripts/secret-scan.sh --range HEAD || fail=1; fi

printf '\n=== gate: %s ===\n' "$([ $fail = 0 ] && echo PASS || echo FAIL)"
exit $fail
