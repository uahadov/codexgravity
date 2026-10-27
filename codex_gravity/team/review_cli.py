"""
``codex-gravity-review``: gate a diff with two independent agents.

    codex-gravity-review --base origin/main --test-cmd "pytest -q"
    codex-gravity-review --staged --review-with codex-cli,antigravity-cli
    codex-gravity-review --base HEAD~1 --auto-test --json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

from codex_gravity.event_bus import EventBus
from codex_gravity.team.agents import TeamAgent, default_agents
from codex_gravity.team.protocol import TeamError
from codex_gravity.team.review import (
    DiffError,
    DiffReviewConfig,
    DiffReviewResult,
    DiffReviewer,
    default_test_command,
)
from codex_gravity.tui import make_ui

COLOR = {"approved": "\033[32m", "changes_requested": "\033[31m", "failed": "\033[31m", "unavailable": "\033[33m"}


def _names(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _weights(value: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in _names(value):
        key, _, raw = item.partition("=")
        if key and raw:
            try:
                out[key.strip()] = float(raw)
            except ValueError:
                continue
    return out


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-gravity-review",
        description="Review a git diff with two independent agents; exit 1 when the change is blocked.",
    )
    parser.add_argument("-w", "--workspace", default=".", help="Project directory")
    parser.add_argument("--base", default=None, help="Git range/rev to diff against, e.g. origin/main or HEAD~1")
    parser.add_argument("--staged", action="store_true", help="Review the index (git diff --cached)")
    parser.add_argument("--untracked", action="store_true", help="Include untracked files in the review")
    parser.add_argument("--title", default="Review this change", help="Short description shown to the reviewers")
    parser.add_argument("--review-with", default="codex-cli,antigravity-cli", help="Comma-separated reviewers")
    parser.add_argument("--weight", default="", help="Reviewer weights, e.g. antigravity=2")
    parser.add_argument("--veto", default="", help="Reviewers whose objection blocks the change")
    parser.add_argument("--majority", action="store_true", help="Weighted majority instead of unanimity")
    parser.add_argument("--threshold", type=float, default=0.5, help="Majority threshold")
    parser.add_argument("--test-cmd", default=None, help="Verification command run after the review")
    parser.add_argument(
        "--auto-test",
        action="store_true",
        help="Use a cheap real default: python -m py_compile on changed Python files",
    )
    parser.add_argument("--max-diff", type=int, default=60_000, help="Maximum diff characters sent to an agent")
    parser.add_argument("--timeout", type=float, default=180.0, help="Per-reviewer timeout in seconds")
    parser.add_argument("--codex-thread", default=None, help="Use the Codex desktop app (chat UUID) instead of the CLI")
    parser.add_argument("--session-id", default=None, help="Explicit 12-hex session id")
    parser.add_argument(
        "--ui",
        choices=["auto", "gui", "curses", "ansi", "plain"],
        default="auto",
        help="auto/gui → dashboard on a terminal; ansi/plain → streaming lines",
    )
    parser.add_argument("--print-diff", action="store_true", help="Print the collected diff and exit")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON")
    return parser


def _format(result: DiffReviewResult, color: bool) -> str:
    status = result.status.upper()
    if color:
        status = f"{COLOR.get(result.status, '')}{status}\033[0m"
    lines = [
        "",
        "┌ Diff review " + "─" * 54,
        f"│ status      {status}",
        f"│ reviewers   {', '.join(f.reviewer for f in result.findings) or '—'}",
        f"│ diff        {result.diff_chars} chars" + (" (truncated)" if result.diff_truncated else ""),
        f"│ decision    {result.decision.reason}",
    ]
    if result.decision.approvals:
        lines.append(f"│ approved by {', '.join(result.decision.approvals)}")
    if result.blockers:
        lines.append("│ blocking issues:")
        for item in result.blockers[:10]:
            lines.append(f"│   - {item[:110]}")
    if result.verification:
        mark = "PASS" if result.verification.passed else "FAIL"
        lines.append(f"│ verification {mark}  `{result.verification.command}` exit {result.verification.exit_code}")
    for item in result.next_actions:
        lines.append(f"│ next        {item[:100]}")
    lines += [
        f"│ session     {result.session_id}",
        f"│ transcript  {result.transcript_path}",
        f"│ report      {result.report_path}",
        "└" + "─" * 68,
    ]
    return "\n".join(lines)


async def _run(args: argparse.Namespace, agents: Optional[dict[str, TeamAgent]] = None) -> int:
    workspace = Path(args.workspace).expanduser()
    if not workspace.is_absolute():
        workspace = workspace.resolve()

    if args.print_diff:
        from codex_gravity.team.review import collect_diff

        try:
            diff, original, truncated = collect_diff(
                workspace, base=args.base, staged=args.staged,
                include_untracked=args.untracked, max_chars=args.max_diff,
            )
        except DiffError as error:
            print(f"Diff error: {error}", file=sys.stderr)
            return 2
        if not diff.strip():
            print("Diff error: Nothing to review: the selected diff is empty.", file=sys.stderr)
            return 2
        print(diff)
        if truncated:
            print(f"\n[diff truncated from {original} chars]", file=sys.stderr)
        return 0

    codex_thread = args.codex_thread
    reviewer_agents = agents or default_agents(turn_timeout=args.timeout, codex_thread=codex_thread)
    test_command = args.test_cmd or (default_test_command(workspace) if args.auto_test else None)

    config = DiffReviewConfig(
        workspace=str(workspace),
        title=args.title,
        base=args.base,
        staged=args.staged,
        include_untracked=args.untracked,
        reviewers=_names(args.review_with),
        reviewer_weights=_weights(args.weight),
        veto_agents=_names(args.veto),
        require_unanimous=not args.majority,
        majority_threshold=args.threshold,
        test_command=test_command,
        timeout=args.timeout,
        max_diff_chars=args.max_diff,
        codex_thread=codex_thread,
        session_id=args.session_id,
    )

    ui = make_ui(args.ui)
    bus = None
    if not args.json:
        ui.header(task=f"diff review — {config.title}", workspace=str(workspace),
                  session=config.session_id, agents=reviewer_agents)
        bus = EventBus()
        bus.subscribe("*", ui.on_event)
        ui.start_animation("reviewing the diff")

    try:
        result = await DiffReviewer(agents=agents, event_bus=bus).review(config)
    except DiffError as error:
        ui.stop_animation()
        print(f"Diff error: {error}", file=sys.stderr)
        return 2
    except TeamError as error:
        ui.stop_animation()
        print(f"Team error: {error}", file=sys.stderr)
        return 2

    if args.json:
        ui.stop_animation()
        print(result.model_dump_json(indent=2))
    else:
        ui.status(f"review {result.status}")
        await ui.wait_close()
        ui.stop_animation()
        print(_format(result, color=bool(getattr(ui.stream, "isatty", lambda: False)())))
    return result.exit_code


def main() -> None:
    args = _build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
