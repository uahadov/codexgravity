import io

import pytest

from codex_gravity.team.consensus import ConsensusDecision
from codex_gravity.team.session import TeamResult, VerificationRecord
from codex_gravity.tui import PlainUi, make_ui
from codex_gravity.tui_screen import (
    MIN_HEIGHT,
    MIN_WIDTH,
    CursesDashboard,
    DashboardState,
    render,
    round_counts,
    sparkline,
    wrap,
)
from tests.team_fakes import ScriptedAgent


def feed(*items: dict) -> list[dict]:
    return list(items)


def message(kind: str, sender="codex", recipient="antigravity", body="body", round_no=1, at=0.0) -> dict:
    return {"kind": kind, "sender": sender, "recipient": recipient, "body": body, "round": round_no, "at": at}


def state_with(feed_items: list[dict], *, result: TeamResult | None = None) -> DashboardState:
    state = DashboardState(
        task="Add retry handling to the HTTP client",
        workspace="/srv/project",
        session="9f3c1ae0b7d2",
        agents={
            "codex": ScriptedAgent("codex", []),
            "antigravity": ScriptedAgent("antigravity", []),
        },
        feed=feed_items,
        round_no=2,
    )
    state.result = result
    return state


def verified_result() -> TeamResult:
    return TeamResult(
        session_id="9f3c1ae0b7d2",
        status="verified",
        rounds=2,
        task="Add retry handling",
        workspace="/srv/project",
        proposal="# Plan v2",
        decision=ConsensusDecision(
            achieved=True, reason="Unanimous approval.", approvals=["codex", "antigravity"]
        ),
        verification=VerificationRecord(
            command="pytest -q", exit_code=0, passed=True, duration_seconds=1.2, stdout_tail="ok"
        ),
        transcript_messages=9,
    )


def test_render_is_exactly_the_requested_size():
    canvas = render(state_with([message("proposal")]), 100, 26)
    assert len(canvas.chars) == 26
    assert all(len(row) == 100 for row in canvas.chars)

    assert canvas.chars[0][0] == "┌"
    assert canvas.chars[0][-1] == "┐"
    assert canvas.chars[-1][0] == "└"
    assert canvas.chars[-1][-1] == "┘"


def test_header_shows_task_session_and_task_line():
    text = render(state_with([]), 100, 26).text()
    assert "Codex · Gravity" in text
    assert "session 9f3c1ae0b7d2" in text
    assert "Add retry handling to the HTTP client" in text


def test_agent_panel_marks_ready_and_reason():
    state = state_with([])
    state.agents["codex-gui"] = ScriptedAgent("codex-gui", [], available=False)
    text = render(state, 100, 26).text()
    assert "✓ codex" in text
    assert "✗ codex-gui" in text
    assert "scripted-unavailable" in text


def test_transcript_panel_renders_badges_rounds_and_body():
    state = state_with([
        message("proposal", body="# Plan v1\n- retry wrapper"),
        {"kind": "round", "round": 2, "at": 0.0},
        message("critique", sender="antigravity", body="No budget test."),
    ])
    text = render(state, 110, 28).text()
    assert "PLAN" in text
    assert "codex → antigravity" in text
    assert "retry wrapper" in text
    assert "── round 2" in text
    assert "CRIT" in text
    assert "No budget test." in text


def test_consensus_panel_reports_status_approvals_and_tests():
    text = render(state_with([], result=verified_result()), 110, 28).text()
    assert "CONSENSUS" in text
    assert "VERIFIED" in text
    assert "codex, antigravity" in text
    assert "PASS exit 0" in text


def test_status_panel_shows_round_and_activity():
    state = state_with([message("proposal", round_no=1), message("critique", round_no=2)])
    text = render(state, 110, 28).text()
    assert "round 2" in text
    assert "activity" in text
    assert "last  waiting" in text


def test_help_overlay_lists_keys():
    state = state_with([message("proposal")])
    state.show_help = True
    text = render(state, 110, 28).text()
    assert "KEYS" in text
    assert "q / Esc" in text
    assert "PgUp PgDn" in text
    assert "retry wrapper" not in text


def test_scroll_shows_older_lines_when_not_following():
    items = [message("proposal", body=f"plan {index}") for index in range(40)]
    state = state_with(items)
    state.follow = False
    state.scroll = 20
    text = render(state, 100, 24).text()
    assert "plan 39" not in text  # scrolled away from the newest entry

    state.scroll = 0
    state.follow = True
    assert "plan 39" in render(state, 100, 24).text()


def test_small_terminal_gets_an_explicit_message():
    text = render(state_with([]), MIN_WIDTH - 10, MIN_HEIGHT - 4).text()
    assert "Terminal too small" in text
    assert f"{MIN_WIDTH}x{MIN_HEIGHT}" in text


def test_wrap_and_sparkline_helpers():
    assert wrap("a" * 25, 10) == ["a" * 10, "a" * 10, "a" * 5]
    assert wrap("one\n\ntwo", 20) == ["one", "", "two"]
    assert wrap("x" * 40, 10, limit=2)[-1].endswith("…")

    assert sparkline([], 5) == ""
    assert len(sparkline([1, 2, 3, 4], 10)) == 4
    assert sparkline([0, 0], 5) == "▁▁"

    assert round_counts([{"round": 1}, {"round": 1}, {"round": 2}, {"kind": "round"}]) == [2, 1]


def test_make_ui_gui_falls_back_to_plain_when_piped():
    assert isinstance(make_ui("gui", io.StringIO()), PlainUi)
    assert isinstance(make_ui("curses", io.StringIO()), PlainUi)


def test_dashboard_without_a_terminal_delegates_to_plain_fallback():
    stream = io.StringIO()
    dashboard = CursesDashboard(stream)
    dashboard.header(task="t", workspace="/tmp", session="abcdef123456", agents={})
    dashboard.on_message(session_id="abcdef123456", round_no=1, kind="proposal",
                         sender="codex", recipient="all", body="hello", at=0.0)
    dashboard.summary(verified_result())
    out = stream.getvalue()
    assert "PLAN" in out
    assert "VERIFIED" in out
    assert dashboard.state.feed == []  # never rendered, nothing buffered


@pytest.mark.asyncio
async def test_wait_close_returns_immediately_without_a_terminal():
    dashboard = CursesDashboard(io.StringIO())
    await dashboard.wait_close(timeout=0.01)  # must not hang
