import io

import pytest

from codex_gravity.event_bus import Event, EventBus, EventType
from codex_gravity.team import TeamConfig, TeamOrchestrator
from codex_gravity.tui import AnsiUi, PlainUi, make_ui
from tests.team_fakes import ScriptedAgent, approve, revise


def team_event(session: str, kind: str, sender="codex", recipient="all", round_no=1, body="hello"):
    return Event(
        event_type=EventType.AGENT_DIALOGUE,
        sender=sender,
        recipient=recipient,
        message="x",
        payload={"team_session": session, "kind": kind, "round": round_no, "body": body},
    )


def test_ui_renders_header_and_messages():
    stream = io.StringIO()
    ui = AnsiUi(stream)
    ui.header(task="Add retry", workspace="/tmp/project", session="abcdef123456", agents={})
    ui.on_event(team_event("abcdef123456", "proposal", sender="codex", body="# Plan\n1. wrap requests"))
    ui.on_event(team_event("abcdef123456", "critique", sender="antigravity", round_no=2,
                           body="no timeout test\nadd a budget case"))

    out = stream.getvalue()
    assert "Codex · Gravity" in out
    assert "Add retry" in out
    assert "PLAN" in out
    assert "# Plan" in out
    assert "round 2" in out
    assert "CRIT" in out
    assert "add a budget case" in out


def test_ui_ignores_other_sessions():
    stream = io.StringIO()
    ui = AnsiUi(stream)
    ui.on_event(team_event("aaaaaaaaaaaa", "proposal", body="first"))
    ui.on_event(team_event("bbbbbbbbbbbb", "proposal", body="second"))
    out = stream.getvalue()
    assert "first" in out
    assert "second" not in out


def test_plain_ui_has_no_escape_codes():
    stream = io.StringIO()
    ui = make_ui("plain", stream)
    assert isinstance(ui, PlainUi)
    ui.on_event(team_event("aaaaaaaaaaaa", "approval", body="looks good"))
    assert "\033[" not in stream.getvalue()
    assert "PASS" in stream.getvalue()


def test_auto_mode_is_plain_without_a_tty():
    assert isinstance(make_ui("auto", io.StringIO()), PlainUi)


class _BrokenStream(io.StringIO):
    def write(self, _text):  # noqa: D401 - simulate a closed pipe
        raise BrokenPipeError()


def test_pipe_closed_exits_quietly():
    ui = make_ui("plain", _BrokenStream())
    with pytest.raises(SystemExit):
        ui.write("line")
    with pytest.raises(SystemExit):
        ui.status("spinner")


def test_ansi_mode_forces_colors():
    ui = make_ui("ansi", io.StringIO())
    assert isinstance(ui, AnsiUi)
    assert "\033[" in ui.format_message(kind="proposal", sender="codex", recipient="all", body="x", at=0)


@pytest.mark.asyncio
async def test_ui_renders_a_real_session_and_replay(tmp_path):
    bus = EventBus()
    stream = io.StringIO()
    ui = AnsiUi(stream)
    bus.subscribe("*", ui.on_event)

    codex = ScriptedAgent("codex", ["# Plan v1", revise("no rollback", questions=["which store?"]), approve()])
    antigravity = ScriptedAgent("antigravity", ["redis", "# Plan v2"])
    result = await TeamOrchestrator(agents={"codex": codex, "antigravity": antigravity}, event_bus=bus).run(
        TeamConfig(
            task="Add retry handling",
            workspace=str(tmp_path),
            plan_with="codex",
            implement_with="antigravity",
            review_with=["codex"],
            max_rounds=3,
        )
    )

    ui.summary(result)
    out = stream.getvalue()
    assert "Add retry handling" in out
    assert "ASK" in out and "ANS" in out and "REV" in out and "PASS" in out
    assert "CONSENSUS" in out or "consensus" in out.lower()
    assert "transcript" in out

    # Replay from disk through the standalone viewer entry point.
    from codex_gravity import tui

    args = type("Args", (), {
        "workspace": str(tmp_path),
        "session": result.session_id,
        "follow": False,
        "timeout": 1.0,
        "ui": "plain",
    })()
    assert await tui._replay(args) == 0
