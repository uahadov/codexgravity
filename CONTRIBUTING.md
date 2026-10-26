# Contributing

Thanks for helping make two-agent collaboration boring and reliable.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check codex_gravity tests
pytest -q
```

The suite is offline: no API keys, no network, no desktop app. Team tests use scripted
agents in `tests/team_fakes.py`, so a new behaviour should come with a scripted test.

## Ground rules

1. **Fail closed.** A missing transport, unparsable verdict or failed command must produce
   an explicit failure state. Never invent model output, approvals, screenshots or cost.
2. **Every claim needs evidence.** If you say "verified", there must be an exit code.
3. **The transcript is the product.** New behaviour must be visible in
   `transcript.jsonl` and `report.md`.
4. **Keep the surface small.** One well-tested tool beats three vague ones.
5. **Never commit secrets.** `.env` is git-ignored; keys stay local.

## What to work on

- New transports (local models, other MCP agents) implementing `TeamAgent`.
- Better review contracts for specific domains (security, migrations, performance).
- Resume/attach a session after a restart, without replaying side effects.
- Dashboard views over team transcripts (read-only).

## Pull requests

- One logical change per PR, with tests.
- `ruff check codex_gravity tests` and `pytest -q` must pass.
- Describe the failure mode your change prevents, not just the feature it adds.

## Reporting bugs

Include: `codex-gravity-team --check` output, the session status, and the relevant
`transcript.jsonl` lines (redact anything private).
