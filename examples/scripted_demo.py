#!/usr/bin/env python3
"""
See the team TUI in seconds — no model, no API key, no desktop app.

The agents below are scripted, but everything else is real: the message
protocol, transcripts, consensus, verification and the live view.

    python examples/scripted_demo.py               # live TUI (colors + spinner)
    python examples/scripted_demo.py --ui plain    # clean lines for CI/logs
    python examples/scripted_demo.py --rounds 2

Afterwards replay what it wrote:

    codex-gravity-tui -w .                     # latest session under ./.codex-gravity
    codex-gravity-tui -w . --session <id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from codex_gravity.event_bus import EventBus
    from codex_gravity.team.agents import AgentReply, AgentTurn, TeamAgent
    from codex_gravity.team.cli import _print_detail
    from codex_gravity.team.session import TeamConfig, TeamOrchestrator
    from codex_gravity.tui import make_ui
except ModuleNotFoundError as error:  # usually: venv not activated
    print(
        f"Missing dependency '{error.name}'. Run this script with the project venv:\n"
        "  .venv/bin/python examples/scripted_demo.py\n\n"
        "or activate it first:\n"
        "  bash/zsh:  source .venv/bin/activate\n"
        "  fish:      source .venv/bin/activate.fish\n\n"
        "fresh checkout:  pip install -e \".[dev]\"",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

TASK = "Add retry handling to the HTTP client"


def approve(summary: str) -> str:
    return "```json\n" + json.dumps(
        {"verdict": "approve", "summary": summary, "blocking_issues": [], "questions": []}
    ) + "\n```"


def revise(issue: str, questions: list[str]) -> str:
    return "```json\n" + json.dumps(
        {"verdict": "revise", "summary": "Needs another pass.",
         "blocking_issues": [issue], "questions": questions}
    ) + "\n```"


class ScriptedAgent(TeamAgent):
    """Plays back a fixed script so the UI can be demonstrated offline."""

    def __init__(self, name: str, replies: list[str], *, delay: float = 0.7) -> None:
        super().__init__(timeout=30)
        self.name = name
        self._replies = list(replies)
        self.delay = delay

    def availability(self) -> tuple[bool, str]:
        return True, f"scripted demo agent ({len(self._replies)} replies queued)"

    async def _ask(self, turn: AgentTurn, timeout: float) -> AgentReply:
        await asyncio.sleep(self.delay)  # let the spinner be visible
        text = self._replies.pop(0) if self._replies else approve("Fine.")
        return AgentReply(ok=True, text=text, engine="scripted-demo")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Offline TUI demo for Codex-Gravity")
    parser.add_argument("--workspace", default=".", help="Where the session is written (default: .)")
    parser.add_argument("--ui", choices=["auto", "gui", "curses", "ansi", "plain"], default="auto")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--fast", action="store_true", help="No artificial delay")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    bus = EventBus()
    ui = make_ui(args.ui)

    agents: dict[str, TeamAgent] = {
        "codex": ScriptedAgent("codex", [
            "# Plan v1\n- wrap requests in a retry helper\n- no budget test yet",
            revise("No retry-budget test.", ["Which backoff strategy?"]),
            approve("Budget test added, backoff documented."),
        ], delay=0 if args.fast else 0.7),
        "antigravity": ScriptedAgent("antigravity", [
            "Exponential with full jitter, capped at 5 attempts.",
            "# Plan v2\n- retry helper with exponential + jitter\n- retries only 429/5xx/timeouts\n- test: budget exhausted after 5 attempts",
        ], delay=0 if args.fast else 0.9),
        "codex-cli": ScriptedAgent("codex-cli", [approve("Reviewed on the CLI transport.")],
                                   delay=0 if args.fast else 0.5),
        "verifier": ScriptedAgent("verifier", [], delay=0),
    }

    ui.header(task=TASK, workspace=str(workspace), session=None, agents=agents)
    bus.subscribe("*", ui.on_event)
    ui.start_animation()

    config = TeamConfig(
        task=TASK,
        workspace=str(workspace),
        plan_with="codex",
        implement_with="antigravity",
        review_with=["codex"],
        max_rounds=args.rounds,
        test_command=f'"{sys.executable}" -c "print(\'suite ok\')"',
    )
    try:
        result = await TeamOrchestrator(agents=agents, event_bus=bus).run(config, agents=agents)
    except Exception:
        ui.stop_animation()
        raise

    ui.summary(result)
    await ui.wait_close()   # full-screen dashboard stays up until q/Esc
    ui.stop_animation()
    _print_detail(result)
    print("\nReplay this session with:")
    print(f"  codex-gravity-tui -w {workspace} --session {result.session_id}")
    return 0 if result.status in ("consensus", "verified") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
