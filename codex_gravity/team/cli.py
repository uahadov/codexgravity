"""
``codex-gravity-team``: run a two-way Codex + Antigravity team session.

Examples
--------
    codex-gravity-team "Add rate limiting to the auth service" --workspace .
    codex-gravity-team "Fix the flaky retry test" -w . --test-cmd "pytest -q -k retry"
    codex-gravity-team "Design the migration" -w . --plan-with codex \
        --implement-with antigravity --review-with codex,antigravity --judge codex
    codex-gravity-team --check          # show which transports are available
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from codex_gravity.event_bus import EventBus
from codex_gravity.team.agents import default_agents
from codex_gravity.team.protocol import TeamError
from codex_gravity.team.session import TeamConfig, TeamOrchestrator, TeamResult
from codex_gravity.tui import make_ui


def _names(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-gravity-team",
        description="Run a Codex + Antigravity team: plan, adversarial review, cross-examination, consensus, verification.",
    )
    parser.add_argument("task", nargs="?", default="", help="The engineering task to decide on")
    parser.add_argument("-w", "--workspace", default=".", help="Absolute (or resolvable) project directory")
    parser.add_argument("--plan-with", default=None, help="Agent that writes the first proposal (default: codex-gui when --codex-thread is set, else codex)")
    parser.add_argument("--implement-with", default="antigravity", help="Agent that answers questions and revises")
    parser.add_argument("--review-with", default=None, help="Comma-separated reviewers (default: same codex transport + antigravity)")
    parser.add_argument("--judge", dest="judge_with", default=None, help="Agent that breaks a deadlock")
    parser.add_argument(
        "--codex-thread",
        default=None,
        help=(
            "Exact existing ChatGPT/Codex desktop chat UUID. Uses the desktop app (no CLI): "
            "the team sends one message into that chat and waits for the new reply. "
            "Also read from CODEX_GRAVITY_CODEX_THREAD_ID / CODEX_THREAD_ID."
        ),
    )
    parser.add_argument("--codex-gui-poll", type=float, default=2.0, help="Seconds between desktop reply checks")
    parser.add_argument("--veto", default="", help="Comma-separated agents whose rejection blocks consensus")
    parser.add_argument("--weight", default="", help="Per-reviewer weights, e.g. codex=2,antigravity=1")
    parser.add_argument("--rounds", type=int, default=3, help="Maximum debate rounds (1-10)")
    parser.add_argument("--timeout", type=float, default=180.0, help="Per-turn timeout in seconds")
    parser.add_argument("--test-cmd", default=None, help="Real verification command run after consensus")
    parser.add_argument("--verify-retries", type=int, default=1, help="Repair attempts after a failed verification (0-3)")
    parser.add_argument("--note", action="append", default=[], help="Human note injected into the debate (repeatable)")
    parser.add_argument("--majority", action="store_true", help="Use weighted majority instead of unanimity")
    parser.add_argument("--threshold", type=float, default=0.5, help="Majority threshold when --majority is used")
    parser.add_argument("--gui", action="store_true", help="Enable the opt-in 'antigravity-gui' desktop agent")
    parser.add_argument("--session-id", default=None, help="Reuse an explicit 12-hex session id")
    parser.add_argument(
        "--ui",
        choices=["auto", "gui", "curses", "ansi", "plain"],
        default="auto",
        help=(
            "auto/gui → full-screen dashboard on a terminal (btop-style panels); "
            "ansi/plain → streaming lines (piped/CI)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON")
    parser.add_argument("--check", action="store_true", help="Only report transport availability")
    return parser


def _weights(value: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in _names(value):
        if "=" not in item:
            continue
        key, _, raw = item.partition("=")
        try:
            out[key.strip()] = float(raw)
        except ValueError:
            continue
    return out


def _print_agents(agents: dict) -> None:
    print("Transports:")
    for name, agent in sorted(agents.items()):
        ok, reason = agent.availability()
        mark = "ready" if ok else "unavailable"
        print(f"  - {name:<18} {mark:<12} {reason}")


def _print_detail(result: TeamResult) -> None:
    print("\n--- Final proposal ---\n")
    print(result.proposal or "(none)")
    print("\n--- Next actions ---")
    for item in result.next_actions:
        print(f"  - {item}")
    print(f"\nTranscript: {result.transcript_path}")
    if result.report_path:
        print(f"Report:     {result.report_path}")


async def _run(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser()
    if not workspace.is_absolute():
        workspace = workspace.resolve()
    codex_thread = (
        args.codex_thread
        or os.environ.get("CODEX_GRAVITY_CODEX_THREAD_ID")
        or os.environ.get("CODEX_THREAD_ID")
    )
    agents = default_agents(
        turn_timeout=args.timeout,
        gui_agent=args.gui,
        codex_thread=codex_thread,
    )

    if args.check:
        _print_agents(agents)
        return 0

    if not args.task.strip():
        print("A task description is required (or pass --check).", file=sys.stderr)
        return 2

    # With a desktop chat id, Codex participates through the app (no CLI).
    codex_transport = "codex-gui" if codex_thread else "codex"
    plan_with = args.plan_with or codex_transport
    review_with = _names(args.review_with) if args.review_with else [codex_transport, "antigravity"]

    config = TeamConfig(
        task=args.task,
        workspace=str(workspace),
        plan_with=plan_with,
        implement_with=args.implement_with,
        review_with=review_with,
        judge_with=args.judge_with,
        codex_thread=codex_thread,
        reviewer_weights=_weights(args.weight),
        veto_agents=_names(args.veto),
        max_rounds=args.rounds,
        require_unanimous=not args.majority,
        majority_threshold=args.threshold,
        turn_timeout=args.timeout,
        human_notes=list(args.note),
        test_command=args.test_cmd,
        verification_retries=args.verify_retries,
        session_id=args.session_id,
    )

    ui = make_ui(args.ui)
    bus = None
    if not args.json:
        ui.header(task=args.task, workspace=str(workspace), session=config.session_id, agents=agents)
        bus = EventBus()
        bus.subscribe("*", ui.on_event)
        ui.start_animation()

    orchestrator = TeamOrchestrator(event_bus=bus)
    try:
        result = await orchestrator.run(config, agents=agents)
    except TeamError as error:
        ui.stop_animation()
        print(f"Team error: {error}", file=sys.stderr)
        return 2

    if args.json:
        ui.stop_animation()
        print(result.model_dump_json(indent=2))
    else:
        ui.summary(result)
        # The full-screen dashboard stays up until q/Esc; streaming UIs return at once.
        await ui.wait_close()
        ui.stop_animation()
        _print_detail(result)
    return 0 if result.status in ("consensus", "verified") else 1


def main() -> None:
    args = _build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
