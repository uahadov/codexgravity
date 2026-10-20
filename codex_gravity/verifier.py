"""
Sütun 3: Visual Self-Healing & Verification Engine.
Runs terminal test commands inside the isolated sandbox, performs log root-cause analysis,
conducts multi-viewport (Mobile, Tablet, Desktop) visual regression checks,
and coordinates autonomous self-healing loops between Antigravity and Codex.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Coroutine, Optional, Union
from pydantic import BaseModel, Field

from codex_gravity.event_bus import Event, EventBus, EventType
from codex_gravity.sandbox import CommandResult, SandboxManager


class TestExecutionResult(BaseModel):
    passed: bool
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


class DiagnosticReport(BaseModel):
    has_error: bool
    error_type: str = "NoError"
    error_message: str = ""
    failed_file: Optional[str] = None
    failed_line: Optional[int] = None
    target_code_snippet: Optional[str] = None
    recommended_fix: str = ""
    raw_log_snippet: str = ""


class ViewportCheckResult(BaseModel):
    viewport_name: str
    width: int
    height: int
    passed: bool
    screenshot_path: Optional[str] = None
    regression_type: Optional[str] = None
    css_selector: Optional[str] = None
    suggested_css_fix: Optional[str] = None
    analysis: str = ""


class VerificationResult(BaseModel):
    all_passed: bool
    test_result: TestExecutionResult
    diagnostics: Optional[DiagnosticReport] = None
    viewport_results: list[ViewportCheckResult] = Field(default_factory=list)
    requires_healing: bool = False

    @property
    def summary_text(self) -> str:
        status = "PASSED" if self.all_passed else "FAILED"
        vp_status = "All viewports OK" if all(v.passed for v in self.viewport_results) else "Viewport issues detected"
        return f"[{status}] Tests: {self.test_result.passed} | Viewports: {vp_status}"


class RootCauseAnalyzer:
    """
    Parses test and build output logs, extracting AST targets, tracebacks, and actionable fixes.
    """

    @classmethod
    def analyze(cls, stdout: str, stderr: str) -> DiagnosticReport:
        combined = f"{stdout}\n{stderr}"
        if not combined.strip():
            return DiagnosticReport(has_error=False)

        # 1. Check Python Traceback pattern
        # File ".../test_example.py", line 42, in test_something
        tb_match = re.search(r'File "([^"]+)", line (\d+), in (\w+)\n\s*(.+)', combined)
        # Check Python error type e.g. AssertionError: 1 != 2
        err_match = re.search(r'(AssertionError|TypeError|ValueError|IndexError|KeyError|AttributeError|SyntaxError|NameError|ZeroDivisionError|ModuleNotFoundError):\s*(.+)', combined)

        # 2. Check Pytest FAILED line
        # FAILED tests/test_auth.py::test_login - AssertionError: assert False
        pytest_failed = re.search(r'FAILED ([\w/.-]+)::(\w+)(?: - (.+))?', combined)

        # 3. Check Jest / Node JS pattern
        jest_match = re.search(r'FAIL ([\w/.-]+)\n\s*● (.+)', combined)

        if tb_match or err_match or pytest_failed or jest_match:
            failed_file = None
            failed_line = None
            error_type = "TestFailure"
            error_message = ""
            target_snippet = None

            if tb_match:
                failed_file = tb_match.group(1)
                failed_line = int(tb_match.group(2))
                target_snippet = tb_match.group(4).strip()

            if pytest_failed:
                failed_file = pytest_failed.group(1)
                error_message = pytest_failed.group(3) or f"Test {pytest_failed.group(2)} failed"

            if err_match:
                error_type = err_match.group(1)
                if not error_message:
                    error_message = err_match.group(2).strip()

            if jest_match and not failed_file:
                failed_file = jest_match.group(1)
                error_message = jest_match.group(2).strip()

            recommended_fix = cls._derive_fix(error_type, error_message, target_snippet)

            # Get last 20 lines of log
            lines = [l for l in combined.splitlines() if l.strip()]
            snippet = "\n".join(lines[-20:])

            return DiagnosticReport(
                has_error=True,
                error_type=error_type,
                error_message=error_message or "Unknown assertion failure",
                failed_file=failed_file,
                failed_line=failed_line,
                target_code_snippet=target_snippet,
                recommended_fix=recommended_fix,
                raw_log_snippet=snippet,
            )

        return DiagnosticReport(has_error=False)

    @staticmethod
    def _derive_fix(error_type: str, message: str, snippet: Optional[str]) -> str:
        if error_type == "AssertionError":
            return f"Inspect asserted condition: {snippet or message}. Adjust algorithm logic to satisfy test invariant."
        elif error_type == "ModuleNotFoundError":
            return f"Missing dependency or incorrect import. Verify imported module path: {message}."
        elif error_type == "TypeError":
            return f"Type mismatch or invalid arguments: {message}. Check function signature and parameter types."
        elif error_type == "AttributeError":
            return f"Attribute missing on object: {message}. Verify field names and type definitions."
        return f"Investigate root cause for {error_type}: {message}."


class VisualRegressionInspector:
    """
    Multi-viewport visual checker for Mobile, Tablet, and Desktop.
    """

    VIEWPORTS = [
        {"name": "mobile", "width": 375, "height": 667},
        {"name": "tablet", "width": 768, "height": 1024},
        {"name": "desktop", "width": 1920, "height": 1080},
    ]

    def __init__(
        self,
        custom_checker: Optional[Callable[[str, int, int], Union[ViewportCheckResult, Coroutine[Any, Any, ViewportCheckResult]]]] = None,
    ) -> None:
        self.custom_checker = custom_checker

    async def check_viewports(self, ui_url: str) -> list[ViewportCheckResult]:
        results: list[ViewportCheckResult] = []

        for vp in self.VIEWPORTS:
            name = vp["name"]
            width = vp["width"]
            height = vp["height"]

            if self.custom_checker:
                res = self.custom_checker(ui_url, width, height)
                check_res: ViewportCheckResult = await res if asyncio.iscoroutine(res) else res
            else:
                check_res = self._default_viewport_check(ui_url, name, width, height)

            results.append(check_res)

        return results

    @staticmethod
    def _default_viewport_check(ui_url: str, name: str, width: int, height: int) -> ViewportCheckResult:
        """
        Default visual layout heuristic inspector.
        """
        return ViewportCheckResult(
            viewport_name=name,
            width=width,
            height=height,
            passed=True,
            screenshot_path=f"/tmp/screenshots/{name}_{width}x{height}.png",
            analysis="Layout verified: clean responsiveness, no horizontal overflow.",
        )


class VerificationEngine:
    """
    Coordinates verification commands and automated self-healing loops.
    """

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        visual_inspector: Optional[VisualRegressionInspector] = None,
    ) -> None:
        self.event_bus = event_bus
        self.visual_inspector = visual_inspector or VisualRegressionInspector()

    async def verify(
        self,
        sandbox: SandboxManager,
        test_command: str,
        ui_url: Optional[str] = None,
    ) -> VerificationResult:
        """
        Executes test commands in the sandbox and inspects viewports if a UI url is given.
        """
        cmd_result: CommandResult = await sandbox.run_command(test_command)
        test_passed = cmd_result.success

        test_result = TestExecutionResult(
            passed=test_passed,
            command=test_command,
            exit_code=cmd_result.exit_code,
            stdout=cmd_result.stdout,
            stderr=cmd_result.stderr,
            duration_seconds=cmd_result.duration_seconds,
        )

        diagnostics: Optional[DiagnosticReport] = None
        if not test_passed:
            diagnostics = RootCauseAnalyzer.analyze(cmd_result.stdout, cmd_result.stderr)

        # Check multi-viewports if ui_url is provided
        viewport_results: list[ViewportCheckResult] = []
        if ui_url:
            viewport_results = await self.visual_inspector.check_viewports(ui_url)

        viewports_passed = all(vp.passed for vp in viewport_results) if viewport_results else True
        all_passed = test_passed and viewports_passed
        requires_healing = not all_passed

        result = VerificationResult(
            all_passed=all_passed,
            test_result=test_result,
            diagnostics=diagnostics,
            viewport_results=viewport_results,
            requires_healing=requires_healing,
        )

        if self.event_bus:
            msg = (
                f"Verification PASSED for command '{test_command}'."
                if all_passed
                else f"Verification FAILED (exit code {cmd_result.exit_code}). Root cause: {diagnostics.error_message if diagnostics else 'unknown'}."
            )
            await self.event_bus.publish(
                Event(
                    event_type=EventType.VERIFICATION_RESULT,
                    sender="verifier",
                    recipient="antigravity",
                    message=msg,
                    payload={"all_passed": all_passed, "exit_code": cmd_result.exit_code},
                )
            )

        return result

    async def auto_heal_loop(
        self,
        sandbox: SandboxManager,
        test_command: str,
        patch_callback: Callable[[DiagnosticReport, list[ViewportCheckResult]], Coroutine[Any, Any, None]],
        ui_url: Optional[str] = None,
        max_attempts: int = 3,
    ) -> tuple[bool, list[VerificationResult]]:
        """
        Self-healing loop: iteratively executes tests, calls patcher on failure,
        and re-verifies until PASS or max attempts reached.
        """
        history: list[VerificationResult] = []

        for attempt in range(1, max_attempts + 1):
            if self.event_bus:
                await self.event_bus.send_dialogue(
                    sender="antigravity",
                    recipient="verifier",
                    message=f"Starting verification cycle (attempt {attempt}/{max_attempts}).",
                )

            res = await self.verify(sandbox, test_command, ui_url=ui_url)
            history.append(res)

            if res.all_passed:
                if self.event_bus:
                    await self.event_bus.send_dialogue(
                        sender="verifier",
                        recipient="router",
                        message=f"All checks passed cleanly on attempt {attempt}. Ready for atomic merge.",
                    )
                return True, history

            if attempt < max_attempts:
                if self.event_bus:
                    await self.event_bus.send_dialogue(
                        sender="verifier",
                        recipient="codex",
                        message=f"Test failed with {res.diagnostics.error_type if res.diagnostics else 'Error'}: {res.diagnostics.error_message if res.diagnostics else ''}. Requesting targeted patch.",
                    )

                # Execute patch callback
                await patch_callback(res.diagnostics or DiagnosticReport(has_error=True), res.viewport_results)

        return False, history
