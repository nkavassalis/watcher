# AGENTS.md — working on Watcher

## Project mode: REAL

GitHub `nkavassalis/watcher`, **public** (Nick asked to open-source it),
default branch `main`. Verify `gh auth status` shows `nkavassalis` before any
push. Both test tiers + ruff + the secret scan green before any push.
No auth in the app — LAN/perimeter hosting only (Nick's explicit call, see
Design decisions); the README says so loudly.

## Purpose

Single-user self-hosted "watch a website for the change I care about" tool:
you give it a URL plus a plain-English intent ("back in stock"), it uses an
LLM **once** to fit a small deterministic detection spec to the page, then
polls on a schedule (default daily) with zero LLM calls and emails you via
Gmail when the condition flips. Spirit: *the model designs the detector, the
code runs it* — everything on the hot path is deterministic, inspectable
JSON, and testable without any model.

## Layout

```
run.py                  thin wrapper; the real entrypoint is watcher/cli.py
watcher/cli.py          argparse entry; daemon launch is `python -m watcher`
watcher/config.py       DEFAULTS <- config.json <- env, deep-merged; masked_config()
watcher/db.py           ALL SQL: watches, checks, per-host backoff table
watcher/net.py          the one outbound path: cookie jar (rung 0), delayed
                        browser-header retry (rung 1), ladder orchestration
watcher/browser.py      rungs 2-4: persistent-context Chromium, consent rules,
                        gated LLM rescue (hard action allowlist)
watcher/llm.py          OpenAI-compatible chat client (urllib only); JSON extraction
watcher/evaluate.py     spec validation + deterministic evaluation (imports NO llm)
watcher/onboard.py      URL+intent -> spec, with the current_met cross-check
watcher/notify.py       Gmail send, sparky-style (stored OAuth, no SDK)
watcher/scheduler.py    in-process thread: onboarding + due checks; check_once()
watcher/web.py          Flask factory, thin routes, input hygiene, JSON-only errors
watcher/templates/      one template; watcher/static/ one JS + one CSS
tests/                  hermetic tier + `-m ui` Playwright tier
scripts/gate.sh         the pre-push gate (also run by .githooks/pre-push)
scripts/secret-scan.sh  run before every commit/push
config.json             LOCAL ONLY (gitignored): real LLM endpoint etc.
```

## How to run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt \
    && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env              # or write config.json (see README table)
.venv/bin/python -m watcher --host 127.0.0.1 --port 8177
```

Open http://127.0.0.1:8177 — empty list, add form at the bottom, settings
gear top-right. For the long-lived house instance, launch supervised (no
systemd on this box): a `nohup bash -c 'while true; do .venv/bin/python -m
watcher --host <LAN-IP> --port 8177 >> logs/watcher.log 2>&1; sleep 5; done'`
loop — the live parent loop is the proven-surviving topology on this
container; `python -m watcher` (never `python run.py`) so other projects'
`pkill -f run.py` can't hit it. Verified live 2026-11-15: listens on the LAN
bind, `/api/health` ok, graination watch onboarded + polled.

## How to run the tests

```bash
.venv/bin/python -m pytest              # hermetic tier: 97 pass, 1 skip (theme)
.venv/bin/python -m pytest -m ui        # browser tier: 6 pass; needs port 8177 free
.venv/bin/ruff check .                  # clean
```

Current state (2026-11-15): 97 hermetic + 6 browser tests, all passing on
Python 3.12.3 / Flask 3.1 / Playwright 1.63 (pinned). The ui tier boots the
real app subprocess plus a **local stub shop and local stub OpenAI endpoint**
— no third party is contacted by either tier.

## Design decisions (and why — don't undo casually)

- **No auth at all** (Nick's call 2026-11). Perimeter/LAN owns access; the
  README carries the loud warning. Do not "add a login" without asking.
- **LLM only at onboarding; zero calls on the poll path.** Token discipline
  (house invariant 11). `test_periodic_check_makes_zero_llm_calls` patches
  every llm entrypoint to raise — keep that test.
- **The cross-check** (2026-11): the onboarding model must predict
  `current_met` for the page as it is NOW; we evaluate the spec ourselves and
  a disagreement gets one feedback round, then `needs_review`. Without it a
  confidently wrong spec silently watches nothing. This caught real design
  risk in testing; do not remove it to "reduce latency".
- **Specs are literal strings only** — `present`/`absent` needles + `mode:
  hash`. No regex, no CSS selectors, no model-supplied code. The haystack is
  raw HTML + normalized visible text, so JSON-LD signals
  (`schema.org/OutOfStock`) are matchable — that's what the graination
  onboarding picked on the live run (2026-11-15, verified).
- **Edge-triggered email** (condition specs): one email per transition into
  `changed` (`last_status != 'changed'`), not per poll; hash specs email on
  every change. Otherwise a restocked page emails daily forever.
- **Challenge ≠ block**: a bot challenge is a machine problem — waiting IS
  the retry, so challenged keeps the user interval and counts as ONE attempt
  (rung-1 retry returns as-is). 403/429 without a challenge signature is a
  block and pauses the whole HOST with doubling backoff (hosts table).
- **check_once() is the single pending-vs-error decision point** (house
  invariant 12). Never fork that logic; scheduler crash-handling mirrors it.
- **`notify_email` absent = default 1; present-but-invalid = 400** — a typo'd
  boolean must not silently disable someone's email.
- **Gmail send is dep-free** (sparky's shape): refresh from
  `client_secret.json`/`tokens.json` in `gmail_dir`, cache the access token
  back to disk, POST base64url MIME. Missing mailbox ⇒ NotifyError ⇒ the
  check still records `changed` + `EMAIL FAILED` in the detail. A broken
  mailbox must never kill a poll.
- **Browser rungs are optional at runtime**: `browser.py` imports playwright
  at module top so `net.fetch` can catch ImportError and stop at rung 1.
  Per-host launch cooldown (default 600 s) so a stuck site can't fork a
  browser per second.
- **Settings UI cannot edit `llm.api_key` or `gmail_dir`** — file/env only;
  `masked_config()` masks the key. Keep new secrets out of the UI too.

## Invariants (test-enforced unless noted)

1. All SQL in `db.py`; routes thin, no SQL in `web.py`. (convention, review)
2. `/api/*` errors are JSON, never HTML 500. `test_errors_under_api_are_json`
3. `record_check()` is THE one place a check outcome is written. (review)
4. Poll path makes zero LLM calls. `test_periodic_check_makes_zero_llm_calls`
5. Rescue model may only click texts present in the code-collected snapshot;
   unknown action / garbage JSON / LLM error ⇒ give_up, never more autonomy.
   `test_browser.py` gate suite.
6. A challenge makes exactly 2 HTTP calls (initial + one delayed retry).
   `test_challenge_retries_once_then_degrades_to_browser`
7. Host backoff doubles and caps; a successful fetch clears it.
   `test_db.py`, `test_scheduler.py`
8. `evaluate.py` must never import `llm` or `browser`. (review — the token
   discipline test is the mechanical backstop)
9. One SQLite connection per thread; the scheduler opens its own. (review)
10. Frontend contract: every `$()` id in app.js exists in the template; every
    `data-act` has a handler; overlays ship hidden; innerHTML only with
    `esc()`. `test_ui_contract.py` — wire it, never weaken it.
11. Email is edge-triggered for condition specs. `test_change_triggers_once_email_on_transition`
12. Editing url/intent always resets the watch to `pending` (spec was fitted
    to the old page). `test_update_fields_and_reonboard_trigger`

## Model provenance (the onboarding/rescue LLM the APP calls)

| field | value |
|---|---|
| endpoint | self-hosted vLLM, OpenAI-compatible (URL lives in gitignored config.json — never in this repo) |
| model id / publisher | served id `qwen3.8-flash-next` (open weights) as of 2026-11-15 |
| tools it is allowed | text-in/text-out only. Rung-4 rescue: click-a-seen-text / wait / give_up via `browser.rescue`'s allowlist — no type/submit/navigate exists to ask for |
| probed | not run through `model-probe.py` (the app never gives it bash/browser-cloud tools; the rescue gate is the defense) |
| authored code | this project was written by a coding agent; treat AI-authored code under the usual secret-scan gate |

If you swap the default endpoint/model, update this table. The fetched page
is untrusted input: the gate is what the code permits, not the model's
reasoning trace (`agent-egress-defense` doctrine).

## Conventions

- New outbound requests go through `net.py` only — never a bare `requests.get`
  elsewhere; it stays the single seam tests monkeypatch.
- Keep `requests.get` patchable and fake-response-tolerant (`getattr`-guard
  `.cookies`); retry knobs stay inert until `net.configure()`.
- Timestamps are UTC ISO strings from `db.now_iso()`; they sort lexicographically.
- New routes: hygiene helpers (`_body/_str/_int/_bool/_url`) + whitelist fields;
  fuzz them in `test_api.py`.
- Frontend: delegation via `data-act` on containers, `esc()` everything,
  no new ids in rendered content.
- Commit style: imperative, terse ("watcher: cap rescue steps at 4").

## Known issues / TODOs

- **No auth** — deliberate; README warns. Perimeter owns it.
- Rungs 2-4 (browser ladder) have never fired against a real live challenge —
  see Unverified. Treat "browser rung works" as test-proven only.
- Cookie jar + browser cooldown are in-memory: a restart re-learns cookies.
  Persisting the jar to the DB is the obvious next step if challenges bite.
- No retry-path audit yet for `needs_review` (a human must click re-fit);
  acceptable for single-user, but there's no "retry onboarding nightly".
- `checks` history prunes to 500/watch; no UI pagination (fine at this scale).
- Gmail send is fire-and-forget: no retry queue if the network is down at
  trigger moment (the detail records `EMAIL FAILED`; the check itself is kept).
- Interval select is fixed options; arbitrary cron-like schedules not built.

## Testing limitations / unverified

- **Bot ladder rungs 2-4 never met a live challenge**: graination returns 200
  at rung 0 from this box (verified 2026-11-15), so persistent-context
  Chrome, consent rules, and the rescue only have fake-page coverage. If a
  site starts challenging, check `logs/watcher.log` for the rung reached
  before believing anything.
- **Live Gmail send untested**: the house instance runs with email disabled
  (recipient not yet configured); `notify.py` has faked-urlopen tests only.
  Nick: enable in config.json (`email.enabled`, `email.to`) and trigger a
  hash-mode watch to prove the mailbox path end to end.
- Live onboarding verified once (graination + the house vLLM endpoint, spec
  on schema.org availability, cross-check passed, manual check returned
  "blocked by: schema.org/OutOfStock"). Not a statistical sample.
- The ui tier ran headless on this box only; no real-device/phone check of
  the mobile shell (Playwright 390px assertions stand in).
- Rung-4 wall-clock cap (45 s) is unit-tested with patched sleep, not real
  timing.

## Do-not-touch list

- `check_once()` failure semantics (blocked/challenged/error/ok) — every
  branch has a test and a reason; changing one needs Nick's OK.
- The rescue allowlist in `browser.py` (actions, caps, snapshot-verified
  clicks) — this is the security boundary for untrusted pages.
- `evaluate.py` staying LLM-free; `net.py` staying the only outbound path.
- The rigid mobile shell CSS block (iOS scars annotated inline).
- `config.py` secret masking + the "api_key not UI-editable" rule.
- `.githooks/pre-push` and `scripts/gate.sh`.

## Guardrails in effect

- REAL: pre-push hook runs `scripts/gate.sh` (both tiers + lint) and
  `scripts/secret-scan.sh`. `gh auth status` must show `nkavassalis`; org
  confirmed as `nkavassalis`; visibility public by Nick's request.
- The repo contains **no internal IPs/hostnames/paths** — the real LLM
  endpoint and LAN bind live only in gitignored `config.json` / the
  environment. The graination URL is an approved example (Nick, 2026-11-15).
- Public-forever: no secrets, hostnames, IPs, home paths, topology, or live
  user data in any tracked file, ever. Scan `git diff --cached` before committing.
