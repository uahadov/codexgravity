import sys

import pytest

from codex_gravity.team.protocol import Kind, TeamError
from codex_gravity.team.session import TeamConfig, TeamOrchestrator
from tests.team_fakes import ScriptedAgent, approve, revise

PLAN = "# Plan\n1. Add a retry wrapper\n2. Cover timeout and 5xx\n"


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


@pytest.mark.asyncio
async def test_team_reaches_unanimous_consensus(tmp_path):
    codex = ScriptedAgent("codex", [PLAN, approve("Solid plan.")])
    antigravity = ScriptedAgent("antigravity", [approve("Agreed.")])
    orchestrator = TeamOrchestrator(agents={"codex": codex, "antigravity": antigravity})

    result = await orchestrator.run(_config(tmp_path, review_with=["codex", "antigravity"]))

    assert result.status == "consensus"
    assert result.rounds == 1
    assert result.proposal == PLAN.strip()
    assert sorted(result.decision.approvals) == ["antigravity", "codex"]
    assert result.verification is None

    assert (str(tmp_path.resolve()), result.session_id) not in orchestrator._active

    from codex_gravity.team.protocol import TeamTranscript
    messages = TeamTranscript(tmp_path, result.session_id).messages()
    seen = [m.kind for m in messages]
    assert seen[0] is Kind.TASK
    assert Kind.PROPOSAL in seen
    assert seen.count(Kind.APPROVAL) == 2
    assert seen[-1] is Kind.CONSENSUS
    assert result.report_path and result.report_path.endswith("report.md")
    assert (tmp_path / ".codex-gravity" / "team" / result.session_id / "result.json").exists()


@pytest.mark.asyncio
async def test_cross_examination_then_revision(tmp_path):
    codex = ScriptedAgent("codex", [PLAN, revise(questions=["Which queue backs the retries?"]), approve()])
    antigravity = ScriptedAgent("antigravity", ["Redis Streams.", PLAN + "3. Use Redis Streams\n"])
    orchestrator = TeamOrchestrator(agents={"codex": codex, "antigravity": antigravity})

    result = await orchestrator.run(_config(tmp_path, max_rounds=3, max_questions=1))

    assert result.status == "consensus"
    assert result.rounds == 2
    assert "Redis Streams" in result.proposal

    from codex_gravity.team.protocol import TeamTranscript
    messages = TeamTranscript(tmp_path, result.session_id).messages()
    questions = [m for m in messages if m.kind is Kind.QUESTION]
    answers = [m for m in messages if m.kind is Kind.ANSWER]
    revisions = [m for m in messages if m.kind is Kind.REVISION]

    assert len(questions) == 1 and len(answers) == 1 and len(revisions) == 1
    assert answers[0].reply_to == questions[0].id
    assert answers[0].correlation_id == questions[0].correlation_id
    assert questions[0].recipient == "antigravity"
    assert answers[0].recipient == "codex"
    # The reviewer actually received the implementer's answer as shared context.
    assert "Redis Streams" in codex.calls[-1].context


@pytest.mark.asyncio
async def test_deadlock_escalates_without_claiming_approval(tmp_path):
    codex = ScriptedAgent("codex", [PLAN, revise("Still no rollback path."), revise("Still no rollback path.")])
    antigravity = ScriptedAgent("antigravity", ["r1", "r2"])
    orchestrator = TeamOrchestrator(agents={"codex": codex, "antigravity": antigravity})

    result = await orchestrator.run(_config(tmp_path, max_rounds=2))

    assert result.status == "deadlocked"
    assert result.rounds == 2
    assert result.decision.achieved is False
    assert any("rollback" in issue for issue in result.decision.blockers)

    from codex_gravity.team.protocol import TeamTranscript
    last = TeamTranscript(tmp_path, result.session_id).messages()[-1]
    assert last.kind is Kind.ESCALATION


@pytest.mark.asyncio
async def test_planner_unavailable_returns_unavailable_without_inventing_a_plan(tmp_path):
    codex = ScriptedAgent("codex", [], available=False)
    antigravity = ScriptedAgent("antigravity", [])
    orchestrator = TeamOrchestrator(agents={"codex": codex, "antigravity": antigravity})

    result = await orchestrator.run(_config(tmp_path))

    assert result.status == "unavailable"
    assert result.proposal == ""
    assert "scripted-unavailable" in result.decision.reason
    assert result.decision.achieved is False
    assert result.agents["codex"]["transport_available"] is False


@pytest.mark.asyncio
async def test_unavailable_reviewer_cannot_produce_consensus(tmp_path):
    codex = ScriptedAgent("codex", [PLAN])
    antigravity = ScriptedAgent("antigravity", [], available=False)
    orchestrator = TeamOrchestrator(agents={"codex": codex, "antigravity": antigravity})

    result = await orchestrator.run(_config(tmp_path, review_with=["antigravity"], max_rounds=1))

    assert result.status == "deadlocked"
    assert result.decision.achieved is False
    assert result.decision.unavailable == ["antigravity"]


@pytest.mark.asyncio
async def test_verification_pass_and_fail(tmp_path):
    codex = ScriptedAgent("codex", [PLAN, approve(), PLAN, approve()])
    antigravity = ScriptedAgent("antigravity", ["r1", "r2"])
    orchestrator = TeamOrchestrator(agents={"codex": codex, "antigravity": antigravity})

    ok = await orchestrator.run(
        _config(tmp_path, test_command=f'"{sys.executable}" -c "print(1)"', max_rounds=1)
    )
    assert ok.status == "verified"
    assert ok.verification is not None and ok.verification.passed is True

    failing = TeamOrchestrator(
        agents={"codex": ScriptedAgent("codex", [PLAN, approve()]), "antigravity": ScriptedAgent("antigravity", [])}
    )
    bad = await failing.run(
        _config(tmp_path, test_command=f'"{sys.executable}" -c "raise SystemExit(3)"',
                verification_retries=0, max_rounds=1)
    )
    assert bad.status == "failed"
    assert bad.verification is not None and bad.verification.exit_code == 3
    assert bad.decision.achieved is True  # the plan was agreed; the build is what failed

    from codex_gravity.team.protocol import TeamTranscript
    kinds = [m.kind for m in TeamTranscript(tmp_path, bad.session_id).messages()]
    assert Kind.VERIFICATION in kinds


@pytest.mark.asyncio
async def test_human_notes_are_injected_into_reviews(tmp_path):
    codex = ScriptedAgent("codex", [PLAN, approve()])
    antigravity = ScriptedAgent("antigravity", [])
    orchestrator = TeamOrchestrator(agents={"codex": codex, "antigravity": antigravity})

    await orchestrator.run(_config(tmp_path, human_notes=["No new dependencies, please."], max_rounds=1))

    assert "No new dependencies, please." in codex.calls[1].instruction


@pytest.mark.asyncio
async def test_unknown_agent_is_a_team_error(tmp_path):
    orchestrator = TeamOrchestrator(agents={"codex": ScriptedAgent("codex", [PLAN])})
    with pytest.raises(TeamError):
        await orchestrator.run(_config(tmp_path, implement_with="gemini-from-mars"))


@pytest.mark.asyncio
async def test_status_and_message_delivery_after_run(tmp_path):
    codex = ScriptedAgent("codex", [PLAN, approve()])
    antigravity = ScriptedAgent("antigravity", [])
    orchestrator = TeamOrchestrator(agents={"codex": codex, "antigravity": antigravity})
    config = _config(tmp_path, session_id="abcdef123456")

    result = await orchestrator.run(config)
    assert result.session_id == "abcdef123456"

    status = orchestrator.status(str(tmp_path), "abcdef123456")
    assert status["status"] == "finished"
    assert status["messages"] >= 4

    stored = await orchestrator.send_message(
        str(tmp_path), "abcdef123456", "human", "all", "note", "Looks good, ship after tests."
    )
    assert stored["status"] == "stored"
    assert stored["message"]["kind"] == "note"

    after = orchestrator.status(str(tmp_path), "abcdef123456")
    assert after["messages"] == status["messages"] + 1

    with pytest.raises(TeamError):
        await orchestrator.send_message(str(tmp_path), "ffffffffffff", "human", "all", "note", "hi")


@pytest.mark.asyncio
async def test_reverse_direction_antigravity_plans_codex_reviews(tmp_path):
    """The roles are symmetric: Antigravity can propose and Codex can push back."""
    antigravity = ScriptedAgent("antigravity", ["# Antigravity plan v1", "# Antigravity plan v2"])
    codex = ScriptedAgent("codex", [revise("No rollback strategy."), approve("Now it is safe.")])
    orchestrator = TeamOrchestrator(agents={"codex": codex, "antigravity": antigravity})

    result = await orchestrator.run(_config(
        tmp_path,
        plan_with="antigravity",
        implement_with="antigravity",
        review_with=["codex"],
        max_rounds=3,
    ))

    assert result.status == "consensus"
    assert result.proposal == "# Antigravity plan v2"
    assert result.decision.approvals == ["codex"]
    # Codex's objection reached Antigravity, and its answer reached Codex.
    assert "No rollback strategy." in antigravity.calls[1].instruction
    assert "Antigravity plan v2" in codex.calls[-1].context


@pytest.mark.asyncio
async def test_cancelled_session_reports_cancelled(tmp_path):
    holder: dict = {}

    def cancelling_review(turn):
        holder["orchestrator"].cancel(str(tmp_path), "0123456789ab")
        return revise()

    orchestrator = TeamOrchestrator(agents={
        "codex": ScriptedAgent("codex", [PLAN, cancelling_review]),
        "antigravity": ScriptedAgent("antigravity", ["answer", "revision"]),
    })
    holder["orchestrator"] = orchestrator

    result = await orchestrator.run(_config(tmp_path, session_id="0123456789ab", max_rounds=3))

    assert result.status == "cancelled"
    assert result.decision.achieved is False
    assert any("cancel" in item.lower() for item in result.next_actions)
