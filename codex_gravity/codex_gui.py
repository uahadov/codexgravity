"""
Use the ChatGPT/Codex **desktop app** as a teammate — no CLI required.

The desktop exposes its own MCP tool catalogue (`codex-app-tools`). This module
discovers that catalogue at runtime instead of assuming tool names, sends one
message to an exact existing chat, and waits for a *new completed* reply.

It is deliberately conservative:

* nothing is sent if the catalogue has no thread-read tool (a reply could not be
  observed, so the turn would be unverifiable);
* nothing is sent while the destination chat is busy;
* an unconfirmed delivery is reported and never retried automatically;
* a message that is accepted but produces no observable reply is an error, not
  an answer.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from codex_gravity.codex_bridge import BridgeError, DesktopCodex, thread_id

MAX_TEXT = 20000
READ_TOOL_CANDIDATES = (
    "read_thread", "get_thread", "thread_read", "readThread", "getThread",
    "read_conversation", "list_thread_turns", "thread_turns",
)
SEND_TOOL_CANDIDATES = (
    "send_message_to_thread", "sendMessageToThread", "send_message", "post_message",
)
BUSY_STATES = {"active", "running", "in_progress", "working", "processing", "queued", "started"}
ASSISTANT_ROLE_HINTS = ("assistant", "codex", "agent", "model", "bot", "gpt")
NON_ASSISTANT_ROLE_HINTS = ("user", "human", "me", "system", "tool")


class CodexDesktopUnavailable(RuntimeError):
    """The desktop catalogue cannot support a verifiable team turn."""


@dataclass
class ThreadSnapshot:
    thread_id: str
    status: str = "unknown"
    busy: bool = False
    turns: int = 0
    last_turn_id: str = ""
    last_role: str = ""
    last_text: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class GUIReply:
    ok: bool
    text: str = ""
    error: str = ""
    engine: str = "codex-desktop"
    tools: list[str] = field(default_factory=list)
    turns_before: int = 0
    turns_after: int = 0
    waited_seconds: float = 0.0
    acknowledged: bool = False


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _collect_text(node: Any, out: list[str], depth: int = 0) -> None:
    if depth > 6 or not node or len("\n".join(out)) > MAX_TEXT:
        return
    if isinstance(node, str):
        text = node.strip()
        if text:
            out.append(text)
        return
    if isinstance(node, dict):
        for key in ("text", "content", "message", "output", "answer", "summary"):
            if key in node:
                _collect_text(node[key], out, depth + 1)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _collect_text(item, out, depth + 1)


def _turn_text(turn: Any) -> tuple[str, str]:
    if not isinstance(turn, dict):
        return "", str(turn)[:MAX_TEXT]
    role = str(turn.get("role") or turn.get("author") or turn.get("type") or "").strip().lower()
    out: list[str] = []
    for key in ("output", "text", "content", "message", "answer", "summary"):
        value = turn.get(key)
        if isinstance(value, str):
            if value.strip():
                out.append(value.strip())
        else:
            _collect_text(value, out)
    return role, "\n".join(out)[:MAX_TEXT]


def _role_is_assistant(role: str) -> bool:
    if not role:
        return True  # schema without roles: rely on turn change + idle state
    if any(hint in role for hint in NON_ASSISTANT_ROLE_HINTS):
        return False
    return any(hint in role for hint in ASSISTANT_ROLE_HINTS)


def snapshot_from(data: dict, expected_id: str) -> ThreadSnapshot:
    """Normalize the app's thread payload; tolerate schema differences."""
    payload = data.get("thread") if isinstance(data.get("thread"), dict) else data
    if isinstance(payload.get("threads"), list) and payload["threads"]:
        first = payload["threads"][0]
        if isinstance(first, dict):
            payload = first
    if not isinstance(payload, dict):
        payload = {}

    status_value = payload.get("status")
    if isinstance(status_value, dict):
        status = str(status_value.get("type") or status_value.get("state") or "unknown").lower()
    else:
        status = str(status_value or "unknown").lower()

    turns_list = payload.get("turns") or payload.get("items") or payload.get("messages") or []
    if not isinstance(turns_list, list):
        turns_list = []
    turns = _as_int(
        payload.get("turnCount") or payload.get("turn_count") or payload.get("numTurns"),
        len(turns_list),
    )
    last = turns_list[-1] if turns_list and isinstance(turns_list[-1], dict) else {}
    last_id = str(last.get("id") or last.get("turnId") or last.get("messageId") or "")
    role, text = _turn_text(last)

    return ThreadSnapshot(
        thread_id=str(payload.get("id") or payload.get("threadId") or expected_id),
        status=status or "unknown",
        busy=status in BUSY_STATES,
        turns=turns,
        last_turn_id=last_id,
        last_role=role,
        last_text=text,
        raw=payload,
    )


class CodexDesktopChat:
    """One exact desktop chat, driven through the app's own MCP catalogue."""

    def __init__(
        self,
        destination: str,
        *,
        client: Optional[Any] = None,
        poll_interval: float = 2.0,
        read_tool: Optional[str] = None,
        send_tool: Optional[str] = None,
        caller_thread: Optional[str] = None,
    ) -> None:
        self.destination = thread_id(destination)
        self._client = client
        self.poll_interval = max(0.2, float(poll_interval))
        self.read_tool = read_tool
        self.send_tool = send_tool
        self.caller_thread = caller_thread

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = DesktopCodex(caller_thread=self.caller_thread)
        return self._client

    @staticmethod
    def _pick(tools: list[str], explicit: Optional[str], candidates: tuple[str, ...]) -> Optional[str]:
        if explicit:
            return explicit if explicit in tools else None
        return next((name for name in candidates if name in tools), None)

    async def catalogue(self) -> list[str]:
        return await self.client.list_tools()

    async def snapshot(self, read_tool: Optional[str] = None) -> ThreadSnapshot:
        tools = await self.catalogue()
        tool = read_tool or self._pick(tools, self.read_tool, READ_TOOL_CANDIDATES)
        if tool is None:
            raise CodexDesktopUnavailable(
                "The desktop app-tools catalogue exposes "
                f"{', '.join(tools) or 'no tools'}, none of which can read a thread."
            )
        data = await self.client.call(tool, {
            "threadId": self.destination,
            "turnLimit": 2,
            "includeOutputs": True,
            "maxOutputCharsPerItem": MAX_TEXT,
        })
        return snapshot_from(data, self.destination)

    @staticmethod
    def _is_new_reply(before: ThreadSnapshot, after: ThreadSnapshot, message: str) -> bool:
        text = after.last_text.strip()
        if not text or after.busy:
            return False
        if text == message.strip():
            return False
        if not _role_is_assistant(after.last_role):
            return False
        return (
            after.turns > before.turns
            or bool(after.last_turn_id and after.last_turn_id != before.last_turn_id)
            or text != before.last_text.strip()
        )

    async def ask(self, message: str, *, timeout: float = 300.0, wait_idle: float = 30.0) -> GUIReply:
        """Send one message and wait for a new completed reply."""
        started = time.perf_counter()
        text = (message or "").strip()
        if not text:
            return GUIReply(ok=False, error="Refusing to send an empty message.")

        try:
            tools = await self.catalogue()
        except BridgeError as error:
            return GUIReply(ok=False, error=f"Desktop app-tools unavailable: {error}")

        read_tool = self._pick(tools, self.read_tool, READ_TOOL_CANDIDATES)
        send_tool = self._pick(tools, self.send_tool, SEND_TOOL_CANDIDATES)
        if read_tool is None:
            return GUIReply(
                ok=False, tools=tools,
                error=(
                    "No thread-read tool in the desktop catalogue "
                    f"({', '.join(tools) or 'none'}), so a reply could not be observed. "
                    "Nothing was sent."
                ),
            )
        if send_tool is None:
            return GUIReply(
                ok=False, tools=tools,
                error=(
                    "No send tool in the desktop catalogue "
                    f"({', '.join(tools) or 'none'}). Nothing was sent."
                ),
            )

        before = await self.snapshot(read_tool)
        idle_deadline = time.perf_counter() + max(0.0, wait_idle)
        while before.busy and time.perf_counter() < idle_deadline:
            await asyncio.sleep(min(self.poll_interval, max(0.2, idle_deadline - time.perf_counter())))
            before = await self.snapshot(read_tool)
        if before.busy:
            return GUIReply(
                ok=False, tools=tools, turns_before=before.turns,
                error=f"Destination chat is busy ({before.status}); nothing was sent.",
            )

        try:
            await self.client.send(self.destination, text)
        except BridgeError as error:
            return GUIReply(
                ok=False, tools=tools, turns_before=before.turns,
                error=(
                    f"Delivery was not confirmed: {error} "
                    "Inspect the chat; this adapter never resends automatically."
                ),
            )

        deadline = max(time.perf_counter() + 1.0, started + max(1.0, timeout))
        while time.perf_counter() < deadline:
            await asyncio.sleep(self.poll_interval)
            after = await self.snapshot(read_tool)
            if self._is_new_reply(before, after, text):
                return GUIReply(
                    ok=True, text=after.last_text.strip(), tools=tools,
                    turns_before=before.turns, turns_after=after.turns,
                    waited_seconds=round(time.perf_counter() - started, 2),
                    acknowledged=True,
                )

        return GUIReply(
            ok=False, tools=tools, turns_before=before.turns,
            waited_seconds=round(time.perf_counter() - started, 2),
            error=(
                "The desktop accepted the message but no completed reply was observed "
                f"within {timeout:.0f}s. Inspect the chat; no answer was assumed."
            ),
        )


def desktop_availability(thread: Optional[str] = None) -> tuple[bool, str]:
    """Cheap pre-flight probe for CLI/MCP status output."""
    destination = thread or os.environ.get("CODEX_GRAVITY_CODEX_THREAD_ID") or os.environ.get("CODEX_THREAD_ID")
    if not destination:
        return False, (
            "no Codex chat id: pass --codex-thread or set "
            "CODEX_GRAVITY_CODEX_THREAD_ID / CODEX_THREAD_ID"
        )
    try:
        thread_id(destination)
    except BridgeError as error:
        return False, str(error)
    if not os.environ.get("CODEX_APP_TOOLS_PIPE_PATH"):
        return False, (
            "CODEX_APP_TOOLS_PIPE_PATH is not inherited: launch this from the "
            "Codex/ChatGPT desktop app, or export that variable"
        )
    try:
        DesktopCodex(caller_thread=destination)
    except BridgeError as error:
        return False, str(error)
    return True, f"desktop chat {destination[:8]}… via the installed app-tools server"


def reply_to_dict(reply: GUIReply) -> dict:
    return {
        "status": "completed" if reply.ok else "unconfirmed",
        "ok": reply.ok,
        "engine": reply.engine,
        "text": reply.text,
        "error": reply.error,
        "tools": reply.tools,
        "turns_before": reply.turns_before,
        "turns_after": reply.turns_after,
        "waited_seconds": reply.waited_seconds,
        "acknowledged": reply.acknowledged,
    }


def snapshot_to_dict(snapshot: ThreadSnapshot) -> dict:
    return {
        "thread_id": snapshot.thread_id,
        "status": snapshot.status,
        "busy": snapshot.busy,
        "turns": snapshot.turns,
        "last_turn_id": snapshot.last_turn_id,
        "last_role": snapshot.last_role,
        "last_text": snapshot.last_text,
    }
