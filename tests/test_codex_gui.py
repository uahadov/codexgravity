import asyncio

import pytest

from codex_gravity.codex_bridge import BridgeError
from codex_gravity.codex_gui import (
    CodexDesktopChat,
    CodexDesktopUnavailable,
    desktop_availability,
    snapshot_from,
)

THREAD = "01a168f9-d943-7a50-be88-2b7a7579a3c9"
BOTH = ["read_thread", "send_message_to_thread"]


def thread_payload(turns, status="idle"):
    return {"thread": {"id": THREAD, "kind": "codex", "status": {"type": status}, "turns": turns}}


class FakeDesktop:
    """Stands in for DesktopCodex: same three async methods, no sockets."""

    def __init__(self, responses, tools=None, send_error=None):
        self.tools = BOTH if tools is None else tools
        self.responses = list(responses)
        self.send_error = send_error
        self.sent: list[tuple[str, str]] = []
        self.read_calls = 0

    async def list_tools(self):
        return list(self.tools)

    async def call(self, name, arguments):
        self.read_calls += 1
        return self.responses.pop(0) if self.responses else self.responses_fallback()

    def responses_fallback(self):
        return thread_payload([{"id": "t1", "role": "assistant", "text": "unchanged"}])

    async def send(self, destination, message):
        self.sent.append((destination, message))
        if self.send_error is not None:
            raise self.send_error
        return {"threadId": destination}


def chat_with(fake, **kwargs):
    kwargs.setdefault("poll_interval", 0.2)
    return CodexDesktopChat(THREAD, client=fake, **kwargs)


@pytest.mark.asyncio
async def test_ask_returns_only_a_new_assistant_reply():
    fake = FakeDesktop([
        thread_payload([{"id": "t1", "role": "assistant", "text": "old answer"}]),
        thread_payload([
            {"id": "t1", "role": "assistant", "text": "old answer"},
            {"id": "t2", "role": "assistant", "text": "new answer"},
        ]),
    ])
    reply = await chat_with(fake).ask("please review", timeout=2)

    assert reply.ok is True
    assert reply.text == "new answer"
    assert reply.turns_before == 1 and reply.turns_after == 2
    assert reply.acknowledged is True
    assert reply.tools == BOTH
    assert fake.sent == [(THREAD, "please review")]


@pytest.mark.asyncio
async def test_no_read_tool_means_nothing_is_sent():
    fake = FakeDesktop([], tools=["send_message_to_thread"])
    reply = await chat_with(fake).ask("please review", timeout=1)

    assert reply.ok is False
    assert "could not be observed" in reply.error or "No thread-read tool" in reply.error
    assert fake.sent == []


@pytest.mark.asyncio
async def test_no_send_tool_means_nothing_is_sent():
    fake = FakeDesktop([], tools=["read_thread"])
    reply = await chat_with(fake).ask("please review", timeout=1)

    assert reply.ok is False
    assert "No send tool" in reply.error
    assert fake.sent == []


@pytest.mark.asyncio
async def test_busy_chat_is_never_interrupted():
    busy = thread_payload([{"id": "t1", "role": "assistant", "text": "working"}], status="active")
    fake = FakeDesktop([busy, busy])
    reply = await chat_with(fake).ask("ping", timeout=1, wait_idle=0)

    assert reply.ok is False
    assert "busy" in reply.error
    assert fake.sent == []


@pytest.mark.asyncio
async def test_unconfirmed_delivery_is_reported_and_not_retried():
    fake = FakeDesktop([thread_payload([])], send_error=BridgeError("ack lost"))
    reply = await chat_with(fake).ask("ping", timeout=1)

    assert reply.ok is False
    assert "never resends" in reply.error
    assert len(fake.sent) == 1


@pytest.mark.asyncio
async def test_no_observable_reply_times_out_without_guessing():
    same = thread_payload([{"id": "t1", "role": "assistant", "text": "old"}])
    fake = FakeDesktop([same] * 20)
    reply = await chat_with(fake).ask("ping", timeout=0.3)

    assert reply.ok is False
    assert "no completed reply was observed" in reply.error
    assert reply.text == ""
    assert len(fake.sent) == 1


@pytest.mark.asyncio
async def test_user_echo_is_not_treated_as_a_reply():
    before = thread_payload([{"id": "t1", "role": "assistant", "text": "old"}])
    echo = thread_payload([
        {"id": "t1", "role": "assistant", "text": "old"},
        {"id": "t2", "role": "user", "text": "ping"},
    ])
    fake = FakeDesktop([before] + [echo] * 20)
    reply = await chat_with(fake).ask("ping", timeout=0.3)

    assert reply.ok is False
    assert reply.text == ""


def test_snapshot_tolerates_alternate_schemas():
    data = {
        "id": THREAD,
        "status": "running",
        "turnCount": 7,
        "messages": [{"messageId": "m9", "author": "assistant", "content": [{"type": "text", "text": "hi"}]}],
    }
    snapshot = snapshot_from(data, THREAD)
    assert snapshot.thread_id == THREAD
    assert snapshot.busy is True
    assert snapshot.turns == 7
    assert snapshot.last_turn_id == "m9"
    assert snapshot.last_text == "hi"
    assert snapshot.last_role == "assistant"


def test_snapshot_survives_an_empty_payload():
    snapshot = snapshot_from({}, THREAD)
    assert snapshot.thread_id == THREAD
    assert snapshot.turns == 0
    assert snapshot.busy is False
    assert snapshot.last_text == ""


def test_availability_explains_missing_configuration(monkeypatch):
    monkeypatch.delenv("CODEX_GRAVITY_CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    ok, reason = desktop_availability(None)
    assert ok is False
    assert "codex" in reason.lower()

    ok, reason = desktop_availability("not-a-uuid")
    assert ok is False
    assert "UUID" in reason

    monkeypatch.delenv("CODEX_APP_TOOLS_PIPE_PATH", raising=False)
    ok, reason = desktop_availability(THREAD)
    assert ok is False
    assert "CODEX_APP_TOOLS_PIPE_PATH" in reason


@pytest.mark.asyncio
async def test_snapshot_raises_when_catalogue_cannot_read():
    fake = FakeDesktop([], tools=["send_message_to_thread"])
    with pytest.raises(CodexDesktopUnavailable):
        await chat_with(fake).snapshot()


def test_poll_interval_floor():
    chat = CodexDesktopChat(THREAD, client=object(), poll_interval=0)
    assert chat.poll_interval == 0.2


@pytest.mark.asyncio
async def test_ask_clamps_empty_message():
    fake = FakeDesktop([])
    reply = await chat_with(fake).ask("   ")
    assert reply.ok is False
    assert fake.sent == []
    await asyncio.sleep(0)


class ThreadSimulator:
    """A desktop chat that answers every message with a scripted reply."""

    def __init__(self, reply: str):
        self.reply = reply
        self.turns = [{"id": "t0", "role": "assistant", "text": "ready"}]
        self.sent: list[str] = []

    async def list_tools(self):
        return BOTH

    async def call(self, name, arguments):
        return thread_payload(list(self.turns))

    async def send(self, destination, message):
        self.sent.append(message)
        self.turns = self.turns + [
            {"id": f"t{len(self.turns)}", "role": "assistant", "text": self.reply}
        ]
        return {"threadId": destination}


@pytest.mark.asyncio
async def test_codex_gui_agent_plans_and_reviews_inside_a_team(tmp_path):
    from codex_gravity.team import TeamConfig, TeamOrchestrator
    from codex_gravity.team.agents import CodexGUIAgent
    from tests.team_fakes import ScriptedAgent, approve

    simulator = ThreadSimulator(reply=approve("Reviewed on the desktop app."))
    gui_agent = CodexGUIAgent(
        destination=THREAD,
        timeout=5,
        chat=CodexDesktopChat(THREAD, client=simulator, poll_interval=0.2),
    )
    orchestrator = TeamOrchestrator(agents={
        "codex-gui": gui_agent,
        "antigravity": ScriptedAgent("antigravity", ["unused"]),
    })

    result = await orchestrator.run(TeamConfig(
        task="Add a bounded queue",
        workspace=str(tmp_path),
        plan_with="codex-gui",
        implement_with="antigravity",
        review_with=["codex-gui"],
        max_rounds=2,
        turn_timeout=10,
    ))

    assert result.status == "consensus"
    assert result.decision.approvals == ["codex-gui"]
    assert result.rounds == 1
    # One message for the proposal, one for the review - both into the same desktop chat.
    assert len(simulator.sent) == 2
    assert "Add a bounded queue" in simulator.sent[0]
    assert "PROPOSAL UNDER REVIEW" in simulator.sent[1]
    assert result.agents["codex-gui"]["transport_available"] is False  # no pipe in this test env
