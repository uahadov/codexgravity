#!/usr/bin/env python3
"""
Regenerate the dashboard preview assets:

    docs/dashboard.svg   - a real render() of a finished session (README image)
    docs/demo.cast       - asciinema v2 recording of the dashboard building up
                           (play it with: asciinema play docs/demo.cast)

Both come from `codex_gravity.tui_screen.render`, so they cannot drift from the
real layout. Run after changing the layout:

    python scripts/render_dashboard_svg.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from codex_gravity.tui_screen import DashboardState, render

WIDTH, HEIGHT = 108, 28

STYLE_HEX = {
    "border": "#64748b",
    "title": "#f59e0b",
    "dim": "#94a3b8",
    "ok": "#22c55e",
    "err": "#ef4444",
    "warn": "#f59e0b",
    "value": "#e2e8f0",
    "sender": "#22d3ee",
    "badge": "#c084fc",
}
STYLE_ANSI = {
    "border": "\033[38;5;244m",
    "title": "\033[38;5;214m",
    "dim": "\033[38;5;245m",
    "ok": "\033[38;5;114m",
    "err": "\033[38;5;203m",
    "warn": "\033[38;5;214m",
    "value": "\033[38;5;253m",
    "sender": "\033[38;5;51m",
    "badge": "\033[38;5;177m",
}

NOW = time.time()


def message(kind: str, sender: str, recipient: str, body: str, round_no: int) -> dict:
    return {"kind": kind, "sender": sender, "recipient": recipient, "body": body, "round": round_no, "at": NOW}


FEED = [
    message("task", "orchestrator", "all", "Add retry handling to the HTTP client", 0),
    message("proposal", "codex-cli", "all", "# Plan v1\n- retry wrapper around fetch()\n- no budget test yet", 0),
    {"kind": "round", "round": 1, "at": NOW},
    message("critique", "antigravity", "codex-cli",
            "verdict: revise\n\nblocking issues:\n- No retry-budget test.\n- 4xx responses must never be retried.", 1),
    message("question", "antigravity", "codex-cli", "Which backoff strategy do you intend?", 1),
    message("answer", "codex-cli", "antigravity", "Exponential with full jitter, capped at 5 attempts.", 1),
    message("revision", "codex-cli", "all",
            "# Plan v2\n- exponential backoff with full jitter\n- retries only 429/5xx/timeouts\n- test: budget exhausted after 5 attempts", 1),
    {"kind": "round", "round": 2, "at": NOW},
    message("approval", "antigravity", "codex-cli", "verdict: approve\nBudget and 4xx cases are covered.", 2),
    message("verification", "verifier", "all", "`pytest -q` exited 0 (PASS) in 4.31s", 2),
    message("consensus", "orchestrator", "all", "Unanimous approval.\n\nApproved by: antigravity, codex-cli\nStatus: verified", 2),
]


class DemoAgent:
    """Availability-only stand-in so the panels show realistic states."""

    def __init__(self, ok: bool, reason: str = "") -> None:
        self.ok = ok
        self.reason = reason

    def availability(self) -> tuple[bool, str]:
        return self.ok, self.reason


AGENTS = {
    "antigravity": DemoAgent(True),
    "antigravity-cli": DemoAgent(True),
    "antigravity-pro": DemoAgent(True),
    "codex": DemoAgent(False, "Codex CLI is unavailable. Set CODEX_GRAVITY_CODEX_BIN…"),
    "codex-cli": DemoAgent(True),
    "codex-gui": DemoAgent(False, "no Codex chat id: pass --codex-thread…"),
}


def build_state(feed: list[dict], status: str = "verified") -> DashboardState:
    from codex_gravity.team.consensus import ConsensusDecision
    from codex_gravity.team.session import TeamResult, VerificationRecord

    state = DashboardState(
        task="Add retry handling to the HTTP client",
        workspace="/srv/project",
        session="9f3c1ae0b7d2",
        agents=AGENTS,
        feed=list(feed),
        round_no=2,
        status=status,
        started=time.monotonic() - 74,
    )
    state.result = TeamResult(
        session_id="9f3c1ae0b7d2",
        status="verified",
        rounds=2,
        task=state.task,
        workspace=state.workspace,
        proposal="# Plan v2",
        decision=ConsensusDecision(achieved=True, reason="Unanimous approval.", approvals=["antigravity", "codex-cli"]),
        verification=VerificationRecord(command="pytest -q", exit_code=0, passed=True, duration_seconds=4.31),
        transcript_messages=len(feed),
    )
    return state


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_svg(canvas) -> str:
    char_w, line_h, pad = 7.8, 17, 12
    width = int(canvas.width * char_w + pad * 2)
    height = int(canvas.height * line_h + pad * 2)
    rows = []
    for row in range(canvas.height):
        runs: list[str] = []
        column = 0
        styles = canvas.styles[row]
        chars = canvas.chars[row]
        while column < canvas.width:
            style = styles[column]
            end = column
            while end < canvas.width and styles[end] == style:
                end += 1
            chunk = escape("".join(chars[column:end]))
            if chunk.strip():
                color = STYLE_HEX.get(style or "value", STYLE_HEX["value"])
                runs.append(f'<tspan fill="{color}">{chunk}</tspan>')
            else:
                runs.append(chunk)
            column = end
        y = pad + line_h * (row + 1) - 4
        rows.append(f'<text x="{pad}" y="{y}" xml:space="preserve">{"".join(runs)}</text>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-label="Codex-Gravity dashboard">\n'
        f'  <rect width="{width}" height="{height}" rx="10" fill="#0b0b0f"/>\n'
        f'  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10" fill="none" '
        f'stroke="#1f2937"/>\n'
        f'  <g font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" '
        f'font-size="13" letter-spacing="0.2">\n'
        + "\n".join("    " + row for row in rows)
        + "\n  </g>\n</svg>\n"
    )


def to_ansi(canvas) -> str:
    out = []
    for row in range(canvas.height):
        parts: list[str] = []
        column = 0
        styles = canvas.styles[row]
        chars = canvas.chars[row]
        while column < canvas.width:
            style = styles[column]
            end = column
            while end < canvas.width and styles[end] == style:
                end += 1
            chunk = "".join(chars[column:end])
            if chunk.strip() and style in STYLE_ANSI:
                parts.append(f"{STYLE_ANSI[style]}{chunk}\033[0m")
            else:
                parts.append(chunk)
            column = end
        out.append("".join(parts))
    return "\n".join(out)


def write_cast(path: Path) -> None:
    """A minimal asciinema v2 recording that replays the dashboard building up."""
    events: list[list] = []
    stamps = [0.4, 1.1, 1.8, 2.4, 3.0, 3.6, 4.4, 5.2]
    for index, stamp in enumerate(stamps):
        feed = FEED[: max(1, min(len(FEED), 2 + index * 2))]
        status = "verified" if index == len(stamps) - 1 else "waiting for antigravity"
        canvas = render(build_state(feed, status), WIDTH, HEIGHT)
        frame = "\033[H\033[2J" + to_ansi(canvas)
        events.append([stamp, "o", frame])
    header = {
        "version": 2,
        "width": WIDTH,
        "height": HEIGHT,
        "timestamp": int(NOW),
        "env": {"SHELL": "/bin/fish", "TERM": "xterm-256color"},
        "title": "Codex-Gravity — two agents, one transcript",
    }
    lines = [json.dumps(header)] + [json.dumps(event) for event in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    canvas = render(build_state(FEED), WIDTH, HEIGHT)
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)

    svg_path = docs / "dashboard.svg"
    svg_path.write_text(to_svg(canvas), encoding="utf-8")
    cast_path = docs / "demo.cast"
    write_cast(cast_path)

    print(f"wrote {svg_path.relative_to(ROOT)} ({svg_path.stat().st_size} bytes)")
    print(f"wrote {cast_path.relative_to(ROOT)} ({cast_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
