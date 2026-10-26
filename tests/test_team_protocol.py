import json
import os

import pytest

from codex_gravity.event_bus import EventBus, EventType
from codex_gravity.team.protocol import (
    ALL,
    Kind,
    Role,
    TeamChannel,
    TeamError,
    TeamMessage,
    TeamTranscript,
    new_session_id,
)


def test_transcript_persists_private_jsonl(tmp_path):
    session_id = new_session_id()
    transcript = TeamTranscript(tmp_path, session_id)
    transcript.append(TeamMessage(session_id=session_id, kind=Kind.TASK, sender="orchestrator", recipient=ALL, body="hi"))

    assert transcript.path.exists()
    assert (os.stat(transcript.path).st_mode & 0o777) == 0o600

    reloaded = TeamTranscript(tmp_path, session_id).messages()
    assert len(reloaded) == 1
    assert reloaded[0].body == "hi"
    assert reloaded[0].kind is Kind.TASK


def test_invalid_session_id_is_rejected(tmp_path):
    with pytest.raises(TeamError):
        TeamTranscript(tmp_path, "../../etc")
    with pytest.raises(TeamError):
        TeamTranscript(tmp_path, "ABCDEF123456")
    with pytest.raises(TeamError):
        TeamTranscript(tmp_path, "abc")


@pytest.mark.asyncio
async def test_channel_drains_mailbox_and_mirrors_events(tmp_path):
    bus = EventBus()
    channel = TeamChannel(TeamTranscript(tmp_path, new_session_id()), bus)

    await channel.send("codex", "antigravity", Kind.CRITIQUE, "please fix", round=1)

    inbox = channel.drain("antigravity")
    assert [m.body for m in inbox] == ["please fix"]
    # Everyone is copied, so a human/dashboard observer sees the same dialogue.
    assert [m.body for m in channel.drain(ALL)] == ["please fix"]
    assert channel.drain("antigravity") == []

    events = bus.get_history(event_type=EventType.AGENT_DIALOGUE)
    assert len(events) == 1
    assert events[0].payload["team_session"] == channel.session_id
    assert events[0].recipient == "antigravity"


@pytest.mark.asyncio
async def test_ask_answer_share_one_correlation_id(tmp_path):
    channel = TeamChannel(TeamTranscript(tmp_path, new_session_id()))
    question, answer = await channel.ask(
        "codex", "antigravity", "Which storage engine?", round=1, answer="Redis"
    )

    assert question.kind is Kind.QUESTION
    assert answer is not None and answer.kind is Kind.ANSWER
    assert answer.reply_to == question.id
    assert question.correlation_id == answer.correlation_id
    thread = channel.thread(question.correlation_id)
    assert [m.kind for m in thread] == [Kind.QUESTION, Kind.ANSWER]


@pytest.mark.asyncio
async def test_channel_cancel_and_markdown(tmp_path):
    channel = TeamChannel(TeamTranscript(tmp_path, new_session_id()))
    await channel.send("human", ALL, Kind.NOTE, "keep it small", role=Role.HUMAN)
    channel.cancel()
    assert channel.cancelled is True

    markdown = channel.transcript.to_markdown()
    assert "# Codex-Gravity team session" in markdown
    assert "human → all" in markdown
    assert "keep it small" in markdown


def test_message_serializes_to_json(tmp_path):
    message = TeamMessage(session_id=new_session_id(), kind=Kind.PROPOSAL, sender="codex", recipient=ALL, body="plan")
    payload = json.loads(message.model_dump_json())
    assert payload["kind"] == "proposal"
    assert TeamMessage.model_validate(payload).id == message.id
