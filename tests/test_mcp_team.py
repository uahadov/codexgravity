import json
from unittest.mock import patch

import pytest

from codex_gravity.mcp_server import create_mcp_server
from tests.team_fakes import ScriptedAgent, approve

PLAN = "# Plan\n1. Add idempotency keys\n2. Store them with the request hash\n"


def _payload(res) -> dict:
    return json.loads(res.content[0].text)


@pytest.mark.asyncio
async def test_mcp_team_tools_roundtrip(tmp_path):
    server = create_mcp_server()
    agents = {
        "codex": ScriptedAgent("codex", [PLAN, approve("Agreed.")]),
        "antigravity": ScriptedAgent("antigravity", [approve("Agreed.")]),
    }
    with patch("codex_gravity.mcp_server.default_agents", return_value=agents):
        res = await server.call_tool("team_run_task", {
            "prompt": "Add idempotency keys to the payment API",
            "workspace_path": str(tmp_path),
            "review_with": "codex,antigravity",
            "max_rounds": 2,
        })
    data = _payload(res)
    assert data["status"] == "consensus"
    assert data["decision"]["achieved"] is True
    assert data["transcript_messages"] >= 5
    session_id = data["session_id"]

    status = _payload(await server.call_tool("team_status", {
        "workspace_path": str(tmp_path), "session_id": session_id,
    }))
    assert status["status"] == "finished"
    assert status["messages"] == data["transcript_messages"]

    messages = _payload(await server.call_tool("team_transcript", {
        "workspace_path": str(tmp_path), "session_id": session_id,
    }))
    assert isinstance(messages, list)
    assert messages[0]["kind"] == "task"
    assert any(m["kind"] == "proposal" for m in messages)

    markdown = (await server.call_tool("team_transcript", {
        "workspace_path": str(tmp_path), "session_id": session_id, "fmt": "markdown",
    })).content[0].text
    assert "# Codex-Gravity team session" in markdown
    assert "idempotency" in markdown

    sent = _payload(await server.call_tool("team_send_message", {
        "workspace_path": str(tmp_path), "session_id": session_id,
        "body": "Ship after CI is green.", "kind": "note",
    }))
    assert sent["status"] == "stored"
    assert sent["message"]["kind"] == "note"

    cancel = _payload(await server.call_tool("team_cancel", {
        "workspace_path": str(tmp_path), "session_id": session_id,
    }))
    assert cancel["status"] == "not_running"


@pytest.mark.asyncio
async def test_mcp_team_run_task_validates_input(tmp_path):
    server = create_mcp_server()
    res = await server.call_tool("team_run_task", {
        "prompt": "   ",
        "workspace_path": str(tmp_path),
    })
    data = _payload(res)
    assert data["status"] == "error"
    assert "Invalid team request" in data["error"]

    missing = _payload(await server.call_tool("team_send_message", {
        "workspace_path": str(tmp_path), "session_id": "ffffffffffff", "body": "hi",
    }))
    assert missing["status"] == "error"

    bad_kind = _payload(await server.call_tool("team_status", {
        "workspace_path": str(tmp_path), "session_id": "../../etc",
    }))
    assert bad_kind["status"] == "error"


@pytest.mark.asyncio
async def test_mcp_team_agents_reports_transports():
    server = create_mcp_server()
    data = _payload(await server.call_tool("team_agents", {}))
    assert "codex" in data
    assert "antigravity" in data
    assert set(data["codex"]) == {"name", "transport_available", "reason"}


@pytest.mark.asyncio
async def test_mcp_team_report_compare_and_decisions(tmp_path):
    server = create_mcp_server()
    sessions = []
    for issue in ("Missing retry limit", "Missing retry limit, no test"):
        agents = {
            "codex": ScriptedAgent("codex", [PLAN, approve("Agreed.")]),
            "antigravity": ScriptedAgent("antigravity", [approve(issue)]),
        }
        with patch("codex_gravity.mcp_server.default_agents", return_value=agents):
            res = await server.call_tool("team_run_task", {
                "prompt": "Add idempotency keys to the payment API",
                "workspace_path": str(tmp_path),
                "review_with": "codex,antigravity",
            })
        sessions.append(_payload(res)["session_id"])

    report = _payload(await server.call_tool("team_report", {
        "workspace_path": str(tmp_path), "session_id": sessions[0],
    }))
    assert report["status"] == "written"
    assert report["bytes"] > 500

    diff = _payload(await server.call_tool("team_compare", {
        "workspace_path": str(tmp_path),
        "session_id": sessions[0], "against": sessions[1],
    }))
    assert diff["before"]["session_id"] == sessions[0]
    assert diff["after"]["session_id"] == sessions[1]
    assert "status" in diff["summary"]

    decisions = _payload(await server.call_tool("team_decisions", {
        "workspace_path": str(tmp_path),
    }))
    assert decisions["entries"] >= 2
    assert decisions["empty"] is False
    assert "idempotency" in decisions["text"]

