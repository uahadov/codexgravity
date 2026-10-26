import asyncio
import pytest

from codex_gravity.event_bus import EventBus, Event, EventType


@pytest.mark.asyncio
async def test_event_bus_pub_sub_and_wildcard():
    bus = EventBus()
    received_dialogues = []
    received_all = []

    def on_dialogue(event: Event):
        received_dialogues.append(event)

    async def on_all(event: Event):
        received_all.append(event)

    sub_d = bus.subscribe(EventType.AGENT_DIALOGUE, on_dialogue)
    bus.subscribe("*", on_all)

    # Publish dialogue event
    await bus.send_dialogue(
        sender="codex",
        recipient="antigravity",
        message="Created draft patch.",
        payload={"anchors": 2},
    )

    # Publish state change event
    e2 = Event(
        event_type=EventType.STATE_CHANGE,
        sender="router",
        recipient="all",
        message="Transitioning to VERIFYING",
        payload={"phase": "VERIFYING"},
    )
    await bus.publish(e2)

    assert len(received_dialogues) == 1
    assert received_dialogues[0].message == "Created draft patch."
    assert len(received_all) == 2

    # Unsubscribe
    assert bus.unsubscribe(sub_d) is True
    await bus.send_dialogue(sender="antigravity", recipient="codex", message="Ready.")
    assert len(received_dialogues) == 1
    assert len(received_all) == 3


@pytest.mark.asyncio
async def test_event_bus_human_intervention_and_transcript():
    bus = EventBus()

    await bus.send_dialogue("codex", "antigravity", "Writing auth middleware.")
    await bus.send_dialogue("antigravity", "codex", "Waiting for AST anchor completion.")
    await bus.inject_human_intervention("Use Argon2 instead of bcrypt!", recipient="codex")

    # History filtering
    human_events = bus.get_history(event_type=EventType.HUMAN_INTERVENTION)
    assert len(human_events) == 1
    assert human_events[0].sender == "human"
    assert "Argon2" in human_events[0].message

    # Format transcript
    transcript = bus.format_walkie_talkie_log()
    assert "[CODEX ➜ ANTIGRAVITY]: Writing auth middleware." in transcript
    assert "[HUMAN ➜ CODEX]: Use Argon2 instead of bcrypt!" in transcript


@pytest.mark.asyncio
async def test_event_bus_stream_queue():
    bus = EventBus()
    q = bus.create_stream_queue()

    await bus.send_dialogue("codex", "antigravity", "Streaming test message.")
    queued_event = await asyncio.wait_for(q.get(), timeout=1.0)
    assert queued_event.message == "Streaming test message."

    bus.remove_stream_queue(q)
