# Watcher

<One paragraph: what it is and who it's for. Plain prose, no marketing.>

## What it does

- <user-visible behaviour, in the order someone would discover it>

## Requirements

- Python 3.11+ (SQLite WAL, `zoneinfo`)
- <ffmpeg / external tools, if any>

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # fill in the values you actually have
.venv/bin/python run.py
```

Open <URL>. <First-run state: what's seeded, what's empty.>

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `<ENV>` | <default> | <what it does, where it's read> |

<Where config lives (file vs env), what is secret, and how secrets are masked in the UI.>

## Deployment

<How it runs where it matters: the systemd unit / nohup loop / container, the
perimeter (WireGuard/Tailscale/Caddy), and why the app itself has no auth.>

## Tests

```bash
.venv/bin/python -m pytest          # hermetic tier
.venv/bin/python -m pytest -m ui    # Playwright + headless Chromium
```

The ui tier auto-skips when Chromium isn't installed. To install it once:
`.venv/bin/pip install playwright==<pinned> && .venv/bin/playwright install chromium`.

## Notes for contributors

See `AGENTS.md` for the architecture, invariants, and the decisions that must not be
undone casually.

## License

<MIT unless Nick says otherwise.>
