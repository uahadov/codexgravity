"""
Unit and integration tests verifying the fixes for:
1. cli.py auto_merge defaulting to False (explicit flag or approval required)
2. sandbox.py active branch restoration and clean stash handling
3. mcp_server.py studio diff reading before cleanup, portability, accurate metrics, and no PYTEST_CURRENT_TEST sniffing
4. router.py & verifier.py stack trace parsing and source-aware self-healing
5. speculative.py real LLM generation & transparent mock mode labeling
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codex_gravity.cli import main as cli_main
from codex_gravity.event_bus import EventBus
from codex_gravity.mcp_server import (
    STUDIO_HEADER,
    create_mcp_server,
    studio_token,
)
from codex_gravity.router import ExecutionSummary, RoutingMode, SmartRouter, TaskRequest
from codex_gravity.sandbox import SandboxManager
from codex_gravity.speculative import (
    AnchorTarget,
    CodexPatch,
    DraftSkeleton,
    SpeculativeDraftingPipeline,
    TokenArbitrageResult,
)
from codex_gravity.verifier import DiagnosticReport, RootCauseAnalyzer


# ---------------------------------------------------------------------------
# 1. CLI & TaskRequest auto_merge Default Tests
# ---------------------------------------------------------------------------

def test_task_request_auto_merge_defaults_to_false():
    req = TaskRequest(
        prompt="Test prompt",
        target_file="src/test.py",
    )
    assert req.auto_merge is False


def test_cli_auto_merge_flag_parsing():
    # Verify CLI with --auto-merge flag sets auto_merge=True
    with patch("sys.argv", ["codex-gravity", "Fix bug", "-f", "src/mod.py", "--auto-merge"]):
        with patch("codex_gravity.cli.SmartRouter.execute_task", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = ExecutionSummary(
                task_id="t1",
                mode=RoutingMode.SMART_ADAPTIVE,
                target_file="src/mod.py",
                success=True,
                arbitrage=TokenArbitrageResult.calculate(100, 10, 10, 10.0),
                merged=True,
            )
            cli_main()
            call_args = mock_exec.call_args[0][0]
            assert isinstance(call_args, TaskRequest)
            assert call_args.auto_merge is True


def test_cli_defaults_to_no_merge_without_flag():
    # Non-interactive stdin (e.g. CI or pipe) without --auto-merge must default to False
    with patch("sys.argv", ["codex-gravity", "Fix bug", "-f", "src/mod.py"]):
        with patch("sys.stdin.isatty", return_value=False):
            with patch("codex_gravity.cli.SmartRouter.execute_task", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = ExecutionSummary(
                    task_id="t2",
                    mode=RoutingMode.SMART_ADAPTIVE,
                    target_file="src/mod.py",
                    success=True,
                    arbitrage=TokenArbitrageResult.calculate(100, 10, 10, 10.0),
                    merged=False,
                )
                cli_main()
                call_args = mock_exec.call_args[0][0]
                assert call_args.auto_merge is False


def test_cli_interactive_user_approval():
    # Interactive stdin where user enters 'y'
    with patch("sys.argv", ["codex-gravity", "Fix bug", "-f", "src/mod.py"]):
        with patch("sys.stdin.isatty", return_value=True):
            with patch("builtins.input", return_value="y"):
                with patch("codex_gravity.cli.SmartRouter.execute_task", new_callable=AsyncMock) as mock_exec:
                    mock_exec.return_value = ExecutionSummary(
                        task_id="t3",
                        mode=RoutingMode.SMART_ADAPTIVE,
                        target_file="src/mod.py",
                        success=True,
                        arbitrage=TokenArbitrageResult.calculate(100, 10, 10, 10.0),
                        merged=True,
                    )
                    cli_main()
                    call_args = mock_exec.call_args[0][0]
                    assert call_args.auto_merge is True


# ---------------------------------------------------------------------------
# 2. Sandbox Active Branch Restoration & Clean Stash Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sandbox_merge_restores_active_branch_and_stashes_dirty_work(temp_git_repo):
    """
    Developer is working on 'feature-x' with dirty uncommitted changes.
    Sandbox must merge into 'main', restore 'feature-x', and restore the uncommitted changes.
    """
    # 1. Developer switches to feature branch and creates uncommitted dirty change
    subprocess.run(["git", "checkout", "-b", "feature-x"], cwd=temp_git_repo, check=True, capture_output=True)
    dirty_file = temp_git_repo / "uncommitted_work.txt"
    dirty_file.write_text("WIP by developer\n", encoding="utf-8")

    sandbox = SandboxManager(repo_root=temp_git_repo, task_id="branch-preserve-test")
    await sandbox.initialize()
    try:
        # Sandbox writes and commits a new module
        await sandbox.write_file("src/service.py", "def service():\n    return 'ok'\n")
        await sandbox.commit_changes("add service.py")

        # Merge to main
        merged = await sandbox.merge_to_main(target_branch="main")
        assert merged is True

        # Developer's active branch in repo_root MUST STILL BE 'feature-x'
        current_branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert current_branch == "feature-x"

        # Developer's uncommitted work must be restored
        assert dirty_file.exists()
        assert dirty_file.read_text(encoding="utf-8") == "WIP by developer\n"

        # The merged file must exist in 'main' branch
        file_check = subprocess.run(
            ["git", "rev-parse", "main:src/service.py"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
        )
        assert file_check.returncode == 0
    finally:
        await sandbox.cleanup()


@pytest.mark.asyncio
async def test_sandbox_merge_preserves_detached_head(temp_git_repo):
    """
    If developer is on a detached HEAD commit, sandbox restores the detached HEAD after merge.
    """
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    subprocess.run(["git", "checkout", head_sha], cwd=temp_git_repo, check=True, capture_output=True)

    sandbox = SandboxManager(repo_root=temp_git_repo, task_id="detached-head-test")
    await sandbox.initialize()
    try:
        await sandbox.write_file("detached_test.txt", "content\n")
        await sandbox.commit_changes("add detached test")

        merged = await sandbox.merge_to_main()
        assert merged is True

        # Active ref should still be the detached HEAD commit
        post_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert post_sha == head_sha
    finally:
        await sandbox.cleanup()


# ---------------------------------------------------------------------------
# 3. MCP Server Studio Diff, Metrics, and Portability Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_distill_context_accurate_metrics_without_inflation():
    """
    Verifies metric inflators max(70.0, ...) and max(raw_lines, 100) are removed.
    """
    server = create_mcp_server()

    # Small 10-line log
    log_text = "\n".join([f"line {i}" for i in range(10)])
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        f.write(log_text)
        log_path = f.name

    try:
        res = await server.call_tool(
            "antigravity_distill_context",
            {"directory_or_log": log_path, "issue_description": "line 5"},
        )
        data = json.loads(res.content[0].text)
        # raw_lines_analyzed should be exactly 10, NOT inflated to 100
        assert data["raw_lines_analyzed"] == 10
        # token_reduction_pct should be accurate (e.g. 10 - 7 / 10 = 30%), NOT forced to >= 70.0
        assert data["token_reduction_pct"] <= 70.0
    finally:
        Path(log_path).unlink(missing_ok=True)


def test_mcp_server_no_pytest_current_test_sniffing():
    """
    Inspects production code of codex_gravity to ensure PYTEST_CURRENT_TEST is never sniffed.
    """
    mcp_code = Path("codex_gravity/mcp_server.py").read_text(encoding="utf-8")
    assert "PYTEST_CURRENT_TEST" not in mcp_code


def test_mcp_server_portable_paths():
    """
    Ensures no hardcoded /home/ua paths exist in mcp_server.py.
    """
    mcp_code = Path("codex_gravity/mcp_server.py").read_text(encoding="utf-8")
    assert "/home/ua" not in mcp_code
    assert "ANTIGRAVITY_HOME" in mcp_code


@pytest.mark.asyncio
async def test_studio_run_reads_diff_from_sandbox_without_authservice_fallback(temp_git_repo):
    """
    Tests that studio run diff endpoint returns actual generated code and does not fall back to AuthService.
    The route is authenticated: an unauthenticated call must be refused without running anything.
    """
    server = create_mcp_server()
    app = server.streamable_http_app()

    from starlette.testclient import TestClient
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        payload = {
            "prompt": "Create mathematical fibonacci calculator",
            "target_file": "src/fib.py",
            "mode": "eco",
        }
        denied = client.post("/api/studio/run", json=payload)
        assert denied.status_code == 403
        assert "No command was executed" in denied.json()["error"]

        resp = client.post("/api/studio/run", json=payload, headers={STUDIO_HEADER: studio_token()})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["verified"] is True
        # The generated code must contain actual code generated by the task, NOT the hardcoded AuthService
        code = data["code"]
        assert "class AuthService" not in code
        assert "execute_operation" in code or "fib" in code or len(code) > 0


# ---------------------------------------------------------------------------
# 4. RootCauseAnalyzer & Autonomous Self-Healing Tests
# ---------------------------------------------------------------------------

def test_root_cause_analyzer_extracts_failing_function_from_assert():
    """
    Tests that when an assertion fails at the top of a test function,
    the analyzer extracts the target function being called (e.g. 'add').
    """
    log = """
tests/test_calc.py:4: in test_calc
>       assert add(2, 2) == 4
E       AssertionError: assert -2 == 4
E        +  where -2 = add(2, 2)
"""
    diag = RootCauseAnalyzer.analyze(log, "")
    assert diag.has_error is True
    assert diag.error_type == "AssertionError"
    assert diag.failing_function == "add"
    assert diag.test_file == "tests/test_calc.py"
    assert diag.target_code_snippet == "assert add(2, 2) == 4"


def test_root_cause_analyzer_extracts_innermost_source_frame():
    """
    Tests that multi-frame tracebacks separate test frames from source frames.
    """
    log = """
Traceback (most recent call last):
  File "tests/test_calc.py", line 10, in test_calc
    result = compute(5)
  File "src/calc.py", line 22, in compute
    return 10 / 0
ZeroDivisionError: division by zero
"""
    diag = RootCauseAnalyzer.analyze(log, "")
    assert diag.has_error is True
    assert diag.error_type == "ZeroDivisionError"
    assert diag.source_file == "src/calc.py"
    assert diag.source_line == 22
    assert diag.source_code_snippet == "return 10 / 0"
    assert diag.failing_function == "compute"


@pytest.mark.asyncio
async def test_no_heuristic_self_healing_without_opt_in(temp_git_repo):
    """
    Default contract: without an LLM answer the built-in healer refuses to flip
    operators, so the failing run stays failed and nothing reaches main.
    """
    router = SmartRouter()

    test_file = temp_git_repo / "test_math.py"
    test_file.write_text(
        "from src.calc import add\n"
        "def test_add():\n"
        "    assert add(2, 2) == 4\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "test_math.py"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add math test"], cwd=temp_git_repo, check=True, capture_output=True)

    def buggy_flash(task, file_path, lang, ctx):
        return DraftSkeleton(
            file_path=file_path,
            language=lang,
            skeleton_code="def add(a, b):\n    # TODO\n    return a - b\n",
            anchors=[AnchorTarget(signature="def add(a, b):", placeholder="# TODO")],
        )

    router.speculative_pipeline.flash_generator = buggy_flash

    async def noop_completer(skeleton):
        return [CodexPatch(anchor_id=skeleton.anchors[0].anchor_id, replacement_code="# buggy draft\n")]

    router.speculative_pipeline.codex_completer = noop_completer

    req = TaskRequest(
        prompt="Implement add function",
        target_file="src/calc.py",
        mode=RoutingMode.SMART_ADAPTIVE,
        test_command=f"{sys.executable} -B -m pytest -o cache_dir=/tmp test_math.py",
        auto_merge=True,
    )

    summary = await router.execute_task(req, repo_root=temp_git_repo)
    assert summary.verified is True
    assert summary.success is False
    assert summary.merged is False
    assert summary.heuristic_repair_used is False
    assert not (temp_git_repo / "src" / "calc.py").exists()
    assert "Heuristic operator flipping is disabled" in summary.walkie_talkie_transcript


@pytest.mark.asyncio
async def test_opt_in_heuristic_self_healing_is_flagged(temp_git_repo):
    """
    With explicit opt-in the semantics-blind repair still runs, but the summary
    flags it so a flipped operator is never mistaken for a verified fix.
    """
    router = SmartRouter()

    test_file = temp_git_repo / "test_math.py"
    test_file.write_text(
        "from src.calc import add\n"
        "def test_add():\n"
        "    assert add(2, 2) == 4\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "test_math.py"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add math test"], cwd=temp_git_repo, check=True, capture_output=True)

    def buggy_flash(task, file_path, lang, ctx):
        return DraftSkeleton(
            file_path=file_path,
            language=lang,
            skeleton_code="def add(a, b):\n    # TODO\n    return a - b\n",
            anchors=[AnchorTarget(signature="def add(a, b):", placeholder="# TODO")],
        )

    router.speculative_pipeline.flash_generator = buggy_flash

    async def noop_completer(skeleton):
        return [CodexPatch(anchor_id=skeleton.anchors[0].anchor_id, replacement_code="# buggy draft\n")]

    router.speculative_pipeline.codex_completer = noop_completer

    req = TaskRequest(
        prompt="Implement add function",
        target_file="src/calc.py",
        mode=RoutingMode.SMART_ADAPTIVE,
        test_command=f"{sys.executable} -B -m pytest -o cache_dir=/tmp test_math.py",
        auto_merge=True,
        allow_heuristic_repair=True,
    )

    summary = await router.execute_task(req, repo_root=temp_git_repo)
    assert summary.success is True
    assert summary.heuristic_repair_used is True
    assert summary.merged is True
    assert "return a + b" in (temp_git_repo / "src" / "calc.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 5. Speculative Pipeline Mock vs Real LLM Execution Labeling Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_speculative_pipeline_transparent_mock_labeling():
    """
    When no API keys are present, pipeline explicitly labels output as mock/template mode.
    """
    with patch.dict(os.environ, {}, clear=True):
        bus = EventBus()
        pipeline = SpeculativeDraftingPipeline(event_bus=bus)

        skeleton = await pipeline.generate_draft("Implement login", "src/auth.py")
        assert skeleton.is_mock is True
        assert "[MOCK GENERATION" in skeleton.skeleton_code
        assert skeleton.provider == "mock-template-generator"

        final_code, patches, arbitrage = await pipeline.complete_draft(skeleton)
        assert arbitrage.is_mock is True
        assert arbitrage.provider == "mock-template"
        assert "[MOCK MODE" in final_code

        # Transcript must transparently declare mock mode
        history = bus.get_history()
        mock_messages = [e for e in history if "[MOCK MODE]" in e.message]
        assert len(mock_messages) >= 1


@pytest.mark.asyncio
async def test_speculative_pipeline_real_gemini_execution_flow():
    """
    When GEMINI_API_KEY is present, pipeline uses real Gemini client and marks is_mock=False.
    """
    mock_gemini_client = MagicMock()
    mock_gemini_resp = MagicMock()
    mock_gemini_resp.text = '"""\nLive Gemini\n"""\ndef handler():\n    # TODO(codex_core_logic): implement business logic\n'
    mock_gemini_client.models.generate_content.return_value = mock_gemini_resp

    with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-xyz"}):
        with patch("codex_gravity.speculative.get_gemini_client", return_value=mock_gemini_client):
            pipeline = SpeculativeDraftingPipeline()
            skeleton = await pipeline.generate_draft("Live task", "src/live.py")

            assert skeleton.is_mock is False
            assert skeleton.provider == "google-gemini-live"
            assert "Live Gemini" in skeleton.skeleton_code


# ---------------------------------------------------------------------------
# 6. Advanced Edge Cases & Deep Robustness Tests
# ---------------------------------------------------------------------------

def test_root_cause_analyzer_multi_frame_pytest_traceback_preserves_all_frames():
    """
    Verifies that multi-frame pytest tracebacks do not skip intermediate frames
    and accurately identify source vs test frames.
    """
    log = """
tests/test_runner.py:10: in test_pipeline
    call_stage_one()
tests/test_runner.py:20: in call_stage_one
    process_data()
src/pipeline.py:35: in process_data
>   return 100 / 0
E   ZeroDivisionError: division by zero
"""
    diag = RootCauseAnalyzer.analyze(log, "")
    assert diag.has_error is True
    assert len(diag.stack_frames) == 3
    assert diag.stack_frames[0].file_path == "tests/test_runner.py"
    assert diag.stack_frames[1].file_path == "tests/test_runner.py"
    assert diag.stack_frames[2].file_path == "src/pipeline.py"
    assert diag.source_file == "src/pipeline.py"
    assert diag.source_line == 35
    assert diag.source_code_snippet == "return 100 / 0"
    assert diag.failing_function == "process_data"


def test_root_cause_analyzer_method_call_on_instance():
    """
    Verifies that assertions calling instance methods (e.g. calc.add)
    correctly extract the method name rather than failing or returning None.
    """
    log = """
tests/test_calc.py:12: in test_calc
>       assert calculator.add(2, 2) == 4
E       AssertionError: assert -2 == 4
E        +  where -2 = calculator.add(2, 2)
"""
    diag = RootCauseAnalyzer.analyze(log, "")
    assert diag.has_error is True
    assert diag.failing_function == "add"


def test_code_stitching_multiline_target_snippet():
    """
    Verifies that multiline target snippets in AnchorTarget are properly
    located and replaced without silently dropping the patch.
    """
    code = (
        "def compute():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    return a + b\n"
    )
    anchor = AnchorTarget(
        anchor_id="anchor-multiline",
        signature="compute",
        placeholder="",
        target_snippet="a = 1\n    b = 2",
    )
    skeleton = DraftSkeleton(
        file_path="src/compute.py",
        skeleton_code=code,
        anchors=[anchor],
    )
    patches = [
        CodexPatch(
            anchor_id="anchor-multiline",
            replacement_code="a = 10\nb = 20",
        )
    ]
    stitched = SpeculativeDraftingPipeline.stitch_code(skeleton, patches)
    assert "a = 10" in stitched
    assert "b = 20" in stitched
    assert "a = 1\n" not in stitched


@pytest.mark.asyncio
async def test_ast_self_healing_async_def_and_blank_lines(temp_git_repo):
    """
    Verifies that SmartRouter._create_healing_patcher uses AST to heal
    async def functions containing blank lines and docstrings.
    """
    router = SmartRouter()
    sandbox = SandboxManager(repo_root=temp_git_repo, task_id="ast-async-heal")
    await sandbox.initialize()
    try:
        async_code = (
            "class AsyncService:\n"
            "    async def calculate(self, a: int, b: int) -> int:\n"
            '        """Performs calculation."""\n'
            "\n"
            "        # intermediate step\n"
            "        x = a\n"
            "\n"
            "        return x - b\n"
        )
        await sandbox.write_file("src/service.py", async_code, writer_id="codex")

        diag = DiagnosticReport(
            has_error=True,
            error_type="AssertionError",
            failing_function="calculate",
            target_code_snippet="assert await s.calculate(2, 2) == 4",
        )

        patcher = router._create_healing_patcher(sandbox, "src/service.py", allow_heuristic_repair=True)
        await patcher(diag, [])

        healed_content = await sandbox.read_file("src/service.py")
        assert "return x + b" in healed_content
        assert "async def calculate" in healed_content
    finally:
        await sandbox.cleanup()


@pytest.mark.asyncio
async def test_router_eco_mode_and_circuit_breaker_propagate_is_mock(temp_git_repo):
    """
    Verifies that running in eco mode or under circuit breaker properly propagates
    is_mock=True when no API keys are present.
    """
    router = SmartRouter()
    req = TaskRequest(
        prompt="Eco task",
        target_file="src/eco.py",
        mode=RoutingMode.ECO,
        auto_merge=False,
    )
    summary = await router.execute_task(req, repo_root=temp_git_repo)
    assert summary.is_mock is True
    assert summary.arbitrage.is_mock is True

    # Circuit breaker failover test
    router_cb = SmartRouter()
    router_cb._codex_failure_simulator = True
    req_cb = TaskRequest(
        prompt="CB task",
        target_file="src/cb.py",
        mode=RoutingMode.SMART_ADAPTIVE,
        auto_merge=False,
    )
    summary_cb = await router_cb.execute_task(req_cb, repo_root=temp_git_repo)
    assert summary_cb.circuit_breaker_triggered is True
    assert summary_cb.is_mock is True
    assert summary_cb.arbitrage.is_mock is True


@pytest.mark.asyncio
async def test_sandbox_initialize_adds_exclude_pattern(temp_git_repo):
    """
    Verifies that SandboxManager.initialize ensures .shadow_sandbox/ is in .git/info/exclude.
    """
    exclude_file = temp_git_repo / ".git" / "info" / "exclude"
    sandbox = SandboxManager(repo_root=temp_git_repo, task_id="exclude-test")
    await sandbox.initialize()
    try:
        assert exclude_file.exists()
        assert ".shadow_sandbox" in exclude_file.read_text(encoding="utf-8")
    finally:
        await sandbox.cleanup()


@pytest.mark.asyncio
async def test_live_llm_self_healing_synthesis(temp_git_repo):
    """
    Verifies that when OPENAI_API_KEY is present, self-healing invokes the LLM.
    """
    router = SmartRouter()
    sandbox = SandboxManager(repo_root=temp_git_repo, task_id="llm-heal-test")
    await sandbox.initialize()
    try:
        await sandbox.write_file("src/calc.py", "def add(a, b):\n    return a - b\n", writer_id="codex")

        diag = DiagnosticReport(
            has_error=True,
            error_type="AssertionError",
            failing_function="add",
            target_code_snippet="assert add(2, 2) == 4",
        )

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}):
            with patch("codex_gravity.router.call_openai_chat", new_callable=AsyncMock) as mock_openai:
                mock_openai.return_value = "def add(a, b):\n    return a + b\n"
                patcher = router._create_healing_patcher(sandbox, "src/calc.py")
                await patcher(diag, [])

                healed = await sandbox.read_file("src/calc.py")
                assert "return a + b" in healed
                assert mock_openai.called
    finally:
        await sandbox.cleanup()

