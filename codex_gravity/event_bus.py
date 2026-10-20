"""
Sütun 2: Inter-Agent Walkie-Talkie Bus.
Provides live pub/sub backchannel dialogue between Codex and Google Antigravity,
tracks communication history, and supports human-in-the-loop intervention.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Optional, Union
import uuid
from pydantic import BaseModel, Field


class EventType(str, Enum):
    AGENT_DIALOGUE = "agent_dialogue"
    STATE_CHANGE = "state_change"
    DRAFT_CREATED = "draft_created"
    SPECULATIVE_PATCH = "speculative_patch"
    VERIFICATION_RESULT = "verification_result"
    VISUAL_DIFF = "visual_diff"
    HUMAN_INTERVENTION = "human_intervention"
    TOKEN_ARBITRAGE = "token_arbitrage"
    SYSTEM = "system"


class Event(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: EventType
    sender: str
    recipient: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)

    def format_line(self) -> str:
        time_str = self.timestamp.strftime("%H:%M:%S")
        sender_label = self.sender.upper()
        recipient_label = self.recipient.upper()
        return f"[{time_str}] [{sender_label} ➜ {recipient_label}]: {self.message}"


EventHandler = Union[
    Callable[[Event], Any],
    Callable[[Event], Coroutine[Any, Any, Any]],
]


class EventBus:
    """
    Central Asynchronous Event Bus for agent backchannel and walkie-talkie logs.
    """

    def __init__(self, max_history: int = 1000) -> None:
        self._subscribers: dict[str, list[tuple[str, EventHandler]]] = {}
        self._history: list[Event] = []
        self._max_history = max(10, max_history)
        self._lock = asyncio.Lock()
        self._stream_queues: list[asyncio.Queue[Event]] = []

    @property
    def history(self) -> list[Event]:
        return list(self._history)

    def subscribe(self, event_type: Union[EventType, str], handler: EventHandler) -> str:
        """
        Subscribes a handler to a specific event type or '*' for all events.
        Returns a unique subscription ID.
        """
        type_key = event_type.value if isinstance(event_type, EventType) else str(event_type)
        sub_id = str(uuid.uuid4())
        if type_key not in self._subscribers:
            self._subscribers[type_key] = []
        self._subscribers[type_key].append((sub_id, handler))
        return sub_id

    def unsubscribe(self, sub_id: str) -> bool:
        """
        Unsubscribes a subscriber by its subscription ID.
        """
        found = False
        for type_key, handlers in self._subscribers.items():
            self._subscribers[type_key] = [h for h in handlers if h[0] != sub_id]
            if len(handlers) != len(self._subscribers[type_key]):
                found = True
        return found

    async def publish(self, event: Event) -> None:
        """
        Publishes an event to matching subscribers and streams.
        Enforces bounded history and concurrent non-blocking handler dispatch.
        """
        async with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

        # Distribute to live stream queues
        dead_queues = []
        for q in list(self._stream_queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
            except Exception:
                dead_queues.append(q)

        for dq in dead_queues:
            if dq in self._stream_queues:
                self._stream_queues.remove(dq)

        # Collect relevant handlers
        handlers_to_run: list[EventHandler] = []
        type_key = event.event_type.value if isinstance(event.event_type, EventType) else str(event.event_type)

        if type_key in self._subscribers:
            handlers_to_run.extend([h[1] for h in self._subscribers[type_key]])
        if "*" in self._subscribers:
            handlers_to_run.extend([h[1] for h in self._subscribers["*"]])

        # Execute synchronous and asynchronous handlers
        async_coros = []
        for handler in handlers_to_run:
            try:
                res = handler(event)
                if asyncio.iscoroutine(res):
                    async_coros.append(res)
            except Exception:
                pass

        if async_coros:
            await asyncio.gather(*async_coros, return_exceptions=True)

    async def send_dialogue(
        self,
        sender: str,
        recipient: str,
        message: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> Event:
        """
        Convenience method for agent-to-agent backchannel communication.
        """
        event = Event(
            event_type=EventType.AGENT_DIALOGUE,
            sender=sender,
            recipient=recipient,
            message=message,
            payload=payload or {},
        )
        await self.publish(event)
        return event

    async def inject_human_intervention(
        self,
        message: str,
        recipient: str = "all",
        payload: Optional[dict[str, Any]] = None,
    ) -> Event:
        """
        Allows human operator to intervene in the walkie-talkie stream.
        """
        event = Event(
            event_type=EventType.HUMAN_INTERVENTION,
            sender="human",
            recipient=recipient,
            message=message,
            payload=payload or {},
        )
        await self.publish(event)
        return event

    def get_history(
        self,
        event_type: Optional[Union[EventType, str]] = None,
        sender: Optional[str] = None,
        recipient: Optional[str] = None,
        limit: int = 100,
    ) -> list[Event]:
        """
        Filters stored event history.
        """
        filtered = self._history
        if event_type:
            type_str = event_type.value if isinstance(event_type, EventType) else str(event_type)
            filtered = [e for e in filtered if e.event_type.value == type_str]
        if sender:
            filtered = [e for e in filtered if e.sender.lower() == sender.lower()]
        if recipient:
            filtered = [e for e in filtered if e.recipient.lower() == recipient.lower()]
        return filtered[-limit:]

    def format_walkie_talkie_log(self, limit: int = 50) -> str:
        """
        Formats dialogue events into a readable walkie-talkie transcript.
        """
        dialogues = self.get_history(event_type=EventType.AGENT_DIALOGUE, limit=limit)
        human_events = self.get_history(event_type=EventType.HUMAN_INTERVENTION, limit=limit)
        all_chat = sorted(dialogues + human_events, key=lambda e: e.timestamp)
        if not all_chat:
            return "--- Walkie-Talkie is quiet ---"
        return "\n".join(e.format_line() for e in all_chat[-limit:])

    def create_stream_queue(self, maxsize: int = 256) -> asyncio.Queue[Event]:
        """
        Creates and registers an asyncio Queue for live SSE / WebSocket event streams.
        """
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._stream_queues.append(q)
        return q

    def remove_stream_queue(self, q: asyncio.Queue[Event]) -> None:
        if q in self._stream_queues:
            self._stream_queues.remove(q)
