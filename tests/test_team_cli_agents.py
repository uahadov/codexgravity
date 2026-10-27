import pytest

from codex_gravity.team.agents import (
    AgentTurn,
    AntigravityCLIAgent,
    default_agents,
)
from codex_gravity.team.protocol import Role


def turn(instruction: str = "review the plan") -> AgentTurn:
    return AgentTurn(
        task="Add retry handling",
        instruction=instruction,
        context="(empty transcript)",
        workspace=__import__("pathlib").Path("."),
        role=Role.REVIEWER,
        round=1,
        session_id="abcdef123456",
        timeout=5,
    )


def test_registry_exposes_both_cli_transports():
    agents = default_agents()
    assert set(agents) == {
        "codex", "codex-cli", "codex-gui",
        "antigravity", "antigravity-cli", "antigravity-pro",
    }
    # The CLI name is an explicit alias of the same transport instance.
    assert agents["codex"] is agents["codex-cli"]


def test_cli_transport_reports_a_missing_binary(monkeypatch):
    monkeypatch.delenv("CODEX_GRAVITY_ANTIGRAVITY_CLI_BIN", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_CLI_BIN", raising=False)
    monkeypatch.delenv("CODEX_GRAVITY_ANTIGRAVITY_CLI_CMD", raising=False)
    agent = AntigravityCLIAgent(binary="/nonexistent/agentapi")
    ok, reason = agent.availability()
    assert ok is False
    assert "install it" in reason


@pytest.mark.asyncio
async def test_cli_transport_uses_a_command_template():
    agent = AntigravityCLIAgent(command_template="printf 'answer for prompt'")
    reply = await agent.respond(turn())
    assert reply.ok is True
    assert reply.text == "answer for prompt"
    assert "template" in reply.engine


@pytest.mark.asyncio
async def test_cli_transport_reports_a_failing_command():
    agent = AntigravityCLIAgent(command_template="sh -c 'exit 3'")
    reply = await agent.respond(turn())
    assert reply.ok is False
    assert "exited 3" in reply.error


@pytest.mark.asyncio
async def test_cli_transport_times_out():
    agent = AntigravityCLIAgent(command_template="sh -c 'sleep 5' {prompt}", timeout=0.3)
    reply = await agent.respond(turn())
    assert reply.ok is False
    assert "timed out" in reply.error.lower()


@pytest.mark.asyncio
async def test_cli_transport_uses_agentapi(monkeypatch):
    from unittest.mock import AsyncMock

    agent = AntigravityCLIAgent(binary="/bin/sh")
    monkeypatch.setattr("codex_gravity.team.agents.call_agentapi", AsyncMock(return_value="CLI answer"))
    reply = await agent.respond(turn())
    assert reply.ok is True
    assert reply.text == "CLI answer"
    assert reply.engine.startswith("antigravity-cli")


@pytest.mark.asyncio
async def test_cli_transport_reports_an_empty_agentapi_answer(monkeypatch):
    from unittest.mock import AsyncMock

    agent = AntigravityCLIAgent(binary="/bin/sh")
    monkeypatch.setattr("codex_gravity.team.agents.call_agentapi", AsyncMock(return_value=None))
    reply = await agent.respond(turn())
    assert reply.ok is False
    assert "produced no answer" in reply.error
