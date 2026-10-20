import json
import pytest
import tempfile
from pathlib import Path

from codex_gravity.mcp_server import create_mcp_server


@pytest.mark.asyncio
async def test_mcp_server_tools_registration():
    server = create_mcp_server()
    tools = await server.list_tools()
    tool_names = [t.name for t in tools]

    expected = [
        "antigravity_inspect_media",
        "antigravity_distill_context",
        "antigravity_verify_and_screenshot",
        "call_codex_architect",
        "call_antigravity_multimodal",
    ]
    for exp in expected:
        assert exp in tool_names


@pytest.mark.asyncio
async def test_mcp_inspect_media_zero_copy():
    server = create_mcp_server()

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(b"\x00" * 1024)
        media_path = f.name

    try:
        # Call tool with local file path (Zero-copy transport)
        res = await server.call_tool(
            "antigravity_inspect_media",
            {"file_path": media_path, "focus_query": "Look for login modal"},
        )
        data = json.loads(res.content[0].text)
        assert data["status"] == "success"
        assert data["file_path"] == str(Path(media_path).resolve())
        assert data["size_bytes"] == 1024
        assert len(data["timestamps"]) > 0
    finally:
        Path(media_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_mcp_distill_context():
    server = create_mcp_server()

    # Generate sample 100-line log
    log_lines = [f"2026-10-20 INFO Routine log line {i}" for i in range(100)]
    log_lines.append("2026-10-20 ERROR PaymentGatewayTimeout: failed connecting to Stripe endpoint")
    log_text = "\n".join(log_lines)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        f.write(log_text)
        log_path = f.name

    try:
        res = await server.call_tool(
            "antigravity_distill_context",
            {"directory_or_log": log_path, "issue_description": "PaymentGatewayTimeout Stripe"},
        )
        data = json.loads(res.content[0].text)
        assert data["raw_lines_analyzed"] >= 100
        assert data["token_reduction_pct"] > 50.0
        assert "PaymentGatewayTimeout" in data["distilled_context"]
    finally:
        Path(log_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_mcp_verify_and_screenshot():
    server = create_mcp_server()
    res = await server.call_tool(
        "antigravity_verify_and_screenshot",
        {"test_command": "python3 -c 'print(\"MCP verification ok\")'", "ui_url": "http://localhost:8000"},
    )
    data = json.loads(res.content[0].text)
    assert data["passed"] is True
    assert data["exit_code"] == 0
    assert "MCP verification ok" in data["stdout_tail"]
    assert len(data["viewport_checks"]) == 3


@pytest.mark.asyncio
async def test_mcp_symmetrical_gateway():
    server = create_mcp_server()

    res_codex = await server.call_tool(
        "call_codex_architect",
        {"prompt": "Refactor caching layer to use LRU", "code_context": "cache = {}"},
    )
    codex_data = json.loads(res_codex.content[0].text)
    assert codex_data["status"] == "completed"
    assert codex_data["provider"] == "codex-o3-mini"

    res_agy = await server.call_tool(
        "call_antigravity_multimodal",
        {"media_path": "/tmp/test.png", "instruction": "Check visual layout"},
    )
    agy_data = json.loads(res_agy.content[0].text)
    assert agy_data["status"] == "completed"
    assert agy_data["provider"] == "gemini-3.8-flash"


def test_mcp_streamable_http_app():
    server = create_mcp_server()
    app = server.streamable_http_app()
    assert app is not None

    # Deep verification: actual HTTP client interaction with MCP endpoint
    from starlette.testclient import TestClient
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        resp = client.get("/mcp")
        assert resp.status_code == 400
        data = resp.json()
        assert data["jsonrpc"] == "2.0"
        assert "Missing session ID" in data["error"]["message"]


@pytest.mark.asyncio
async def test_mcp_file_uri_support():
    """
    Tests that standard file:/// URIs are correctly resolved by MCP tools.
    """
    server = create_mcp_server()

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(b"\x00" * 512)
        media_path = f.name

    try:
        file_uri = f"file://{media_path}"
        res = await server.call_tool(
            "antigravity_inspect_media",
            {"file_path": file_uri, "focus_query": "check zero copy uri"},
        )
        data = json.loads(res.content[0].text)
        assert data["status"] == "success"
        assert data["size_bytes"] == 512
    finally:
        Path(media_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_call_antigravity_verifier_tool():
    server = create_mcp_server()
    res = await server.call_tool(
        "call_antigravity_verifier",
        {"test_command": "python3 -c 'print(\"verifier tool ok\")'"},
    )
    data = json.loads(res.content[0].text)
    assert data["passed"] is True
    assert data["exit_code"] == 0
    assert "verifier tool ok" in data["stdout_tail"]


def test_mcp_dashboard_and_health_routes():
    from starlette.testclient import TestClient
    server = create_mcp_server()
    client = TestClient(server.streamable_http_app())

    # GET / returns dashboard HTML
    res_home = client.get("/")
    assert res_home.status_code == 200
    assert "Codex-Gravity MCP Server" in res_home.text
    assert "antigravity_inspect_media" in res_home.text

    # GET /health returns JSON status
    res_health = client.get("/health")
    assert res_health.status_code == 200
    data = res_health.json()
    assert data["status"] == "healthy"
    assert data["server"] == "codex-gravity-mcp"
    assert len(data["active_tools"]) == 6

