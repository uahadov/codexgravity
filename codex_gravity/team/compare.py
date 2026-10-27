"""
Compare two sessions: what changed after the review.

    codex-gravity-compare -w .                 # the two most recent sessions
    codex-gravity-compare -w . 9f3c 41ab       # two specific sessions
    codex-gravity-compare -w . 9f3c --json

Answers the question a transcript cannot answer at a glance: did the proposal
actually move, which blockers were resolved, and did verification change.
"""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
from typing import Any, Optional

from codex_gravity.team.report import load_result

MAX_DIFF_LINES = 160


def recent_sessions(workspace: str | Path, limit: int = 2) -> list[str]:
    base = Path(workspace).expanduser().resolve() / ".codex-gravity" / "team"
    if not base.is_dir():
        return []
    sessions = [p for p in base.iterdir() if p.is_dir() and p.name != "tmp"]
    sessions.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in sessions[:limit]]


def snapshot(workspace: str | Path, session_id: str) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve()
    result = load_result(root, session_id)
    decision = result.get("decision") or {}
    verification = result.get("verification") or {}
    findings = result.get("findings") or result.get("reviews") or []
    return {
        "session_id": session_id,
        "task": result.get("task") or result.get("title") or "",
        "status": result.get("status") or ("unknown" if not result else ""),
        "rounds": result.get("rounds", 0),
        "messages": result.get("transcript_messages", 0),
        "elapsed_seconds": result.get("elapsed_seconds", 0),
        "approvals": list(decision.get("approvals") or []),
        "blockers": list(decision.get("blockers") or []),
        "reason": decision.get("reason") or "",
        "proposal": result.get("proposal") or "",
        "findings": [
            {"reviewer": f.get("reviewer", "?"), "verdict": f.get("verdict", "")}
            for f in findings if isinstance(f, dict)
        ],
        "verification": {
            "command": verification.get("command"),
            "exit_code": verification.get("exit_code"),
            "passed": verification.get("passed"),
        } if verification else None,
    }


def _diff(before: str, after: str, limit: int = MAX_DIFF_LINES) -> dict[str, Any]:
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    lines = list(difflib.unified_diff(
        before_lines, after_lines, fromfile="before", tofile="after", lineterm="", n=2,
    ))
    return {
        "changed": bool(lines),
        "added": sum(1 for line in lines if line.startswith("+") and not line.startswith("+++")),
        "removed": sum(1 for line in lines if line.startswith("-") and not line.startswith("---")),
        "lines": lines[:limit],
        "truncated": len(lines) > limit,
    }


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    old_blockers, new_blockers = set(before["blockers"]), set(after["blockers"])
    old_approvals, new_approvals = set(before["approvals"]), set(after["approvals"])
    return {
        "before": {k: before[k] for k in ("session_id", "status", "rounds", "messages", "elapsed_seconds")},
        "after": {k: after[k] for k in ("session_id", "status", "rounds", "messages", "elapsed_seconds")},
        "task_changed": before["task"] != after["task"],
        "status_changed": before["status"] != after["status"],
        "resolved_blockers": sorted(old_blockers - new_blockers),
        "new_blockers": sorted(new_blockers - old_blockers),
        "gained_approvals": sorted(new_approvals - old_approvals),
        "lost_approvals": sorted(old_approvals - new_approvals),
        "verification": {
            "before": before["verification"],
            "after": after["verification"],
        },
        "proposal": _diff(before["proposal"], after["proposal"]),
    }


def render_text(result: dict[str, Any]) -> str:
    before, after = result["before"], result["after"]
    lines = [
        f"{'before':<8} {before['session_id']}  {before['status']}  "
        f"{before['rounds']} round(s) · {before['messages']} msg · {before['elapsed_seconds']}s",
        f"{'after':<8} {after['session_id']}  {after['status']}  "
        f"{after['rounds']} round(s) · {after['messages']} msg · {after['elapsed_seconds']}s",
        "",
    ]
    if result["task_changed"]:
        lines.append("task changed between the two sessions")
    if result["status_changed"]:
        lines.append(f"status    {before['status']} -> {after['status']}")
    else:
        lines.append(f"status    unchanged ({after['status']})")

    for label, key in (("resolved", "resolved_blockers"), ("new     ", "new_blockers")):
        items = result[key]
        lines.append(f"{label}  {len(items)} blocking issue(s)")
        lines += [f"          - {item[:200]}" for item in items[:10]]

    if result["gained_approvals"] or result["lost_approvals"]:
        gained = ", ".join(result["gained_approvals"]) or "—"
        lost = ", ".join(result["lost_approvals"]) or "—"
        lines.append(f"approvals +{gained} / -{lost}")

    old_v, new_v = result["verification"]["before"], result["verification"]["after"]
    if old_v or new_v:
        def describe(record: Optional[dict]) -> str:
            if not record:
                return "not run"
            return f"`{record.get('command')}` exit {record.get('exit_code')}"

        lines.append(f"verify    {describe(old_v)} -> {describe(new_v)}")

    diff = result["proposal"]
    if diff["changed"]:
        lines.append("")
        lines.append(f"proposal  +{diff['added']} / -{diff['removed']} line(s) changed")
        lines += diff["lines"]
        if diff["truncated"]:
            lines.append("          (diff truncated)")
    else:
        lines.append("")
        lines.append("proposal  unchanged")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codex-gravity-compare",
        description="Compare two Codex-Gravity sessions (status, blockers, proposal).",
    )
    parser.add_argument("sessions", nargs="*", help="Two session ids (default: the two most recent)")
    parser.add_argument("-w", "--workspace", default=".", help="Project directory")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    if not args.sessions:
        chosen = recent_sessions(workspace, 2)
        if len(chosen) < 2:
            print("Need two sessions to compare; run the team at least twice.", flush=True)
            raise SystemExit(2)
        first, second = chosen[1], chosen[0]
    elif len(args.sessions) == 1:
        others = [sid for sid in recent_sessions(workspace, 50) if sid != args.sessions[0]]
        if not others:
            print("Need a second session to compare against.", flush=True)
            raise SystemExit(2)
        first, second = args.sessions[0], others[0]
    elif len(args.sessions) == 2:
        first, second = args.sessions
    else:
        print("Pass at most two session ids.", flush=True)
        raise SystemExit(2)

    result = compare(snapshot(workspace, first), snapshot(workspace, second))
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(render_text(result))
