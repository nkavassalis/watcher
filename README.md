# Watcher

Tell Watcher about a web page and describe, in plain English, the change you
care about — "back in stock", "price drops below $50", "the schedule page
changes" — and it figures out how to watch for that change, then checks the
page on a schedule and emails you when it happens.

It is a single-user self-hosted admin app: a Flask + SQLite + vanilla-JS
interface, an in-process scheduler, and one OpenAI-compatible LLM endpoint
used **only** to fit a detector to a page when you add (or re-fit) a watch.
Day-to-day polling makes zero LLM calls.

## What it does

- **Add a watch**: paste a URL, describe the change you want to hear about,
  pick a check frequency (default: once a day). The onboarding LLM pass also
  gives the watch a short friendly name ("Reflx Lab Double-X — back in
  stock"); anything you type in the edit sheet wins and is never overwritten,
  and clearing the name lets the LLM re-guess it on the next re-fit.
- **Onboarding**: Watcher fetches the page and asks the LLM for a small
  JSON detection spec — literal "must appear" / "must be absent" strings plus
  the model's own prediction of whether your condition holds *right now*.
  Watcher evaluates the spec itself and refuses to accept it unless its own
  verdict matches the model's prediction (one correction round, then the
  watch lands in *needs review* instead of silently watching the wrong thing).
- **Polling**: a scheduler thread fetches each page at its interval and
  evaluates the stored spec deterministically. No LLM, no tokens.
- **Bot checks**: sites behind Cloudflare-style challenges climb a ladder —
  per-host cookie jar → one delayed retry with browser headers → persistent
  headless Chromium → consent-rule clicks → (optionally) a tightly gated LLM
  "rescue" that may only click buttons whose text it saw in a snapshot.
  403/429 pauses the whole host with doubling backoff.
- **Notify**: when the condition flips to met, you get an email via the
  Gmail API (stored-OAuth, no SDK), plus a row in the check history. Emails
  are edge-triggered: one per transition into "changed", not one per poll.
- **Self-healing detectors**: if a site redesigns around a detector (neither
  the "met" nor the "not yet" signal appears on two consecutive real polls),
  Watcher re-fits it with the LLM and **emails you the outcome** — always,
  pass or fail. Page outages and bot walls never count toward staleness, a
  re-fit that meets one alerts you instead of fitting garbage, the old
  detector survives any failed re-fit, and a second staleness within 24h
  waits for you in `needs_review` instead of burning more LLM calls.

## Requirements

- Python 3.11+ (developed on 3.12)
- An OpenAI-compatible chat endpoint (vLLM, llama.cpp, Ollama, or a hosted
  API) reachable from the Watcher box — used at onboarding and for rare
  detector re-fits, never on routine polls
- Optional: `playwright` + Chromium for the bot-check browser rungs
  (`pip install playwright && playwright install chromium`); without it the
  ladder stops at the HTTP rungs
- Optional: Gmail OAuth files (`client_secret.json` + `tokens.json`) in
  `~/.gmail` for email notifications (the classic installed-app OAuth layout)

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env            # or write config.json — see below
.venv/bin/python -m watcher --host 127.0.0.1 --port 8177
```

Open http://127.0.0.1:8177 — empty watch list, an add form at the bottom
(URL + "what to watch for" + frequency), and a settings gear (LLM endpoint,
notify address, default frequency).

Example watch: `https://graination.ca/product/reflx-lab-double-x-bw-film-35mm-36-exposures/`
with intent `back in stock` — Watcher will fit a detector to the page's
schema.org `Offer.availability` and tell you when the film is orderable again.

## Configuration

Config is one JSON file (`config.json`, gitignored) deep-merged over
built-in defaults, with these environment variables as overrides:
`WATCHER_LLM_BASE_URL`, `WATCHER_LLM_MODEL`, `WATCHER_LLM_API_KEY`,
`WATCHER_GMAIL_DIR`, `WATCHER_CONFIG`.

| Setting | Default | Meaning |
|---|---|---|
| `llm.base_url` | `http://127.0.0.1:8000/v1` | OpenAI-compatible endpoint (onboarding only) |
| `llm.model` | `""` (first served) | model id for chat + onboarding |
| `llm.api_key` | `""` | sent as bearer if set; **file/env only, never UI-editable, and a key supplied via the environment is never written back to disk** |
| `email.enabled` | `false` | send Gmail notifications on trigger |
| `email.to` | `""` | recipient |
| `email.from_addr` | `""` | must exist in Gmail "Send as" |
| `email.gmail_dir` | `~/.gmail` | `client_secret.json` + `tokens.json` |
| `scheduler.default_interval_minutes` | `1440` | default poll frequency (daily) |
| `fetch.browser_enabled` | `true` | allow the headless-Chromium rungs of the bot ladder |

The settings UI shows `llm.api_key` masked and cannot edit it; the Gmail
credential directory is likewise file-only.

## ⚠️ Security: no auth — LAN/perimeter only

**Watcher has no authentication of any kind.** This is a known, deliberate
gap: whoever can reach the port can add watches, change the LLM endpoint, and
read your watch history. Do not expose it to the internet. Run it on a
trusted LAN, behind WireGuard/Tailscale, or behind a reverse proxy that owns
auth. The default bind is `127.0.0.1`; widening `--host` is your deliberate
act, and should stay inside a perimeter.

Also by design: the LLM is only ever asked to *describe* a page (it produces
a JSON spec, never code or selectors), and the bot-check "rescue" model can
only choose from a hard-coded allowlist of actions (click a text it verifiably
saw, wait, give up). The fetched pages are untrusted input; the gate is what
the code permits, not what the model says.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # hermetic tier (no network, no browser)
.venv/bin/python -m pytest -m ui    # Playwright + headless Chromium, port 8177 free
```

The ui tier boots the real app against a throwaway DB plus a **local stub
shop and a local stub LLM** — neither tier ever contacts a third party. The
ui tier auto-skips when Chromium isn't installed.

## Notes for contributors

See `AGENTS.md` for the architecture, invariants, and the decisions that must
not be undone casually.

## License

MIT — see `LICENSE`.
