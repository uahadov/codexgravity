# Teamwork protocol

This document describes how two agents (Codex and Antigravity/Gemini) actually
collaborate: the message protocol, the consensus rules, the on-disk artefacts, the
transports, and how to plug in your own agent.

## 1. Mental model

A **session** is one task. It owns one transcript. A session runs **rounds**; each
round contains reviews, optional cross-examination, and a revision. The session ends in
exactly one status:

| Status | Meaning |
| --- | --- |
| `consensus` | Every required reviewer approved and any `test_command` passed (or none was requested). |
| `verified` | Consensus **and** the verification command exited 0. |
| `deadlocked` | The round budget ran out without approval (or a judge ruled `deadlock`). |
| `failed` | The plan was approved but verification did not pass within the repair budget. |
| `unavailable` | A required agent could not be reached; nothing was invented. |
| `cancelled` | Cancelled at a round boundary. |

There is no "probably fine" state.

## 2. Message protocol

Every message is a `TeamMessage` with: `id`, `session_id`, `round`, `kind`, `sender`,
`recipient`, `role`, optional `reply_to`, optional `correlation_id`, `metadata`, and
`created_at`.

| Kind | Sent by | Meaning |
| --- | --- | --- |
| `task` | orchestrator | The task statement, once per session. |
| `note` | human | Free-form constraint injected into every later round. |
| `proposal` | planner | The first plan. |
| `critique` | reviewer | `revise`/`reject` verdict with blocking issues. |
| `approval` | reviewer | Structured approval (never carries blockers). |
| `question` | reviewer | Cross-examination question to the implementer. |
| `answer` | implementer | Answer linked by `reply_to` + `correlation_id`. |
| `revision` | implementer | Full replacement proposal. |
| `verification` | verifier | Real command result (exit code, tails, duration). |
| `consensus` | orchestrator/judge | Final agreement, approvals, status. |
| `escalation` | orchestrator/judge | Deadlock report for a human. |
| `status` | any | Transport/agent failure notes (never a fake answer). |

`reply_to`/`correlation_id` make a Q&A pair a *thread*: `channel.thread(correlation_id)`
returns both halves, including after a process restart.

## 3. Round lifecycle

```
round 0  planner  → proposal
round N  reviewers (in configured order) → verdicts
         tally → consensus? stop
         questions (max_questions) → implementer answers → recorded as answer
         implementer → revision (blocking issues + objections + answers + notes)
round N+1 ...
         (optional) judge on deadlock
         (optional) verification + repair budget
         final consensus/escalation message, report.md, result.json
```

Prompts are role-specific (`proposal_prompt`, `review_prompt`, `answer_prompt`,
`revision_prompt`, `judge_prompt`, `verification_prompt` in
`codex_gravity/team/consensus.py`) and always include the rendered transcript slice, so
each agent sees what the others said — including dissent.

## 4. Consensus rules

`parse_review` turns a reply into a `ReviewFinding`:

1. Prefer a fenced ```` ```json ```` block; otherwise scan for a decodable JSON object.
2. Normalize the verdict (`approve|approved|lgtm|ok|…`, `revise|changes_requested|…`,
   `reject|block|…`, `abstain`).
3. **Fail closed**: no JSON, empty text, or an unknown verdict → `revise` with the raw text
   as the blocking issue and `parsed=false`.
4. **Contradiction guard**: `approve` + non-empty `blocking_issues` → demoted to `revise`.

`tally` then applies:

| Rule | Effect |
| --- | --- |
| unavailable reviewers | excluded from the denominator, listed in `decision.unavailable` |
| no available reviewer | never consensus |
| any unresolved blocking issue | never consensus |
| `require_unanimous=True` (default) | every available reviewer must approve |
| `require_unanimous=False` | `approval_weight / total_weight > majority_threshold` |
| `veto_agents` | a non-approval from a veto agent blocks regardless of weights |

Dissent (`decision.dissent`) and blockers (`decision.blockers`) are always returned, so a
"won" vote still documents the objection.

## 5. On-disk artefacts

```
<workspace>/.codex-gravity/
├── team.json          # committed policy: roles, weights, rounds, budget
├── decisions.md       # committed decision log (accepted sessions)
└── team/<session_id>/
    ├── transcript.jsonl   # append-only, mode 0600, one TeamMessage per line
    ├── report.md          # human summary + full rendered transcript
    ├── report.html        # optional: one self-contained page (codex-gravity-report)
    └── result.json        # the TeamResult object
```

Guarantees: `session_id` must match `^[a-f0-9]{12}$`; the session directory must stay
inside the workspace; `.codex-gravity` and `team/` may not be symlinks; writes use
`O_NOFOLLOW`. Corrupt lines are skipped rather than crashing a reader.

## 5b. Project policy (`team.json`)

```bash
codex-gravity-team --init -w . --test-cmd "pytest -q"   # write defaults + your test command
codex-gravity-team --init -w . --force                 # overwrite
codex-gravity-team "…" -w . --dry-run                   # resolved plan, zero model calls
```

```json
{
  "plan_with": "codex",
  "implement_with": "antigravity",
  "review_with": ["codex", "antigravity"],
  "judge_with": null,
  "reviewer_weights": {"codex": 2},
  "veto_agents": ["antigravity"],
  "max_rounds": 3,
  "require_unanimous": true,
  "test_command": "pytest -q",
  "turn_timeout": 180,
  "verification_retries": 1,
  "codex_thread": null,
  "max_agent_calls": 0,
  "budget_minutes": 0,
  "remember_decisions": true
}
```

Precedence: **flags > `team.json` > built-in defaults**. Unknown keys and out-of-range
values are rejected (`exit 2`) instead of being silently ignored, so a typo cannot quietly
disable a veto. `--dry-run` prints the effective plan plus transport reachability and
stops there.

## 5c. Budgets

`--max-calls N` and/or `--budget-minutes M` bound the run. Each agent turn is charged
before it is sent; when the budget is gone:

- the turn fails with an explicit `ok=False, error="call budget exhausted…"`;
- no further reviewer/judge/repair calls are made;
- verification is skipped (it cannot prove a verdict that was never reached);
- the result status is **`budget_exhausted`** — not `consensus`, not `deadlocked`;
- the transcript records `Stopped by budget: … (after N agent calls)`.

## 5d. Decision memory (`decisions.md`)

A session that ends `consensus` or `verified` appends a dated entry: task, session id,
status, approvals, verification command, and the first few decision-like lines of the
final proposal. The next session passes the last 8 entries to the planner, reviewers and
implementer under `PROJECT DECISIONS ALREADY MADE`, with the instruction not to re-open
them unless the task contradicts them.

Write or read failures never fail the session: they are recorded as a `status` message
and `result.decision_memory` stays `null`. `--no-remember` (or
`"remember_decisions": false`) disables both reading and writing for a run.

Long-lived projects should keep this file under review like code: it is context, and stale
entries will steer the team.

## 5e. Report and compare

```bash
codex-gravity-report -w .                       # latest session → report.html
codex-gravity-report -w . --session 9f3c1ae0b7d2 -o /tmp/audit.html
codex-gravity-compare -w .                      # two most recent sessions
codex-gravity-compare -w . 9f3c1ae0b7d2 41ab7cde9012 --json
```

The report is static HTML with inline CSS: verdict card, blocking issues, per-reviewer
findings, the real verification output and the transcript. Model output is HTML-escaped and
long bodies/messages are clipped, so attaching it is safe. It also renders diff-review
sessions (findings instead of a proposal).

`codex-gravity-compare` answers "what changed": status transition, resolved and newly
introduced blockers, approval delta, verification change and a bounded unified diff of the
proposal.

## 6. Transports

| Agent | Implementation | Failure mode |
| --- | --- | --- |
| `codex-cli` | `run_codex_architect` → installed Codex CLI, ephemeral, read-only, child MCP disabled | missing binary / non-zero exit / empty answer → `ok=False` |
| `codex` | alias of `codex-cli` | same |
| `antigravity-cli` | `agentapi new-conversation` (or `CODEX_GRAVITY_ANTIGRAVITY_CLI_CMD` template) | no binary, non-zero exit, empty output, timeout → `ok=False` |
| `codex-gui` | the ChatGPT/Codex **desktop app** through its own `codex-app-tools` catalogue (runtime tool discovery) | no chat id, no inherited pipe, busy chat, no read tool, or no observable reply → `ok=False`, nothing sent |
| `antigravity` | `speculative.call_agentapi`, else Google GenAI SDK in a worker thread | no binary/key → `ok=False` with instructions |
| `antigravity-pro` | same transports, second model (`CODEX_GRAVITY_GEMINI_PRO_MODEL`, default `gemini-2.5-pro`) | used so a CI review has two independent opinions |
| `antigravity-gui` | `AntigravityGUI.start/poll`, opt-in | wrong model, busy chat, draft, attention state → `ok=False` |

### Headless servers / CI

```bash
export CODEX_GRAVITY_CODEX_BIN=/usr/local/bin/codex
export CODEX_GRAVITY_ANTIGRAVITY_CLI_BIN=/opt/antigravity/bin/agentapi
codex-gravity-team "..." -w /srv/project \
  --plan-with codex-cli --review-with antigravity-cli,codex-cli \
  --test-cmd "pytest -q" --ui plain
```

`CODEX_GRAVITY_ANTIGRAVITY_CLI_CMD` accepts a full command template with
`{prompt}` substituted (shell-quoted), so a distribution whose CLI flags differ
can still join the team. The `-cli` transports never fall back to a cloud SDK:
if the binary is missing, the turn is `unavailable` and the reason names the
variable to set.

Every adapter is wrapped by `TeamAgent.respond`, which adds a timeout and converts any
exception into a failed turn. A failed turn is recorded as a `status` message and the
agent is marked `unavailable` for that round.

## 6b. Codex via the desktop app (no CLI)

`codex-gui` treats one desktop chat as one teammate:

```bash
# The chat UUID identifies the exact conversation (never a title, never --last).
export CODEX_GRAVITY_CODEX_THREAD_ID=01a168f9-d943-7a50-be88-2b7a7579a3c9
codex-gravity-team "Harden the upload endpoint" -w . --test-cmd "pytest -q"
# --codex-thread <uuid> does the same per run and switches the default
# plan/review transports to codex-gui.
```

A turn is:

1. read the chat (busy state, turn count, last message);
2. wait briefly for idle, then refuse if it is still busy;
3. send exactly one message (`send_message_to_thread`-style tool);
4. poll until a **new** assistant turn appears and the chat is idle again;
5. return that text, or fail with `unconfirmed`.

Guarantees and limits:

- **Tool names are discovered, not assumed.** The app advertises its catalogue; a send
  tool but no read tool means the turn is refused (a reply could not be verified).
- **No blind retries.** An unconfirmed delivery is reported once and never resent.
- **No echo confusion.** A turn whose last message is the operator's own prompt (role
  `user`) is not treated as a reply.
- **Runs inside the app.** `CODEX_APP_TOOLS_PIPE_PATH` is injected by the desktop app, so
  launch the MCP server from the app (Codex as MCP client). A bare terminal has no pipe.
- **Chat state is user state.** The team only writes into the chat you name; it never
  creates, forks or renames conversations. Use a **dedicated** chat for the teammate —
  pointing it at the chat that orchestrates the team would inject the team's messages into
  that same turn.

## 7. CLI reference

| Flag | Default | Meaning |
| --- | --- | --- |
| `-w, --workspace` | `.` | Project directory (must exist). |
| `--codex-thread` | env | Exact ChatGPT/Codex desktop chat UUID; also switches the default Codex roles to `codex-gui`. |
| `--codex-gui-poll` | `2` | Seconds between desktop reply checks. |
| `--plan-with` | `codex-gui` with `--codex-thread`, else `codex` | Agent that writes the first proposal. |
| `--implement-with` | `antigravity` | Agent that answers questions and revises. |
| `--review-with` | same Codex transport + `antigravity` | Reviewers, in order. |
| `--judge` | — | Agent that rules on a deadlock. |
| `--veto` | — | Agents whose objection blocks consensus. |
| `--weight` | — | `codex=2,antigravity=1` weights. |
| `--rounds` | `3` | Debate budget (1–10). |
| `--majority` / `--threshold` | unanimity / `0.5` | Voting model. |
| `--test-cmd` | — | Verification command (real exit code). |
| `--verify-retries` | `1` | Repair attempts after a failed verification (0–3). |
| `--note` | — | Human note injected into the debate (repeatable). |
| `--gui` | off | Enable the `antigravity-gui` transport. |
| `--session-id` | random | Explicit 12-hex id for reproducible sessions. |
| `--ui` | `auto` | `auto` (colors on a TTY, plain when piped), `ansi`, or `plain`. |
| `--timeout` | `180` | Per-turn timeout (5–1800 s). |
| `--json` | off | Print the full `TeamResult`. |
| `--check` | — | Only print transport availability, run nothing. |

Exit codes: `0` consensus/verified, `1` deadlocked/failed/unavailable/cancelled, `2`
usage or team error.

## 7b. Live dashboard (TUI)

`codex_gravity/tui_screen.py` renders a **btop-style full-screen dashboard** on a
terminal. It subscribes to the same EventBus events as the web dashboard and redraws
~10×/s; the layout is a pure function (`render(state, width, height)`) so it is unit
tested without a terminal.

```text
┌ Codex · Gravity — team session ────────────── session 9f3c1ae0b7d2 · 01:14 ┐
│  task  Add retry handling to the HTTP client                              │
├─ AGENTS ────────────────────┬─ TRANSCRIPT ───────────────────────────────┤
│ ✓ antigravity          3    │ 15:37:09  CRIT antigravity → codex-cli     │
│ ✗ codex                0    │     verdict: revise                        │
│    Codex CLI is unavailable │ 15:37:09  ASK  antigravity → codex-cli     │
│─ STATUS ────────────────────│ ── round 2 ─────────────────────────────── │
│ round 2 · msgs 11           │ 15:37:09  PASS antigravity → codex-cli     │
│ activity ▄█▆                │ 15:37:09  TEST verifier → all              │
│ last  verified              │ 15:37:09  DONE orchestrator → all          │
│─ CONSENSUS ─────────────────│                                            │
│ VERIFIED · tests PASS exit 0│                                            │
├─────────────────────────────┴────────────────────────────────────────────┤
│  q quit · ↑↓ scroll · f follow · ? help · verified ⠋   /srv/project       │
└──────────────────────────────────────────────────────────────────────────┘
```

Panels:

| Panel | Shows |
| --- | --- |
| header | task, session id, elapsed time |
| AGENTS | every transport with ✓/✗, its reason when unavailable, and how many messages it sent |
| STATUS | round, message count, per-round activity sparkline, last event |
| CONSENSUS | status, approving agents, verification exit code, rounds/messages |
| TRANSCRIPT | badge + sender → recipient + wrapped body, `── round N ──` separators |
| footer | keys, spinner, workspace path |

Keys: `q`/`Esc` quit · `↑`/`↓` (and `k`/`j`) scroll · `PgUp`/`PgDn` page · `Home`/`End`
jump · `f` follow · `r` reset · `?` help overlay.

Behaviour:

- **Automatic fallback.** Piped output (CI, `docker logs`, `tee`) uses plain lines;
  `--ui ansi` keeps colours, `--ui plain` forces text, `--ui gui` forces the dashboard.
- **Small terminals** (< 76×18) show an explicit "terminal too small" hint instead of a
  broken layout.
- **The final screen stays** after consensus until you press `q`/`Esc`, then the proposal
  and file paths are printed normally.
- **No dependencies**: curses only. If the terminal cannot start curses, the run falls
  back to plain output and says so.

Replay and follow need no running team — they read the on-disk transcript:

```bash
codex-gravity-tui --workspace /srv/project                   # latest session
codex-gravity-tui -w /srv/project --session 9f3c1ae0b7d2     # specific session
codex-gravity-tui -w /srv/project --follow --timeout 1800    # tail until DONE/ESC
```

`python examples/scripted_demo.py` runs a complete session with scripted agents so the
dashboard can be tried without a model, API key or desktop app.

## 7c. Diff review (gate mode)

`codex-gravity-review` reviews a concrete diff instead of a plan. It reuses the same
verdict contract and tally, so a change is approved only when every reviewer approved and
the optional verification command passed. Exit codes: `0` approved, `1` blocked/failed,
`2` unavailable/empty diff. CI usage (sticky PR comment) is documented in
**[CI.md](CI.md)**.

## 7d. Report, compare, replay

```bash
codex-gravity-report -w /srv/project                    # latest session → report.html
codex-gravity-report -w /srv/project --session 9f3c1ae0b7d2 -o audit.html
codex-gravity-compare -w /srv/project                   # two most recent sessions
codex-gravity-compare -w /srv/project 9f3c1ae0b7d2 41ab7cde9012 --json
```

Both read only from disk, so they work on a server that never runs a model.
`scripts/render_report_demo.py` regenerates the example report in the README.

## 8. MCP tools

```jsonc
// team_run_task
{"prompt": "Add idempotency keys", "workspace_path": "/abs/project",
 "plan_with": "codex-gui", "implement_with": "antigravity",
 "review_with": "codex-gui,antigravity", "judge_with": "codex-gui",
 "codex_thread": "01a168f9-d943-7a50-be88-2b7a7579a3c9",
 "veto_agents": "", "reviewer_weights": "codex-gui=2",
 "max_rounds": 3, "require_unanimous": true,
 "test_command": "pytest -q", "verification_retries": 1,
 "human_notes": "Keep the public API stable.", "gui_agent": false}

// codex_gui_ask - one message, wait for the new reply (no team session)
{"thread_id": "01a168f9-d943-7a50-be88-2b7a7579a3c9",
 "message": "Review this diff and list blocking issues.",
 "timeout_seconds": 300, "poll_seconds": 2}

// team_send_message - inject a constraint into a live session
{"workspace_path": "/abs/project", "session_id": "9f3c1ae0b7d2",
 "sender": "human", "recipient": "all", "kind": "note",
 "body": "Do not touch the billing module."}

// team_report / team_compare / team_decisions - artefacts from disk
{"workspace_path": "/abs/project", "session_id": "9f3c1ae0b7d2"}
{"workspace_path": "/abs/project", "session_id": "9f3c1ae0b7d2", "against": "41ab7cde9012"}
{"workspace_path": "/abs/project", "limit": 8}
```

`team_status` and `team_transcript` read from disk, so they work in a fresh process.
`team_send_message` is delivered live when the session runs in that MCP process
(it is drained at the next round) and stored for audit otherwise. `team_cancel` stops at
the next round boundary — an already in-flight agent turn is not killed.

## 9. Programmatic use

```python
import asyncio
from codex_gravity.team import TeamConfig, TeamOrchestrator

async def main():
    orchestrator = TeamOrchestrator()
    result = await orchestrator.run(TeamConfig(
        task="Add a bounded LRU cache to the fetcher",
        workspace="/abs/project",
        review_with=["codex", "antigravity"],
        test_command="pytest -q -k cache",
    ))
    print(result.status, result.decision.reason)
    print(result.transcript_path)

asyncio.run(main())
```

### Custom agent

```python
from codex_gravity.team.agents import AgentReply, AgentTurn, TeamAgent

class MyAgent(TeamAgent):
    name = "my-agent"

    def availability(self):
        return True, "local model"

    async def _ask(self, turn: AgentTurn, timeout: float) -> AgentReply:
        text = await my_model(turn.instruction, context=turn.context, timeout=timeout)
        return AgentReply(ok=True, text=text, engine="my-model")

orchestrator = TeamOrchestrator(agents={"codex": MyAgent(), "antigravity": MyAgent()})
```

Any object that follows the `TeamAgent` contract can plan, implement, review or judge.
Return `AgentReply(ok=False, error=...)` when the model cannot answer — the engine treats
that as unavailable, never as approval.

## 10. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `status: unavailable`, `proposal: ""` | planner transport down | `codex-gravity-team --check`, then sign in / install the CLI, pass `--codex-thread`, or set `GEMINI_API_KEY` |
| `codex-gui: no Codex chat id` | no destination chat | pass `--codex-thread <uuid>` or set `CODEX_GRAVITY_CODEX_THREAD_ID` |
| `codex-gui: CODEX_APP_TOOLS_PIPE_PATH is not inherited` | launched outside the desktop app | start the MCP server from ChatGPT/Codex, or export the variable |
| `codex-gui: no thread-read tool` | app version does not expose a read tool | update the app; nothing was sent |
| `codex-gui: busy` | the chat is mid-turn | wait for it to finish; the adapter never interrupts |
| `delivery was not confirmed` | acknowledgement lost | inspect the exact chat; it is never resent automatically |
| `deadlocked` with the same blockers | implementer repeats itself | raise `--rounds`, add `--judge`, or clarify the task |
| `failed` after verification | real tests failed | read `verification.stdout_tail`; the repair budget was exhausted |
| all reviews `parsed: false` | model ignored the JSON contract | keep the default prompts, or teach your agent the contract |
| `Team error: Unknown agent` | typo in `--review-with` | names are `codex`, `codex-gui`, `antigravity`, `antigravity-gui`, or your injected agents |
| `Invalid session id` | not 12 lowercase hex chars | omit `--session-id` |

## 11. Design limits

- The orchestrator is turn-based, not free-for-all chat: bounded rounds keep cost and
  latency predictable and make the transcript auditable.
- Verification runs in the **workspace**, not in a sandboxed worktree, because agents edit
  real files. The result records `isolation: "workspace (not a sandbox worktree)"`.
- Model side effects (file edits) are not rolled back automatically. Use git and review the
  diff; the transcript is evidence, not a commit.
- Token/cost numbers elsewhere in the project are estimates; the team layer intentionally
  reports latency and engines per turn instead of inventing cost.
