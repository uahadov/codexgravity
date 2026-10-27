#!/usr/bin/env python3
"""
Render the example session report shown in the README.

The team in this demo is scripted (no model, no network): the point is to show the
report layout, and the caption says so. Writes:

    docs/report-example.html
    docs/report-example.png   (1280x1900, headless Chromium)

    python scripts/render_report_demo.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_gravity.team.agents import AgentReply, AgentTurn, TeamAgent  # noqa: E402
from codex_gravity.team.protocol import Role  # noqa: E402
from codex_gravity.team.report import render_session_html  # noqa: E402
from codex_gravity.team.session import TeamConfig, TeamOrchestrator  # noqa: E402

TASK = "Add retry handling with jitter to the HTTP client"
COMMAND = "python3 -c \"print('14 passed in 0.42s')\""


def verdict(kind: str, summary: str, blockers=(), questions=()) -> str:
    return "```json\n" + json.dumps({
        "verdict": kind, "summary": summary,
        "blocking_issues": list(blockers), "questions": list(questions),
    }) + "\n```"

PLAN = """# Plan — retries for the HTTP client

1. Add `RetryPolicy(max_attempts=4, base_delay=0.2, max_delay=5)` in `http/retry.py`.
2. Retry on `TimeoutError` and 5xx, with exponential backoff and full jitter.
3. Do not retry non-idempotent POSTs unless the caller passes `idempotent=True`.
4. Tests: timeout, 5xx, 429 with `Retry-After`, and a budget-exhausted case.
"""

REVISION = PLAN.replace(
    "4. Tests:",
    "4. Honour a total retry budget (wall-clock), not just attempt count.\n5. Tests:",
)


class DemoAgent(TeamAgent):
    """Replies from a fixed script, keyed by role and round."""

    def __init__(self, name: str) -> None:
        super().__init__(timeout=30.0)
        self.name = name
        self.turns = 0

    async def _ask(self, turn: AgentTurn, timeout: float) -> AgentReply:
        self.turns += 1
        text = self._script(turn)
        return AgentReply(ok=True, text=text, engine=f"scripted ({self.name})")

    def _script(self, turn: AgentTurn) -> str:
        if turn.role is Role.PLANNER:
            return PLAN
        if turn.role is Role.REVIEWER:
            if turn.round == 1 and self.name == "antigravity":
                return verdict(
                    "revise",
                    "No total retry budget: four attempts with full jitter can exceed 20s.",
                    ["No total retry budget: four attempts with full jitter can exceed 20s."],
                    ["Is there a wall-clock cap the caller can pass?"],
                )
            return verdict("approve", "Timeboxed and testable as written.")
        if turn.role is Role.IMPLEMENTER:
            if "Answer the reviewer's question" in turn.instruction:
                return (
                    "Yes: `RetryPolicy(deadline=2.0)` bounds the whole call. The budget is "
                    "checked before each sleep, so the last attempt never overruns it."
                )
            return REVISION
        return "No objection."


def main() -> None:
    workspace = Path(tempfile.mkdtemp(prefix="cg-report-demo-"))
    codex = DemoAgent("codex")
    antigravity = DemoAgent("antigravity")
    orchestrator = TeamOrchestrator(agents={"codex": codex, "antigravity": antigravity})

    result = asyncio.run(orchestrator.run(
        TeamConfig(
            task=TASK,
            workspace=str(workspace),
            implement_with="codex",
            review_with=["codex", "antigravity"],
            max_rounds=3,
            max_questions=1,
            test_command=COMMAND,
            verification_retries=0,
            remember_decisions=False,
        ),
        agents={"codex": codex, "antigravity": antigravity},
    ))
    print(f"session {result.session_id}: {result.status} in {result.rounds} round(s)")

    page = render_session_html(workspace, result.session_id)
    html_path = ROOT / "docs" / "report-example.html"
    html_path.write_text(page, encoding="utf-8")

    chromium = Path.home() / ".gemini" / "antigravity" / "bin" / "chromium"
    binary = shutil.which("chromium") or shutil.which("chromium-browser") or str(chromium)
    png = ROOT / "docs" / "report-example.png"
    subprocess.run(
        [
            binary, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-sandbox",
            "--force-device-scale-factor=1", "--window-size=1280,2620",
            "--virtual-time-budget=3000", f"--screenshot={png}", html_path.as_uri(),
        ],
        check=True,
        capture_output=True,
    )
    print(f"wrote {html_path.relative_to(ROOT)} ({html_path.stat().st_size // 1024} KB)")
    print(f"wrote {png.relative_to(ROOT)} ({png.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
