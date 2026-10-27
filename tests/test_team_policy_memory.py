import time
from pathlib import Path

import pytest

from codex_gravity.team.agents import AgentTurn
from codex_gravity.team.config import (
    DEFAULT_POLICY,
    load_policy,
    policy_path,
    policy_summary,
    write_policy,
)
from codex_gravity.team.memory import (
    append_decision,
    decisions_count,
    decisions_path,
    load_decisions,
    proposal_points,
)
from codex_gravity.team.protocol import Kind, Role, TeamError, TeamTranscript
from codex_gravity.team.session import TeamConfig, TeamOrchestrator, _BudgetedAgent
from tests.team_fakes import ScriptedAgent, approve, reject

PLAN = "# Plan\n1. Add a retry wrapper\n2. Cover timeout and 5xx\n"


def _turn() -> AgentTurn:
    return AgentTurn(
        task="task", instruction="instruction", context="",
        workspace=Path(".").resolve(), role=Role.PLANNER, round=0, session_id="abcdef012345",
    )


def _config(tmp_path, **overrides) -> TeamConfig:
    data = {
        "task": "Add retry handling to the HTTP client",
        "workspace": str(tmp_path),
        "plan_with": "codex",
        "implement_with": "antigravity",
        "review_with": ["codex"],
        "max_rounds": 2,
    }
    data.update(overrides)
    return TeamConfig(**data)


# -- policy -----------------------------------------------------------------


def test_init_writes_policy_and_refuses_overwrite(tmp_path):
    path = write_policy(tmp_path, overrides={"max_rounds": 2, "test_command": "pytest -q"})

    assert path == policy_path(tmp_path)
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = load_policy(tmp_path)
    assert loaded["max_rounds"] == 2
    assert loaded["test_command"] == "pytest -q"
    # Unset keys fall back to the defaults instead of disappearing.
    assert loaded["implement_with"] == DEFAULT_POLICY["implement_with"]

    with pytest.raises(TeamError, match="already exists"):
        write_policy(tmp_path)
    write_policy(tmp_path, force=True)
    assert load_policy(tmp_path)["max_rounds"] == DEFAULT_POLICY["max_rounds"]


def test_load_policy_without_file_is_defaults(tmp_path):
    policy = load_policy(tmp_path)
    assert policy == DEFAULT_POLICY
    assert policy is not DEFAULT_POLICY


def test_unknown_setting_is_rejected(tmp_path):
    path = policy_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"rounds": 4}', encoding="utf-8")

    with pytest.raises(TeamError, match="unknown setting"):
        load_policy(tmp_path)


def test_invalid_rounds_are_rejected(tmp_path):
    path = policy_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"max_rounds": 99}', encoding="utf-8")

    with pytest.raises(TeamError, match="max_rounds"):
        load_policy(tmp_path)


def test_policy_summary_lists_roles_and_budget():
    summary = policy_summary({**DEFAULT_POLICY, "max_agent_calls": 12, "veto_agents": ["codex"]})
    assert "plan      codex" in summary
    assert "review    codex, antigravity" in summary
    assert "12 calls" in summary
    assert "veto      codex" in summary


# -- decision memory --------------------------------------------------------


def test_decisions_append_and_load(tmp_path):
    for index in range(3):
        append_decision(
            tmp_path, task=f"Task {index}", session_id=f"abc{index}", status="consensus",
            approvals=["codex", "antigravity"], proposal=f"# Plan {index}\n1. Step {index}\n",
        )

    text = load_decisions(tmp_path)
    assert "Task 0" in text and "Task 2" in text
    assert decisions_count(tmp_path) == 3

    limited = load_decisions(tmp_path, limit=1)
    assert "Task 2" in limited and "Task 0" not in limited


def test_decisions_missing_file_is_empty(tmp_path):
    assert load_decisions(tmp_path) == ""
    assert decisions_count(tmp_path) == 0
    assert not decisions_path(tmp_path).exists()


def test_proposal_points_skip_headings():
    points = proposal_points("# Plan\n\n- Add a retry wrapper\n1. Cover timeouts\n2. Log failures\n")
    assert points == ["Add a retry wrapper", "Cover timeouts", "Log failures"]


@pytest.mark.asyncio
async def test_consensus_writes_decision_memory(tmp_path):
    codex = ScriptedAgent("codex", [PLAN, approve()])
    orchestrator = TeamOrchestrator(agents={"codex": codex})

    result = await orchestrator.run(_config(tmp_path))

    assert result.status == "consensus"
    memory = decisions_path(tmp_path)
    assert result.decision_memory == str(memory)
    text = memory.read_text(encoding="utf-8")
    assert "Add retry handling" in text and "consensus" in text and "Add a retry wrapper" in text


@pytest.mark.asyncio
async def test_deadlock_does_not_write_memory(tmp_path):
    codex = ScriptedAgent("codex", [PLAN, reject("Unsafe."), reject("Still unsafe.")])
    orchestrator = TeamOrchestrator(agents={"codex": codex})

    result = await orchestrator.run(_config(tmp_path, max_rounds=2))

    assert result.status == "deadlocked"
    assert result.decision_memory is None
    assert not decisions_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_remember_decisions_can_be_disabled(tmp_path):
    append_decision(
        tmp_path, task="Earlier decision", session_id="aaaa", status="consensus",
        approvals=["codex"], proposal="1. Use Redis for the queue\n",
    )
    codex = ScriptedAgent("codex", [PLAN, approve()])
    orchestrator = TeamOrchestrator(agents={"codex": codex})

    result = await orchestrator.run(_config(tmp_path, remember_decisions=False))

    assert result.decision_memory is None
    assert decisions_count(tmp_path) == 1
    assert all("PROJECT DECISIONS" not in text for text in codex.instructions)


@pytest.mark.asyncio
async def test_previous_decisions_reach_the_planner_and_the_reviewer(tmp_path):
    append_decision(
        tmp_path, task="Earlier decision", session_id="aaaa", status="consensus",
        approvals=["codex"], proposal="1. Use Redis for the queue\n",
    )
    codex = ScriptedAgent("codex", [PLAN, approve()])
    orchestrator = TeamOrchestrator(agents={"codex": codex})

    await orchestrator.run(_config(tmp_path))

    assert "PROJECT DECISIONS" in codex.instructions[0]
    assert "Use Redis for the queue" in codex.instructions[0]
    assert "PROJECT DECISIONS" in codex.instructions[1]


# -- budget -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_budget_stops_the_run_without_a_verdict(tmp_path):
    codex = ScriptedAgent("codex", [PLAN, approve()])
    orchestrator = TeamOrchestrator(agents={"codex": codex})

    result = await orchestrator.run(_config(tmp_path, max_agent_calls=1))

    assert result.status == "budget_exhausted"
    assert len(codex.calls) == 1  # planner only; the reviewer turn was refused
    assert result.decision.achieved is False
    assert any("budget" in action.lower() for action in result.next_actions)

    messages = TeamTranscript(tmp_path, result.session_id).messages()
    assert Kind.STATUS in [m.kind for m in messages]
    assert any("budget" in (m.body or "").lower() for m in messages)


@pytest.mark.asyncio
async def test_budgeted_agent_refuses_turns_after_the_deadline():
    agent = ScriptedAgent("codex", [approve()])
    budget = {"calls": 0, "stopped": ""}
    proxy = _BudgetedAgent(agent, budget, 0, time.monotonic() - 1)

    reply = await proxy.respond(_turn())  # the budget refuses before the agent is touched

    assert reply.ok is False
    assert "deadline" in reply.error
    assert budget["stopped"] == "run deadline reached"
    assert agent.calls == []


@pytest.mark.asyncio
async def test_budgeted_agent_forwards_and_counts_when_unlimited():
    agent = ScriptedAgent("codex", [approve()])
    budget = {"calls": 0, "stopped": ""}
    proxy = _BudgetedAgent(agent, budget, 0, None)

    assert proxy.name == "codex"
    assert proxy.describe()["name"] == "codex"
    reply = await proxy.respond(_turn())

    assert reply.ok is True
    assert budget["calls"] == 1
    assert len(agent.calls) == 1
