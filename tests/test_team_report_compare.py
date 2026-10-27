import json
import os
import sys
from pathlib import Path

import pytest

from codex_gravity.team import compare as compare_mod
from codex_gravity.team import report as report_mod
from codex_gravity.team.session import TeamConfig, TeamOrchestrator
from tests.team_fakes import ScriptedAgent, approve, reject

PLAN = "# Plan\n1. Add a retry wrapper\n2. Cover timeout and 5xx\n"


def _config(tmp_path, **overrides) -> TeamConfig:
    data = {
        "task": "Add retry handling to the HTTP client",
        "workspace": str(tmp_path),
        "plan_with": "codex",
        "implement_with": "antigravity",
        "review_with": ["codex"],
        "max_rounds": 2,
        "remember_decisions": False,
    }
    data.update(overrides)
    return TeamConfig(**data)


def _session(tmp_path, session_id: str, **result) -> Path:
    folder = tmp_path / ".codex-gravity" / "team" / session_id
    folder.mkdir(parents=True)
    payload = {"session_id": session_id, "transcript_messages": 3, "rounds": 1,
               "elapsed_seconds": 1.0, "task": "task", "proposal": "", **result}
    (folder / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    return folder


# -- report -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_renders_verdict_transcript_and_verification(tmp_path):
    codex = ScriptedAgent("codex", [PLAN, approve("Solid plan.")])
    orchestrator = TeamOrchestrator(agents={"codex": codex})
    result = await orchestrator.run(_config(tmp_path, test_command="true"))

    page = report_mod.render_session_html(tmp_path, result.session_id)

    assert page.startswith("<!doctype html>")
    assert "Add retry handling to the HTTP client" in page
    assert "verified" in page
    assert "Solid plan." in page
    assert "exit 0" in page
    assert "Add a retry wrapper" in page  # transcript body
    assert "<script" not in page and "src=" not in page  # self-contained, no scripts


@pytest.mark.asyncio
async def test_report_escapes_model_output(tmp_path):
    nasty = '<script>alert(1)</script> "quoted" & <b>bold</b>'
    codex = ScriptedAgent("codex", [f"# Plan\n1. {nasty}\n", reject(nasty)])
    orchestrator = TeamOrchestrator(agents={"codex": codex})
    result = await orchestrator.run(_config(tmp_path, max_rounds=1))

    page = report_mod.render_session_html(tmp_path, result.session_id)

    assert "<script" not in page
    assert "&lt;script&gt;" in page
    assert "&lt;b&gt;bold&lt;/b&gt;" in page


def test_report_handles_missing_result_file(tmp_path):
    folder = tmp_path / ".codex-gravity" / "team" / "abcdef012345"
    folder.mkdir(parents=True)

    page = report_mod.render_session_html(tmp_path, "abcdef012345")

    assert "unknown" in page
    assert "abcdef012345" in page


def test_write_report_and_latest_session(tmp_path):
    older = _session(tmp_path, "aaaaaaaaaaaa", status="deadlocked")
    newer = _session(tmp_path, "bbbbbbbbbbbb", status="consensus")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    assert report_mod.latest_session(tmp_path) == "bbbbbbbbbbbb"
    path = report_mod.write_report(tmp_path, "aaaaaaaaaaaa")

    assert path.name == "report.html"
    assert path.parent == older
    assert "deadlocked" in path.read_text(encoding="utf-8")


def test_report_cli_writes_file(tmp_path, monkeypatch, capsys):
    _session(tmp_path, "cccccccccccc", status="consensus")
    monkeypatch.setattr(sys, "argv", ["codex-gravity-report", "-w", str(tmp_path)])

    report_mod.main()

    assert "wrote" in capsys.readouterr().out
    assert (tmp_path / ".codex-gravity" / "team" / "cccccccccccc" / "report.html").exists()


def test_report_cli_without_sessions_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["codex-gravity-report", "-w", str(tmp_path)])

    with pytest.raises(SystemExit) as error:
        report_mod.main()

    assert error.value.code == 2
    assert "No team sessions" in capsys.readouterr().out


# -- compare ----------------------------------------------------------------


def test_compare_reports_resolved_blockers_and_proposal_diff(tmp_path):
    _session(
        tmp_path, "aaaaaaaaaaaa", status="deadlocked", rounds=2,
        decision={"approvals": [], "blockers": ["No timeout handling", "No tests"]},
        proposal="# Plan\n1. Retry loop\n",
    )
    _session(
        tmp_path, "bbbbbbbbbbbb", status="consensus", rounds=3,
        decision={"approvals": ["codex", "antigravity"], "blockers": ["No tests"]},
        proposal="# Plan\n1. Retry loop\n2. Timeout of 2s\n3. Tests for 5xx\n",
    )

    result = compare_mod.compare(
        compare_mod.snapshot(tmp_path, "aaaaaaaaaaaa"),
        compare_mod.snapshot(tmp_path, "bbbbbbbbbbbb"),
    )

    assert result["resolved_blockers"] == ["No timeout handling"]
    assert result["new_blockers"] == []
    assert result["gained_approvals"] == ["antigravity", "codex"]
    assert result["status_changed"] is True
    assert result["proposal"]["changed"] is True
    assert result["proposal"]["added"] >= 2

    text = compare_mod.render_text(result)
    assert "deadlocked -> consensus" in text
    assert "No timeout handling" in text
    assert "+2" in text


def test_compare_identical_sessions(tmp_path):
    for session_id in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
        _session(tmp_path, session_id, status="consensus", proposal="# Plan\n1. Retry\n")

    result = compare_mod.compare(
        compare_mod.snapshot(tmp_path, "aaaaaaaaaaaa"),
        compare_mod.snapshot(tmp_path, "bbbbbbbbbbbb"),
    )

    assert result["status_changed"] is False
    assert result["proposal"]["changed"] is False
    assert "proposal  unchanged" in compare_mod.render_text(result)


def test_recent_sessions_newest_first(tmp_path):
    old = _session(tmp_path, "aaaaaaaaaaaa")
    new = _session(tmp_path, "bbbbbbbbbbbb")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    assert compare_mod.recent_sessions(tmp_path, 2) == ["bbbbbbbbbbbb", "aaaaaaaaaaaa"]


def test_compare_cli_uses_two_latest_sessions(tmp_path, monkeypatch, capsys):
    old = _session(tmp_path, "aaaaaaaaaaaa", status="deadlocked", proposal="# Plan\n1. A\n")
    new = _session(tmp_path, "bbbbbbbbbbbb", status="consensus", proposal="# Plan\n1. A\n2. B\n")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    monkeypatch.setattr(sys, "argv", ["codex-gravity-compare", "-w", str(tmp_path)])

    compare_mod.main()

    out = capsys.readouterr().out
    assert "bbbbbbbbbbbb" in out and "deadlocked -> consensus" in out


def test_compare_cli_needs_two_sessions(tmp_path, monkeypatch, capsys):
    _session(tmp_path, "aaaaaaaaaaaa")
    monkeypatch.setattr(sys, "argv", ["codex-gravity-compare", "-w", str(tmp_path)])

    with pytest.raises(SystemExit) as error:
        compare_mod.main()

    assert error.value.code == 2
    assert "Need two sessions" in capsys.readouterr().out


def test_compare_cli_json(tmp_path, monkeypatch, capsys):
    _session(tmp_path, "aaaaaaaaaaaa", status="deadlocked")
    _session(tmp_path, "bbbbbbbbbbbb", status="consensus")
    monkeypatch.setattr(
        sys, "argv",
        ["codex-gravity-compare", "-w", str(tmp_path), "aaaaaaaaaaaa", "bbbbbbbbbbbb", "--json"],
    )

    compare_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["after"]["session_id"] == "bbbbbbbbbbbb"
    assert payload["status_changed"] is True


def test_compare_snapshot_without_result_file(tmp_path):
    folder = Path(tmp_path) / ".codex-gravity" / "team" / "dddddddddddd"
    folder.mkdir(parents=True)

    snap = compare_mod.snapshot(tmp_path, "dddddddddddd")

    assert snap["status"] == "unknown"
    assert snap["blockers"] == []
