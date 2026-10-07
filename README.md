# claude

A self-hosted agent fleet: a SQLite task queue and asyncio worker pool that runs Claude
agents in parallel, verifies each result with a second agent, pauses risky actions for
your approval, can browse the web with Playwright, and alerts you by SMS or phone call
when something goes wrong. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design,
threat model, limits and roadmap.

## Quickstart

```bash
python3.11+ -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env      # fill in ANTHROPIC_API_KEY, optionally Twilio; then export it
playwright install chromium   # or point FLEET_CHROMIUM_PATH at an existing Chromium

fleet add-task "Summarise the top story on https://example.com" --budget 0.50
fleet run --once          # or without --once to keep serving the queue
fleet status              # tasks, cost, and pending approvals
fleet approve 3           # approve request #3 (add --deny to refuse)
```

Anything that is destructive or outward-facing (writing/deleting files, clicking buttons
or submitting forms in the browser, running a Claude Code session) pauses the task and
creates an approval request. It is denied if you do not answer within
`FLEET_APPROVAL_TIMEOUT_S`. With Twilio configured you get an SMS containing the exact
`fleet approve N` command.

The default model is `claude-opus-5-5`; override with `FLEET_MODEL` (and optionally
`FLEET_REVIEW_MODEL`). Cost is estimated from `fleet/pricing.py`; update it if prices change.

## Development

```bash
pytest          # no network, Anthropic and Twilio are mocked; browser test uses local Chromium
ruff check . && ruff format --check .
```

## Status

M1 (core), the M2 browser tool and M3 alerts are implemented. Laptop and phone agents
(M4/M5) are design only. Secrets come only from environment variables.
