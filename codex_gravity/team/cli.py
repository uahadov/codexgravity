"""
``codex-gravity-team``: run a two-way Codex + Antigravity team session.

Examples
--------
    codex-gravity-team "Add rate limiting to the auth service" --workspace .
    codex-gravity-team "Fix the flaky retry test" -w . --test-cmd "pytest -q -k retry"
    codex-gravity-team "Design the migration" -w . --plan-with codex \
        --implement-with antigravity --review-with codex,antigravity --judge codex
    codex-gravity-team --check          # show which transports are available
    codex-gravity-team --init           # write .codex-gravity/team.json (the team's policy)
    codex-gravity-team "..." --dry-run  # print the resolved plan without calling a model
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
from codex_gravity.team.config import load_policy, policy_summary, write_policy
from codex_gravity.team.memory import decisions_count
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
    parser.add_argument("--plan-with", default=None, help="Agent that writes the first proposal (policy default: codex; codex-gui when --codex-thread is set)")
    parser.add_argument("--implement-with", default=None, help="Agent that answers questions and revises (policy default: antigravity)")
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
    parser.add_argument("--rounds", type=int, default=None, help="Maximum debate rounds (1-10, policy default: 3)")
    parser.add_argument("--timeout", type=float, default=None, help="Per-turn timeout in seconds (policy default: 180)")
    parser.add_argument("--test-cmd", default=None, help="Real verification command run after consensus")
    parser.add_argument("--verify-retries", type=int, default=None, help="Repair attempts after a failed verification (0-3)")
    parser.add_argument("--max-calls", type=int, default=None, help="Stop after N agent calls (policy default: unlimited)")
    parser.add_argument("--budget-minutes", type=float, default=None, help="Stop after N minutes (policy default: unlimited)")
    parser.add_argument("--no-remember", action="store_true", help="Do not read or write .codex-gravity/decisions.md")
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
    parser.add_argument("--init", action="store_true", help="Write .codex-gravity/team.json and exit")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing policy file with --init")
    parser.add_argument("--dry-run", action="store_true", help="Print the resolved plan and exit without calling a model")
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
    if result.decision_memory:
        print(f"Decisions:  {result.decision_memory}")


async def _run(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser()
    if not workspace.is_absolute():
        workspace = workspace.resolve()

    if args.init:
        overrides: dict = {}
        if args.test_cmd:
            overrides["test_command"] = args.test_cmd
        if args.rounds:
            overrides["max_rounds"] = args.rounds
        try:
            path = write_policy(workspace, force=args.force, overrides=overrides)
        except TeamError as error:
            print(f"{error}", file=sys.stderr)
            return 2
        print(f"wrote {path}\nCommit it: every run in this repository now uses the same roles, rounds and budget.")
        return 0

    try:
        policy = load_policy(workspace)
    except TeamError as error:
        print(f"Policy error: {error}", file=sys.stderr)
        return 2

    codex_thread = (
        args.codex_thread
        or os.environ.get("CODEX_GRAVITY_CODEX_THREAD_ID")
        or os.environ.get("CODEX_THREAD_ID")
        or policy["codex_thread"]
    )
    # A desktop chat id routes Codex through the app unless the policy says otherwise.
    plan_with = args.plan_with or policy["plan_with"]
    review_with = _names(args.review_with) if args.review_with else list(policy["review_with"])
    if codex_thread:
        if "codex" in review_with:
            review_with = ["codex-gui" if name == "codex" else name for name in review_with]
        if plan_with == "codex":
            plan_with = "codex-gui"

    turn_timeout = float(args.timeout if args.timeout is not None else policy["turn_timeout"])
    effective = {
        "plan_with": plan_with,
        "implement_with": args.implement_with or policy["implement_with"],
        "review_with": review_with,
        "judge_with": args.judge_with or policy["judge_with"],
        "codex_thread": codex_thread,
        "reviewer_weights": _weights(args.weight) or dict(policy["reviewer_weights"]),
        "veto_agents": _names(args.veto) or list(policy["veto_agents"]),
        "max_rounds": args.rounds if args.rounds is not None else policy["max_rounds"],
        "require_unanimous": bool(policy["require_unanimous"]) and not args.majority,
        "turn_timeout": turn_timeout,
        "test_command": args.test_cmd or policy["test_command"],
        "verification_retries": (
            args.verify_retries if args.verify_retries is not None else policy["verification_retries"]
        ),
        "max_agent_calls": args.max_calls if args.max_calls is not None else policy["max_agent_calls"],
        "budget_minutes": (
            args.budget_minutes if args.budget_minutes is not None else policy["budget_minutes"]
        ),
        "remember_decisions": bool(policy["remember_decisions"]) and not args.no_remember,
    }

    agents = default_agents(
        turn_timeout=turn_timeout,
        gui_agent=args.gui,
        codex_thread=codex_thread,
    )

    if args.check:
        _print_agents(agents)
        return 0

    if not args.task.strip():
        print("A task description is required (or pass --check / --init / --dry-run).", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"workspace  {workspace}")
        print(f"task       {args.task.strip()[:200]}")
        print(f"decisions  {decisions_count(workspace)} recorded\n")
        print(policy_summary(dict(policy, **effective)))
        print()
        _print_agents(agents)
        return 0

    config = TeamConfig(
        task=args.task,
        workspace=str(workspace),
        plan_with=effective["plan_with"],
        implement_with=effective["implement_with"],
        review_with=effective["review_with"],
        judge_with=effective["judge_with"],
        codex_thread=effective["codex_thread"],
        reviewer_weights=effective["reviewer_weights"],
        veto_agents=effective["veto_agents"],
        max_rounds=effective["max_rounds"],
        require_unanimous=effective["require_unanimous"],
        majority_threshold=args.threshold,
        turn_timeout=effective["turn_timeout"],
        human_notes=list(args.note),
        test_command=effective["test_command"],
        verification_retries=effective["verification_retries"],
        max_agent_calls=effective["max_agent_calls"],
        budget_minutes=effective["budget_minutes"],
        remember_decisions=effective["remember_decisions"],
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
