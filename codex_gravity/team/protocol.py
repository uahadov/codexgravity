"""
Two-way team protocol: messages, threading, persistent transcripts and mailboxes.

Everything the team says to itself is a :class:`TeamMessage` with a stable id, a
correlation id for question/answer pairs, a round number and an explicit sender
and recipient. Transcripts are append-only JSONL inside
``<workspace>/.codex-gravity/team/<session_id>/`` with private permissions, so a
session survives an MCP restart and can be audited afterwards.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from codex_gravity.event_bus import Event, EventBus, EventType

SESSION_ID_PATTERN = re.compile(r"^[a-f0-9]{12}$")
ALL = "all"


class TeamError(RuntimeError):
    """Raised when a team operation cannot be performed safely."""


class Role(str, Enum):
    ORCHESTRATOR = "orchestrator"
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    CRITIC = "critic"
    JUDGE = "judge"
    HUMAN = "human"
    SYSTEM = "system"


class Kind(str, Enum):
    TASK = "task"
    PROPOSAL = "proposal"
    CRITIQUE = "critique"
    QUESTION = "question"
    ANSWER = "answer"
    REVISION = "revision"
    APPROVAL = "approval"
    REJECTION = "rejection"
    NOTE = "note"
    CONSENSUS = "consensus"
    VERIFICATION = "verification"
    ESCALATION = "escalation"
    STATUS = "status"


class TeamMessage(BaseModel):
    """One line of team dialogue. Immutable once written to the transcript."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    session_id: str
    round: int = 0
    kind: Kind
    sender: str
    recipient: str
    role: Role = Role.ORCHESTRATOR
    subject: str = ""
    body: str = ""
    reply_to: Optional[str] = None
    correlation_id: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("session_id")
    @classmethod
    def _valid_session(cls, value: str) -> str:
        if not SESSION_ID_PATTERN.fullmatch(value):
            raise ValueError("session_id must be 12 lowercase hex characters")
        return value

    def headline(self) -> str:
        subject = f" — {self.subject}" if self.subject else ""
        return f"[r{self.round}] {self.sender} -> {self.recipient} ({self.kind.value}){subject}"


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


class TeamTranscript:
    """Append-only, private, per-session transcript of team dialogue."""

    def __init__(self, workspace: str | Path, session_id: str) -> None:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise TeamError("Invalid session id.")
        root = Path(workspace).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise TeamError("workspace must be an existing absolute directory.")
        base = root / ".codex-gravity" / "team"
        for parent in (root / ".codex-gravity", base):
            if parent.is_symlink():
                raise TeamError("Team state directories cannot be symbolic links.")
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.workspace = root.resolve()
        self.session_id = session_id
        self.directory = (base / session_id).resolve()
        if not _is_within(self.directory, base):
            raise TeamError("Team state must remain inside the workspace.")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.path = self.directory / "transcript.jsonl"

    # -- writing -----------------------------------------------------------
    def append(self, message: TeamMessage) -> TeamMessage:
        payload = json.dumps(message.model_dump(mode="json"), ensure_ascii=False) + "\n"
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return message

    def write_report(self, name: str, content: str) -> Path:
        target = (self.directory / name).resolve()
        if not _is_within(target, self.directory):
            raise TeamError("Report path escapes the session directory.")
        fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
        return target

    # -- reading -----------------------------------------------------------
    def messages(self) -> list[TeamMessage]:
        if not self.path.exists():
            return []
        out: list[TeamMessage] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(TeamMessage.model_validate(json.loads(line)))
            except (ValueError, json.JSONDecodeError):
                continue
        return out

    def render(self, limit: int = 14) -> str:
        """Compact transcript text handed to an agent as shared context."""
        messages = self.messages()[-limit:]
        if not messages:
            return "(empty transcript)"
        return "\n".join(
            f"{m.headline()}\n{m.body.strip()[:2000]}" for m in messages
        )

    def to_markdown(self, title: str = "Codex-Gravity team session") -> str:
        messages = self.messages()
        lines = [f"# {title}", "", f"- Session: `{self.session_id}`", f"- Workspace: `{self.workspace}`", ""]
        for m in messages:
            stamp = time.strftime("%H:%M:%S", time.localtime(m.created_at))
            lines.append(f"### {stamp} · {m.sender} → {m.recipient} · {m.kind.value} · round {m.round}")
            if m.subject:
                lines.append(f"**{m.subject}**")
            lines.append("")
            lines.append(m.body.strip() or "(empty)")
            lines.append("")
        return "\n".join(lines)


class TeamChannel:
    """
    Delivery layer over a transcript.

    ``send`` persists the message, mirrors it onto the shared EventBus (so the
    Studio dashboard shows the same dialogue) and drops it into the recipient's
    mailbox. Mailboxes let a running session receive questions, answers and
    human interjections without restarting.
    """

    def __init__(self, transcript: TeamTranscript, event_bus: Optional[EventBus] = None) -> None:
        self.transcript = transcript
        self.event_bus = event_bus
        self._inboxes: dict[str, list[TeamMessage]] = {}
        self._cancelled = False

    @property
    def session_id(self) -> str:
        return self.transcript.session_id

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    async def send(
        self,
        sender: str,
        recipient: str,
        kind: Kind,
        body: str,
        *,
        round: int = 0,
        role: Role = Role.ORCHESTRATOR,
        subject: str = "",
        reply_to: Optional[str] = None,
        correlation_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> TeamMessage:
        message = TeamMessage(
            session_id=self.session_id,
            round=round,
            kind=kind,
            sender=sender,
            recipient=recipient,
            role=role,
            subject=subject,
            body=body,
            reply_to=reply_to,
            correlation_id=correlation_id,
            metadata=metadata or {},
        )
        self.transcript.append(message)
        self._inboxes.setdefault(recipient, []).append(message)
        if recipient != ALL:
            self._inboxes.setdefault(ALL, []).append(message)
        if self.event_bus is not None:
            await self.event_bus.publish(
                Event(
                    event_type=EventType.AGENT_DIALOGUE,
                    sender=sender,
                    recipient=recipient,
                    message=f"[team:{self.session_id} r{round}] {kind.value}: {body.strip()[:600]}",
                    payload={
                        "team_session": self.session_id,
                        "message_id": message.id,
                        "kind": kind.value,
                        "round": round,
                        "correlation_id": correlation_id,
                        "subject": subject,
                        # Full text for live viewers (the human-readable message
                        # field is intentionally truncated for logs).
                        "body": body[:4000],
                    },
                )
            )
        return message

    async def ask(
        self,
        sender: str,
        recipient: str,
        question: str,
        *,
        round: int = 0,
        role: Role = Role.ORCHESTRATOR,
        answer: Optional[str] = None,
    ) -> tuple[TeamMessage, Optional[TeamMessage]]:
        """
        Record a question and (when supplied) its answer under one correlation id.

        A caller that cannot answer immediately records the question only; the
        answer is appended later with the same correlation id, so the pair stays
        linked across turns and process restarts.
        """
        correlation = uuid.uuid4().hex[:12]
        question_msg = await self.send(
            sender, recipient, Kind.QUESTION, question,
            round=round, role=role, correlation_id=correlation, subject="cross-examination",
        )
        answer_msg = None
        if answer is not None:
            answer_msg = await self.send(
                recipient, sender, Kind.ANSWER, answer,
                round=round, role=role, reply_to=question_msg.id,
                correlation_id=correlation, subject="answer",
            )
        return question_msg, answer_msg

    def drain(self, recipient: str = ALL) -> list[TeamMessage]:
        return self._inboxes.pop(recipient, [])

    def thread(self, correlation_id: str) -> list[TeamMessage]:
        return [m for m in self.transcript.messages() if m.correlation_id == correlation_id]
