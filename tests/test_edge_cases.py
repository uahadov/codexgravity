import asyncio
import sys
import tempfile
import subprocess
from pathlib import Path
import pytest

from codex_gravity.event_bus import EventBus, Event, EventType
from codex_gravity.sandbox import SandboxManager
from codex_gravity.speculative import (
    AnchorTarget,
    CodexPatch,
    DraftSkeleton,
    SpeculativeDraftingPipeline,
)
from codex_gravity.verifier import (
    DiagnosticReport,
    RootCauseAnalyzer,
    VerificationEngine,
    VisualRegressionInspector,
    ViewportCheckResult,
)
from codex_gravity.router import SmartRouter, TaskRequest, RoutingMode
from codex_gravity.mcp_server import create_mcp_server


@pytest.fixture
def temp_git_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir).resolve()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
        (repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)
        yield repo_dir


@pytest.mark.asyncio
async def test_sandbox_deeply_nested_file_and_read_error(temp_git_repo):
    sandbox = SandboxManager(repo_root=temp_git_repo, task_id="deep-test")
    await sandbox.initialize()
    try:
        # Deeply nested path
        nested = "a/b/c/d/e/deep.py"
        await sandbox.write_file(nested, "VALUE = 'deep'\n")
        assert sandbox.file_exists(nested) is True
        assert await sandbox.read_file(nested) == "VALUE = 'deep'\n"

        # Reading non-existent file
        with pytest.raises(FileNotFoundError):
            await sandbox.read_file("non_existent_file.py")
    finally:
        await sandbox.cleanup()


@pytest.mark.asyncio
async def test_sandbox_command_timeout(temp_git_repo):
    sandbox = SandboxManager(repo_root=temp_git_repo, task_id="timeout-test")
    await sandbox.initialize()
    try:
        # Command that sleeps longer than timeout
        res = await sandbox.run_command("sleep 5", timeout=0.2)
        assert res.success is False
        assert res.exit_code == -1
        assert "timed out" in res.stderr
    finally:
        await sandbox.cleanup()


@pytest.mark.asyncio
async def test_event_bus_fault_tolerant_handlers():
    bus = EventBus()

    def crashing_handler(e):
        raise RuntimeError("Handler failed on purpose")

    received = []
    def ok_handler(e):
        received.append(e)

    bus.subscribe(EventType.AGENT_DIALOGUE, crashing_handler)
    bus.subscribe(EventType.AGENT_DIALOGUE, ok_handler)

    # Crashing handler must not crash publish or prevent ok_handler
    event = await bus.send_dialogue("codex", "antigravity", "Test resilient event")
    assert len(received) == 1
    assert received[0].message == "Test resilient event"


@pytest.mark.asyncio
async def test_speculative_typescript_pipeline():
    pipeline = SpeculativeDraftingPipeline()
    skeleton = await pipeline.generate_draft(
        task_description="Build TypeScript payment calculator",
        file_path="src/payment.ts",
        language="typescript",
    )
    assert skeleton.language == "typescript"
    assert "export async function executeOperation" in skeleton.skeleton_code
    assert len(skeleton.anchors) >= 1

    final_code, patches, arbitrage = await pipeline.complete_draft(skeleton)
    assert "// TODO(codex_core_logic)" not in final_code
    assert "Codex verified logic implementation" in final_code
    assert arbitrage.dollars_saved > 0


def test_root_cause_analyzer_various_python_errors():
    err1 = "ModuleNotFoundError: No module named 'nonexistent_module'"
    diag1 = RootCauseAnalyzer.analyze("", err1)
    assert diag1.has_error is True
    assert diag1.error_type == "ModuleNotFoundError"
    assert "Missing dependency" in diag1.recommended_fix

    err2 = "TypeError: unsupported operand type(s) for +: 'int' and 'str'"
    diag2 = RootCauseAnalyzer.analyze("", err2)
    assert diag2.has_error is True
    assert diag2.error_type == "TypeError"
    assert "Type mismatch" in diag2.recommended_fix


@pytest.mark.asyncio
async def test_auto_heal_loop_max_attempts_exceeded():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir).resolve()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
        (repo_dir / "README.md").write_text("# Test\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)

        engine = VerificationEngine()
        sandbox = SandboxManager(repo_root=repo_dir, task_id="unhealable")
        await sandbox.initialize()

        try:
            # Patch callback that doesn't fix the failure
            async def failing_patcher(diag, viewports):
                pass

            passed, history = await engine.auto_heal_loop(
                sandbox=sandbox,
                test_command="python3 -c 'exit(1)'",
                patch_callback=failing_patcher,
                max_attempts=2,
            )
            assert passed is False
            assert len(history) == 2
            assert history[0].all_passed is False
            assert history[1].all_passed is False
        finally:
            await sandbox.cleanup()


@pytest.mark.asyncio
async def test_mcp_media_inspect_nonexistent():
    server = create_mcp_server()
    res = await server.call_tool("antigravity_inspect_media", {"file_path": "/path/to/missing.mp4", "focus_query": "test"})
    assert "File does not exist" in res.content[0].text


@pytest.mark.asyncio
async def test_router_no_auto_merge(temp_git_repo):
    router = SmartRouter()
    req = TaskRequest(
        prompt="Write helper function",
        target_file="src/helper.py",
        auto_merge=False,
    )
    summary = await router.execute_task(req, repo_root=temp_git_repo)
    assert summary.success is True
    assert summary.merged is False
    # Main repo should NOT have the file when auto_merge is False
    assert not (temp_git_repo / "src" / "helper.py").exists()
