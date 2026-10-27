# Two-agent review in CI

`codex-gravity-review` gates a diff with two independent reviewers and exits non-zero
when the change is blocked. It can run locally or as a GitHub Action.

## Local

```bash
codex-gravity-review --base origin/main --auto-test
codex-gravity-review --staged --review-with antigravity,antigravity-pro
codex-gravity-review --base HEAD~1 --test-cmd "pytest -q" --json
```

| Exit | Meaning |
| --- | --- |
| `0` | approved (and the verification command, if any, passed) |
| `1` | changes requested, or the change was approved but verification failed |
| `2` | no reviewer was reachable, empty diff, or usage error |

`--auto-test` picks a real default: `python -m py_compile` on the Python files in the diff.

Artefacts per run: `.codex-gravity/team/<session>/transcript.jsonl` and `review.md`.

## GitHub Action

`action.yml` in the repository root makes this available as
`uses: uahadov/codexgravity@v1`:

```yaml
name: team-review
on: [pull_request]
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: uahadov/codexgravity@v1
        with:
          base: origin/${{ github.base_ref }}
          gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
          reviewers: antigravity,antigravity-pro
          test-command: pytest -q
```

It posts **one sticky comment** per pull request (updated on every push) with the two
verdicts, blocking issues, dissent and the verification result. Inputs:

| Input | Default | Notes |
| --- | --- | --- |
| `base` | — | Revision to diff against, e.g. `origin/main` |
| `reviewers` | `antigravity,antigravity-pro` | Two different Gemini models by default |
| `test-command` | — | Real verification command (empty = skip) |
| `auto-test` | `true` | Falls back to `py_compile` on changed Python files |
| `fail-on-blockers` | `true` | Set `false` for advisory-only reviews |
| `gemini-api-key` | — | Required unless a local `agentapi` binary exists |
| `title`, `max-diff` | see `action.yml` | Cosmetic / context budget |

Outputs: `status` (`approved|changes_requested|failed|unavailable`) and `session-id`.

### Credentials

- **`GEMINI_API_KEY`** (or `GOOGLE_API_KEY`): the `antigravity` and `antigravity-pro`
  transports use the Google GenAI SDK when no local `agentapi` binary is present — which
  is the normal case on a runner.
- **Codex CLI on a runner** is optional. Install it and authenticate if you want
  `codex-cli` as a reviewer; otherwise keep the default Gemini pair.
- The action only needs `pull-requests: write` to post the comment.

### Cost and limits

- Two reviewers = two model calls per run, each capped by `max-diff` (60k chars default).
- The diff is the only code sent; private repository content never leaves the runner
  except to the model provider you configured.
- An unreachable provider is reported (`status: unavailable`, exit `2`) — the action never
  invents an approval.

## Dogfooding this repository

`.github/workflows/team-review.yml` runs the action on its own pull requests. Enable it by
setting the repository variable `CODEX_GRAVITY_REVIEW=true` and the secret
`GEMINI_API_KEY`; optionally set `CODEX_GRAVITY_TEST_CMD` (e.g. `pytest -q`).
