import asyncio

import pytest

from codex_gravity.team import cli
from codex_gravity.team.config import load_policy, policy_path
from codex_gravity.team.consensus import ConsensusDecision
from codex_gravity.team.session import TeamConfig, TeamOrchestrator, TeamResult


def _args(*argv: str):
    return cli._build_parser().parse_args(list(argv))


def _fake_result(config: TeamConfig) -> TeamResult:
    return TeamResult(
        session_id=config.session_id or "abcdef012345",
        status="consensus",
        rounds=1,
        task=config.task,
        workspace=config.workspace,
        proposal="plan",
        decision=ConsensusDecision(achieved=True, reason="agreed", approvals=["codex"]),
    )


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    async def fake_run(self, config, agents=None):
        captured["config"] = config
        captured["agents"] = agents
        return _fake_result(config)

    monkeypatch.setattr(TeamOrchestrator, "run", fake_run)
    return captured


def test_init_writes_policy_and_refuses_to_overwrite(tmp_path):
    argv = ["--init", "-w", str(tmp_path), "--test-cmd", "pytest -q", "--rounds", "2"]

    assert asyncio.run(cli._run(_args(*argv))) == 0
    policy = load_policy(tmp_path)
    assert policy["test_command"] == "pytest -q"
    assert policy["max_rounds"] == 2

    assert asyncio.run(cli._run(_args("--init", "-w", str(tmp_path)))) == 2
    assert asyncio.run(cli._run(_args("--init", "-w", str(tmp_path), "--force"))) == 0
    assert load_policy(tmp_path)["test_command"] is None


def test_policy_file_drives_the_run(tmp_path, monkeypatch):
    policy_path(tmp_path).parent.mkdir(parents=True)
    policy_path(tmp_path).write_text(
        '{"plan_with": "codex", "review_with": ["codex"], "max_rounds": 1,'
        ' "remember_decisions": false, "veto_agents": ["codex"], "max_agent_calls": 7}',
        encoding="utf-8",
    )
    captured = _capture(monkeypatch)

    code = asyncio.run(cli._run(_args("Add retries", "-w", str(tmp_path), "--json")))

    assert code == 0
    config = captured["config"]
    assert config.max_rounds == 1
    assert config.remember_decisions is False
    assert config.veto_agents == ["codex"]
    assert config.max_agent_calls == 7


def test_flags_override_the_policy(tmp_path, monkeypatch):
    policy_path(tmp_path).parent.mkdir(parents=True)
    policy_path(tmp_path).write_text(
        '{"max_rounds": 3, "remember_decisions": true, "max_agent_calls": 9}', encoding="utf-8"
    )
    captured = _capture(monkeypatch)

    asyncio.run(cli._run(_args(
        "Add retries", "-w", str(tmp_path), "--json", "--rounds", "2",
        "--no-remember", "--max-calls", "4",
    )))

    config = captured["config"]
    assert config.max_rounds == 2
    assert config.remember_decisions is False
    assert config.max_agent_calls == 4


def test_unknown_policy_key_fails_closed(tmp_path, monkeypatch, capsys):
    policy_path(tmp_path).parent.mkdir(parents=True)
    policy_path(tmp_path).write_text('{"rounds": 5}', encoding="utf-8")

    code = asyncio.run(cli._run(_args("Add retries", "-w", str(tmp_path))))

    assert code == 2
    assert "Policy error" in capsys.readouterr().err


def test_dry_run_prints_the_plan_without_calling_a_model(tmp_path, monkeypatch, capsys):
    policy_path(tmp_path).parent.mkdir(parents=True)
    policy_path(tmp_path).write_text('{"max_agent_calls": 5}', encoding="utf-8")

    async def explode(self, config, agents=None):  # pragma: no cover - must not run
        raise AssertionError("dry-run must not start a session")

    monkeypatch.setattr(TeamOrchestrator, "run", explode)

    code = asyncio.run(cli._run(_args("Add retries", "-w", str(tmp_path), "--dry-run")))

    out = capsys.readouterr().out
    assert code == 0
    assert "Add retries" in out
    assert "5 calls" in out
    assert "Transports:" in out


def test_task_is_required_outside_special_modes(tmp_path, capsys):
    assert asyncio.run(cli._run(_args("-w", str(tmp_path)))) == 2
    assert "task description is required" in capsys.readouterr().err


@pytest.mark.parametrize("rounds", ["0", "11"])
def test_rounds_are_validated(rounds, tmp_path):
    parser = cli._build_parser()
    args = parser.parse_args(["task", "-w", str(tmp_path), "--rounds", rounds])

    with pytest.raises(Exception):
        asyncio.run(cli._run(args))
