import asyncio
import os
import pytest
from pathlib import Path

from codex_gravity.event_bus import EventBus, Event, EventType
from codex_gravity.sandbox import LockState, SandboxManager, StateLock
from codex_gravity.speculative import (
    AnchorTarget,
    CodexPatch,
    DraftSkeleton,
    SpeculativeDraftingPipeline,
)
from codex_gravity.verifier import DiagnosticReport, RootCauseAnalyzer
from codex_gravity.router import RoutingMode, SmartRouter, TaskRequest
from codex_gravity.mcp_server import create_mcp_server


@pytest.mark.asyncio
async def test_statelock_timeout_zero_attempt():
    """Verifies StateLock acquires free token even when timeout=0."""
    lock = StateLock()
    acquired = await lock.acquire_write_token("agent_a", timeout=0.0)
    assert acquired is True
    assert lock.active_writer == "agent_a"
    await lock.release_write_token("agent_a")


@pytest.mark.asyncio
async def test_statelock_write_transaction_context_manager():
    """Verifies write_transaction context manager automatically releases lock on exception."""
    lock = StateLock()
    with pytest.raises(ValueError, match="simulated failure"):
        async with lock.write_transaction("agent_x"):
            assert lock.active_writer == "agent_x"
            assert lock.state == LockState.CODING
            raise ValueError("simulated failure")

    assert lock.active_writer is None
    assert lock.state == LockState.IDLE


@pytest.mark.asyncio
async def test_sandbox_can_write_enforces_none_writer(tmp_path):
    """Verifies write_file rejects anonymous writes when lock is held by another agent."""
    sm = SandboxManager(repo_root=Path.cwd())
    await sm.initialize()
    try:
        await sm.state_lock.acquire_write_token("codex", state=LockState.CODING)
        # Attempt writing without writer_id should be rejected
        with pytest.raises(PermissionError, match="cannot write"):
            await sm.write_file("test.py", "x = 1", writer_id=None)

        # Writing with correct writer_id succeeds
        path = await sm.write_file("test.py", "x = 1", writer_id="codex")
        assert path.exists()
    finally:
        await sm.state_lock.release_write_token("codex")
        await sm.cleanup()


def test_sandbox_task_id_sanitization():
    """Verifies path traversal strings in task_id are sanitized and contained."""
    sm = SandboxManager(repo_root=Path.cwd(), task_id="../../../tmp/evil_payload")
    # Must be sanitized to contain only safe alphanumeric/hyphen characters
    assert ".." not in sm.task_id
    assert "/" not in sm.task_id
    assert sm.sandbox_dir.parent == sm.sandbox_base


@pytest.mark.asyncio
async def test_sandbox_process_group_kills_orphans():
    """Verifies subprocess timeout kills child processes in the process group."""
    sm = SandboxManager(repo_root=Path.cwd())
    await sm.initialize()
    try:
        res = await sm.run_command("sleep 30", timeout=0.5)
        assert res.success is False
        assert "timed out" in res.stderr
    finally:
        await sm.cleanup()


def test_code_stitching_repeated_placeholders():
    """Verifies multiple anchors sharing identical placeholder are not clobbered."""
    skeleton_code = (
        "def func_a():\n"
        "    # TODO: logic\n"
        "\n"
        "def func_b():\n"
        "    # TODO: logic\n"
    )
    skeleton = DraftSkeleton(
        file_path="multi.py",
        skeleton_code=skeleton_code,
        anchors=[
            AnchorTarget(anchor_id="a1", signature="func_a", placeholder="# TODO: logic"),
            AnchorTarget(anchor_id="a2", signature="func_b", placeholder="# TODO: logic"),
        ],
    )
    patches = [
        CodexPatch(anchor_id="a1", replacement_code="return 'result_a'"),
        CodexPatch(anchor_id="a2", replacement_code="return 'result_b'"),
    ]
    stitched = SpeculativeDraftingPipeline.stitch_code(skeleton, patches)
    assert "return 'result_a'" in stitched
    assert "return 'result_b'" in stitched
    assert "# TODO: logic" not in stitched


def test_code_stitching_mixed_tabs_and_spaces():
    """Verifies files using tabs receive tab-indented patches without TabError."""
    tab_skeleton = (
        "def tabbed_function():\n"
        "\tif True:\n"
        "\t\t# TODO: tab logic\n"
    )
    skeleton = DraftSkeleton(
        file_path="tabs.py",
        skeleton_code=tab_skeleton,
        anchors=[
            AnchorTarget(anchor_id="tab1", signature="tabbed_function", placeholder="# TODO: tab logic"),
        ],
    )
    patches = [
        CodexPatch(anchor_id="tab1", replacement_code="    return 'clean_tabs'"),
    ]
    stitched = SpeculativeDraftingPipeline.stitch_code(skeleton, patches)
    assert "\t\treturn 'clean_tabs'" in stitched
    # Valid Python parsing
    import ast
    ast.parse(stitched)


def test_root_cause_analyzer_module_and_lambda_frames():
    """Verifies RootCauseAnalyzer extracts failed frames with <module> and <lambda>."""
    tb = (
        'Traceback (most recent call last):\n'
        '  File "/workspace/src/app.py", line 12, in <module>\n'
        '    run_pipeline()\n'
        'ZeroDivisionError: division by zero\n'
    )
    diag = RootCauseAnalyzer.analyze(tb, "")
    assert diag.has_error is True
    assert diag.failed_file == "/workspace/src/app.py"
    assert diag.failed_line == 12
    assert diag.target_code_snippet == "run_pipeline()"
    assert diag.error_type == "ZeroDivisionError"


@pytest.mark.asyncio
async def test_router_visual_healing_never_pollutes_python_files(tmp_path):
    """Verifies visual regression CSS fix creates dedicated styles.css rather than corrupting python files."""
    sm = SandboxManager(repo_root=Path.cwd())
    await sm.initialize()
    try:
        await sm.write_file("main.py", "def entrypoint():\n    return 42\n", writer_id="codex")
        router = SmartRouter()
        patcher = router._create_healing_patcher(sm, target_file="main.py")

        from codex_gravity.verifier import ViewportCheckResult
        failed_vp = [
            ViewportCheckResult(
                viewport_name="mobile",
                width=375,
                height=667,
                passed=False,
                suggested_css_fix="display: none;",
                css_selector=".header",
            )
        ]
        await patcher(DiagnosticReport(has_error=False), failed_vp)

        # main.py must NOT contain CSS code
        py_content = await sm.read_file("main.py")
        assert "display: none;" not in py_content
        import ast
        ast.parse(py_content)  # Must remain 100% valid Python syntax

        # styles.css should contain the fix
        assert sm.file_exists("styles.css")
        css_content = await sm.read_file("styles.css")
        assert "display: none;" in css_content
    finally:
        await sm.cleanup()


@pytest.mark.asyncio
async def test_mcp_distill_context_long_multiline_string_safe():
    """Verifies large 50KB multiline log string does not trigger OSError: File name too long."""
    server = create_mcp_server()
    distill_tool = server._tool_manager._tools["antigravity_distill_context"]

    # 50KB of multiline error logs
    huge_log = "\n".join([f"Line {i}: test log entry for verification and diagnostics error" for i in range(1200)])
    result_str = await distill_tool.fn(directory_or_log=huge_log, issue_description="diagnostics error")
    import json
    res = json.loads(result_str)
    assert res["distilled_lines"] > 0
    assert "ast_anchor" in res
