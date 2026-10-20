import pytest
import sys
import tempfile
import subprocess
from pathlib import Path

from codex_gravity.event_bus import EventBus
from codex_gravity.sandbox import SandboxManager
from codex_gravity.verifier import (
    DiagnosticReport,
    RootCauseAnalyzer,
    VerificationEngine,
    VisualRegressionInspector,
)


def test_root_cause_analyzer_python():
    sample_traceback = """
Traceback (most recent call last):
  File "/workspace/tests/test_auth.py", line 42, in test_login
    assert response.status_code == 200
AssertionError: assert 401 == 200
"""
    diag = RootCauseAnalyzer.analyze(sample_traceback, "")
    assert diag.has_error is True
    assert diag.error_type == "AssertionError"
    assert diag.failed_file == "/workspace/tests/test_auth.py"
    assert diag.failed_line == 42
    assert "assert response.status_code == 200" in (diag.target_code_snippet or "")
    assert "Adjust algorithm logic" in diag.recommended_fix


def test_root_cause_analyzer_pytest_summary():
    stdout = """
============================= test session starts ==============================
FAILED tests/test_calc.py::test_addition - AssertionError: 5 != 6
============================== 1 failed in 0.12s ===============================
"""
    diag = RootCauseAnalyzer.analyze(stdout, "")
    assert diag.has_error is True
    assert diag.failed_file == "tests/test_calc.py"
    assert "AssertionError" in diag.error_message or "failed" in diag.error_message


@pytest.mark.asyncio
async def test_visual_multi_viewport_inspector():
    inspector = VisualRegressionInspector()
    results = await inspector.check_viewports("http://localhost:3000")

    assert len(results) == 3
    names = [r.viewport_name for r in results]
    assert "mobile" in names
    assert "tablet" in names
    assert "desktop" in names

    # Verify standard responsive resolutions
    mobile_res = next(r for r in results if r.viewport_name == "mobile")
    assert mobile_res.width == 375
    assert mobile_res.height == 667
    assert mobile_res.passed is True


@pytest.mark.asyncio
async def test_verification_and_auto_healing_loop():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir).resolve()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
        (repo_dir / "README.md").write_text("# Test\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)

        bus = EventBus()
        engine = VerificationEngine(event_bus=bus)
        sandbox = SandboxManager(repo_root=repo_dir, task_id="heal-test")
        await sandbox.initialize()

        try:
            # Initially write buggy code and test
            await sandbox.write_file("calc.py", "def add(a, b): return a - b\n")
            await sandbox.write_file("test_calc.py", "from calc import add\ndef test_add(): assert add(2, 2) == 4\n")

            # Patch callback to heal the buggy function
            async def patcher(diag: DiagnosticReport, viewports):
                # Fix calc.py
                await sandbox.write_file("calc.py", "def add(a, b): return a + b\n")

            # Run auto heal loop with python test
            passed, history = await engine.auto_heal_loop(
                sandbox=sandbox,
                test_command=f"{sys.executable} -B -m pytest -o cache_dir=/tmp test_calc.py",
                patch_callback=patcher,
                max_attempts=3,
            )

            assert passed is True
            assert len(history) == 2
            assert history[0].all_passed is False
            assert history[1].all_passed is True
        finally:
            await sandbox.cleanup()


def test_root_cause_analyzer_multi_frame_traceback():
    """
    Ensures multi-frame tracebacks extract the innermost application frame,
    not test runner / pytest framework frames from the top.
    """
    multi_frame = """
Traceback (most recent call last):
  File "/site-packages/_pytest/runner.py", line 341, in pytest_runtest_protocol
    res = runtestprotocol(item, nextitem=nextitem)
  File "/workspace/src/auth_service.py", line 75, in verify_token
    raise PermissionError("Expired session token")
PermissionError: Expired session token
"""
    diag = RootCauseAnalyzer.analyze(multi_frame, "")
    assert diag.has_error is True
    assert diag.error_type == "PermissionError"
    assert diag.failed_file == "/workspace/src/auth_service.py"
    assert diag.failed_line == 75
    assert "Expired session token" in diag.error_message
    assert 'raise PermissionError("Expired session token")' in (diag.target_code_snippet or "")


def test_root_cause_analyzer_pytest_frame_extraction():
    """
    Ensures pytest's standard failure frame format is parsed for line number and code snippet.
    """
    pytest_stdout = """
__________________________________ test_calc ___________________________________
test_calc.py:4: in test_calc
>       assert add(2, 2) == 4
E       AssertionError: assert -2 == 4
=========================== short test summary info ============================
FAILED test_calc.py::test_calc - AssertionError: assert -2 == 4
"""
    diag = RootCauseAnalyzer.analyze(pytest_stdout, "")
    assert diag.has_error is True
    assert diag.error_type == "AssertionError"
    assert diag.failed_file == "test_calc.py"
    assert diag.failed_line == 4
    assert diag.target_code_snippet == "assert add(2, 2) == 4"


def test_root_cause_analyzer_jest_and_ts():
    ts_err = "src/payment.ts:25:9 - error TS2339: Property 'currency' does not exist on type 'PaymentRequest'."
    diag = RootCauseAnalyzer.analyze(ts_err, "")
    assert diag.has_error is True
    assert diag.failed_file == "src/payment.ts"
    assert diag.failed_line == 25
    assert diag.error_type == "TS2339"
    assert "TypeScript type check failed" in diag.recommended_fix


@pytest.mark.asyncio
async def test_visual_viewport_overflow_detection():
    inspector = VisualRegressionInspector()
    results = await inspector.check_viewports("http://localhost:3000/overflow")

    mobile_res = next(r for r in results if r.viewport_name == "mobile")
    assert mobile_res.passed is False
    assert mobile_res.regression_type == "HorizontalOverflow"
    assert "flex-direction: column" in (mobile_res.suggested_css_fix or "")

    desktop_res = next(r for r in results if r.viewport_name == "desktop")
    assert desktop_res.passed is True
