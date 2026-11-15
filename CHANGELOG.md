# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Watches have a display `name`: set it when adding (or edit it later in the
  pencil sheet); it defaults to the URL and becomes the row title, with the
  raw URL kept as a small subtitle. Additive DB migration backfills existing
  rows from their url.

### Fixed

- `pytest.ini` was never committed — the seeder's `.gitignore` `*.ini` pattern
  silently ate it, so clones ran the whole suite un-deselected with no `ui`
  marker registered. Now un-ignored and tracked.
- Hermetic `PUT /api/settings` wrote the repo's real `./config.json` — the
  file the live instance boots from — clobbering it with test values
  (2026-11-15). `create_app` now refuses implicit-config writes under
  `WATCHER_TEST_MODE` (set by conftest), fixtures pass an explicit tmp
  config path, and a tripwire test asserts `./config.json` never changes.

### Changed

- Check verdicts are now human sentences: `not yet — still shows: <needle>` /
  `not yet — waiting for: <needle>` / `yes — found: <needle>` instead of the
  cryptic `blocked by:` / `trigger condition met` (raw needles stay in the
  `matched_json` column for debugging).
- Watch rows label their parts: `WATCHING FOR: <intent>` and `Check daily ·
  last: …`; previously the intent floated unlabelled.
- New edit sheet (✎): change URL, intent, frequency, and the email toggle.
  Changing URL/intent re-fits the detector automatically; manual re-fit is an
  explicit "Re-fit detector" button inside the same sheet (the separate row
  button is gone).

## [0.1.0] — 2026-11-15

Initial release.

### Added

- Watch CRUD: URL + plain-English intent + per-watch check frequency
  (default daily) with email-notify toggle.
- LLM-assisted onboarding: one OpenAI-compatible chat call drafts a
  deterministic detection spec (`present`/`absent` literal needles or
  `hash` mode); a `current_met` cross-check with one feedback round refuses
  specs whose verdict disagrees with the model's own reading of the page.
- Deterministic polling: in-process scheduler evaluates stored specs with
  zero LLM calls; edge-triggered Gmail notifications (dep-free OAuth-token
  send, sparky-style).
- Bot-check ladder: per-host cookie jar → one delayed browser-header retry →
  persistent-context headless Chromium (optional) → consent-rule clicks →
  hard-gated LLM rescue (click-seen-text / wait / give_up only). 403/429
  pauses a host with doubling backoff; challenges keep the user interval.
- Admin web UI (no auth — LAN/perimeter only, see README warning): watch
  list with status glyphs, per-watch check history, check-now / re-fit /
  pause / resume, and a settings panel (LLM endpoint, notify address,
  default frequency) with masked, non-editable secrets.
- Test tiers: 97 hermetic tests (fake fetcher, fake LLM, fake page objects,
  fake Gmail) + 7 Playwright browser tests driving the real app against a
  local stub shop and stub LLM.
