"""Deterministic scripted agents for team tests (no network, no model)."""

from __future__ import annotations

import json

from codex_gravity.team.agents import AgentReply, AgentTurn, TeamAgent


def approve(summary: str = "Looks implementable.") -> str:
    return "```json\n" + json.dumps(
        {"verdict": "approve", "summary": summary, "blocking_issues": [], "questions": []}
    ) + "\n```"


def revise(issue: str = "Missing error handling for timeouts.", questions: list[str] | None = None) -> str:
    return "```json\n" + json.dumps(
        {
            "verdict": "revise",
            "summary": "Needs another pass.",
            "blocking_issues": [issue],
            "questions": questions or [],
        }
    ) + "\n```"


def reject(issue: str = "Approach is unsafe.") -> str:
    return "```json\n" + json.dumps(
        {"verdict": "reject", "summary": "Unsafe.", "blocking_issues": [issue], "questions": []}
    ) + "\n```"


class ScriptedAgent(TeamAgent):
    """Plays back a queue of replies and records every turn it received."""

    def __init__(self, name: str, replies: list | None = None, *, available: bool = True, timeout: float = 5.0) -> None:
        super().__init__(timeout=timeout)
        self.name = name
        self._replies = list(replies or [])
        self._available = available
        self.calls: list[AgentTurn] = []

    def availability(self) -> tuple[bool, str]:
        return (True, "scripted") if self._available else (False, "scripted-unavailable")

    @property
    def instructions(self) -> list[str]:
        return [turn.instruction for turn in self.calls]

    async def _ask(self, turn: AgentTurn, timeout: float) -> AgentReply:
        self.calls.append(turn)
        if not self._available:
            return AgentReply(ok=False, error="scripted-unavailable", engine="scripted")
        item = self._replies.pop(0) if self._replies else approve()
        if isinstance(item, AgentReply):
            return item
        if callable(item):
            return AgentReply(ok=True, text=str(item(turn)), engine="scripted")
        return AgentReply(ok=True, text=str(item), engine="scripted")
