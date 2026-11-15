# AGENTS.md — working on Watcher

## Project mode: REAL

<!-- SIMPLE: local-only repo, no remote, pre-push hook installed and must stay. -->
<!-- REAL: GitHub <org>/<repo>, <public|private>, default branch main. Verify
     `gh auth status` shows nkavassalis before any push. Both test tiers green
     before any push or handoff. -->

## Purpose

<2–5 sentences: what it is, who uses it, why it exists. State the one-line spirit
if there is one — e.g. "a really good read-only frontend for a folder of media",
"fewer moving parts, not more". Every future change should be checkable against it.>

## Layout

```
<file>        <responsibility — one line each; the map, not a dump>
```

## How to run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt \
    && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env          # fill in locally; never commit
.venv/bin/python run.py       # <host:port, what you should see>
```

## How to run the tests

```bash
.venv/bin/python -m pytest              # hermetic tier (default: -m "not ui")
.venv/bin/python -m pytest -m ui        # browser tier; needs port <N> free
```
<Current state: X hermetic tests, Y browser tests, all passing on Python 3.12 /
Flask 3.x / Chromium (Playwright <pinned version>).>

## Design decisions (and why — don't undo casually)

- **<decision>** — <the reason, and the date/decision-maker if it was a user call>.
  <What breaks if it's "simplified".>

## Invariants (test-enforced unless noted)

1. <rule that must never break; name the test that enforces it, or "(not automated)">

## Model provenance (fill in if any model drives tools here)

| field | value |
|---|---|
| model id / publisher | <id, publisher, hub revision, sha256> |
| tools it is allowed | <none / read-only / bash-browser-cloud> |
| probed | <date, `model-probe.py` result, report path> |
| authored code | <which dirs/commits this model wrote; commit trailer `Assisted-by: <model>`> |

Unprobed or unknown-publisher weights get **no tools** (see `agent-egress-defense`
skill). AI-written code that ships passes the secret scan's model-origin canaries.

## Conventions

- <naming, formatting, where new code goes, patterns to follow, patterns to avoid>

## Known issues / TODOs

- <what is broken, fragile, or deliberately deferred — and why>

## Testing limitations / unverified

- <what was never actually exercised, and why. What Nick should check by hand.>

## Do-not-touch list

- <files/behaviours that need Nick's explicit OK to change, and why>

## Guardrails in effect

- <mode-specific: for SIMPLE — local-only, pre-push hook blocks remotes, no gh repo
  create; for REAL — org/account, visibility, branch, the full gate (unit + ui + lint
  + secret scan) that must be green before any push>
- Public-forever: no secrets, hostnames, IPs, home paths, topology, or live user data
  in any tracked file, ever. Scan `git diff --cached` before committing.
