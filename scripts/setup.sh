#!/usr/bin/env bash
# setup.sh — one-shot, idempotent environment setup for Watcher.
# Safe to re-run any time (new box, after a pull, broken venv).
#
#   scripts/setup.sh [--no-browser] [--force-browser]
#
# What it does:
#   1. creates .venv (python3 -m venv) if missing
#   2. installs requirements.txt + requirements-dev.txt
#   3. sets core.hooksPath=.githooks (the push gate; a LOCAL git setting that
#      does not survive cloning — every clone needs this line)
#   4. seeds config.json from config.example.json if absent (never overwrites)
#   5. ensures Playwright + a launchable Chromium for the -m ui tier
#      (real launch test, not a directory guess; --force-browser reinstalls)
#
# Reversibility: everything lives in .venv/, one local git config key, and a
# gitignored config.json. Undo with:  rm -rf .venv && git config --unset
# core.hooksPath && rm -f config.json — nothing outside the repo is touched.
set -euo pipefail
cd "$(dirname "$0")/.."

no_browser=0; force_browser=0
for a in "$@"; do case "$a" in
  --no-browser)   no_browser=1 ;;
  --force-browser) force_browser=1 ;;
  -h|--help)      sed -n '2,18p' "$0"; exit 0 ;;
  *) echo "unknown flag: $a (see --help)" >&2; exit 2 ;;
esac; done

PY=${PYTHON:-python3}

if [ ! -x .venv/bin/python ]; then
  echo "venv: creating with $PY"
  "$PY" -m venv .venv
else
  echo "venv: exists, reusing"
fi

echo "deps: installing (requirements + requirements-dev)"
.venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt

if command -v git >/dev/null && [ -d .git ]; then
  git config core.hooksPath .githooks
  echo "hooks: core.hooksPath=.githooks (pre-push runs the gate + secret scan)"
fi

if [ ! -f config.json ] && [ -f config.example.json ]; then
  cp config.example.json config.json
  echo "config: seeded config.json — EDIT IT (llm.base_url points at your endpoint)"
else
  echo "config: config.json left alone"
fi

if [ "$no_browser" = 1 ]; then
  echo "chromium: skipped (--no-browser); -m ui tier will skip"
elif [ "$force_browser" = 0 ] && .venv/bin/python - <<'EOF' >/dev/null 2>&1
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); b.close()
EOF
then
  echo "chromium: launchable, nothing to do"
else
  echo "chromium: installing (playwright install chromium)"
  .venv/bin/playwright install chromium
fi

echo
echo "done. verify + run:"
echo "  .venv/bin/python -m pytest          # hermetic tier"
echo "  .venv/bin/python -m pytest -m ui    # browser tier (needs port 8177 free)"
echo "  .venv/bin/python -m watcher --host 127.0.0.1 --port 8177"
