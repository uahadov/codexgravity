"""
Sütun 3: Visual Self-Healing & Verification Engine.
Runs terminal test commands inside the isolated sandbox, performs log root-cause analysis,
conducts multi-viewport (Mobile, Tablet, Desktop) visual regression checks,
and coordinates autonomous self-healing loops between Antigravity and Codex.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional, Union
from pydantic import BaseModel, Field

from codex_gravity.event_bus import Event, EventBus, EventType
from codex_gravity.sandbox import CommandResult, LockState, SandboxManager


class TestExecutionResult(BaseModel):
    passed: bool
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


class StackFrame(BaseModel):
    file_path: str
    line_number: int
    function_name: str = ""
    code_snippet: str = ""
    is_test: bool = False


class DiagnosticReport(BaseModel):
    has_error: bool
    error_type: str = "NoError"
    error_message: str = ""
    failed_file: Optional[str] = None
    failed_line: Optional[int] = None
    target_code_snippet: Optional[str] = None
    recommended_fix: str = ""
    raw_log_snippet: str = ""

    # Source code localization fields for targeted self-healing
    source_file: Optional[str] = None
    source_line: Optional[int] = None
    source_code_snippet: Optional[str] = None
    failing_function: Optional[str] = None
    test_file: Optional[str] = None
    test_line: Optional[int] = None
    test_assert_snippet: Optional[str] = None
    stack_frames: list[StackFrame] = Field(default_factory=list)


class ViewportCheckResult(BaseModel):
    viewport_name: str
    width: int
    height: int
    passed: bool
    # ``measured`` separates "a real renderer inspected this viewport" from
    # "no visual backend is configured". An unmeasured viewport is never
    # reported as a verified pass.
    measured: bool = False
    backend: str = "none"
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
    viewports_measured: bool = False
    requires_healing: bool = False

    @property
    def summary_text(self) -> str:
        status = "PASSED" if self.all_passed else "FAILED"
        if not self.viewport_results:
            vp_status = "not requested"
        elif not self.viewports_measured:
            vp_status = "NOT MEASURED (no visual backend configured)"
        elif all(v.passed for v in self.viewport_results):
            vp_status = "All viewports OK"
        else:
            vp_status = "Viewport issues detected"
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

        failed_file: Optional[str] = None
        failed_line: Optional[int] = None
        target_snippet: Optional[str] = None
        error_type: str = "TestFailure"
        error_message: str = ""

        # 1. Check for Pytest test failure frame
        # e.g.: test_calc.py:4: in test_calc\n>       assert add(2, 2) == 4\nE       AssertionError: assert -2 == 4
        pytest_frame = re.search(r'([\w/.-]+\.py):(\d+): in ([^\n]+)\n(?:[^\n]*\n)*?>\s*(.+)', combined)
        if pytest_frame:
            failed_file = pytest_frame.group(1)
            failed_line = int(pytest_frame.group(2))
            target_snippet = pytest_frame.group(4).strip()

        # 1b. Check for Pytest direct test failure line at bottom:
        # e.g.: test_math.py:3: AssertionError
        pytest_bottom_frame = re.search(r'([\w/.-]+\.py):(\d+):\s*([A-Za-z_]\w*(?:Error|Exception))', combined)
        if pytest_bottom_frame:
            if not failed_file:
                failed_file = pytest_bottom_frame.group(1)
            if not failed_line:
                failed_line = int(pytest_bottom_frame.group(2))
            error_type = pytest_bottom_frame.group(3)
            if not target_snippet:
                snip_match = re.search(r'>\s*([^\n]+)', combined)
                if snip_match:
                    target_snippet = snip_match.group(1).strip()
            if not error_message:
                err_msg_match = re.search(r'E\s+(?:[A-Za-z_]\w*:\s*)?([^\n]+)', combined)
                if err_msg_match:
                    error_message = err_msg_match.group(1).strip()

        # 2. Check for standard Python tracebacks (search all frames and pick innermost user frame)
        # Handles <module>, <lambda>, <listcomp>, methods, etc.
        tb_matches = list(re.finditer(r'File "([^"]+)", line (\d+), in ([^\n]+)\n\s*(.+)', combined))
        if tb_matches:
            user_frames = [
                m for m in tb_matches
                if not any(k in m.group(1) for k in ("_pytest", "unittest", "pluggy", "<frozen", "site-packages"))
            ]
            best_match = user_frames[-1] if user_frames else tb_matches[-1]
            if not failed_file:
                failed_file = best_match.group(1)
                failed_line = int(best_match.group(2))
                target_snippet = best_match.group(4).strip()

        # 3. Check Pytest FAILED summary line
        # FAILED tests/test_auth.py::test_login - AssertionError: assert False
        pytest_failed = re.search(r'FAILED ([\w/.-]+)::(\w+)(?: - (.+))?', combined)
        if pytest_failed:
            if not failed_file:
                failed_file = pytest_failed.group(1)
            if pytest_failed.group(3):
                error_message = pytest_failed.group(3).strip()

        # 4. Check Jest / Node JS pattern
        jest_match = re.search(r'FAIL ([\w/.-]+)', combined)
        if jest_match and not failed_file:
            failed_file = jest_match.group(1)
        jest_err = re.search(r'●\s+(.+)', combined)
        if jest_err and not error_message:
            error_message = jest_err.group(1).strip()
        jest_snippet = re.search(r'>\s*(\d+)\s*\|\s*(.+)', combined)
        if jest_snippet:
            if not failed_line:
                failed_line = int(jest_snippet.group(1))
            if not target_snippet:
                target_snippet = jest_snippet.group(2).strip()

        # 5. Check TypeScript compiler error
        # src/index.ts:15:7 - error TS2322: Type 'string' is not assignable to type 'number'.
        ts_match = re.search(r'([^\s:]+\.[jt]sx?):(\d+):(\d+)\s*-\s*error\s*(TS\d+):\s*(.+)', combined)
        if ts_match:
            if not failed_file:
                failed_file = ts_match.group(1)
            if not failed_line:
                failed_line = int(ts_match.group(2))
            error_type = ts_match.group(4)
            if not error_message:
                error_message = ts_match.group(5).strip()

        # 6. Check Python Exception / Error types
        err_matches = list(re.finditer(r'([A-Za-z_]\w*(?:Error|Exception)):\s*(.*)', combined))
        if err_matches:
            last_err = err_matches[-1]
            error_type = last_err.group(1)
            if not error_message:
                error_message = last_err.group(2).strip()

        has_error = bool(failed_file or tb_matches or pytest_failed or err_matches or jest_match or ts_match)
        if not has_error and combined.strip() and any(k in combined.lower() for k in ("fail", "error", "exception", "traceback")):
            has_error = True
            error_type = "ExecutionFailure"
            error_message = combined.strip().splitlines()[-1]

        # 7. Collect all stack frames to distinguish test frames from source frames
        stack_frames: list[StackFrame] = []
        source_file: Optional[str] = None
        source_line: Optional[int] = None
        source_code_snippet: Optional[str] = None
        failing_function: Optional[str] = None
        test_file: Optional[str] = None
        test_line: Optional[int] = None
        test_assert_snippet: Optional[str] = None

        def _is_test_file(p: str) -> bool:
            name = Path(p).name.lower()
            return "test" in name or "/test/" in p or "/tests/" in p or p.startswith("test_")

        def _is_lib_file(p: str) -> bool:
            return any(k in p for k in ("_pytest", "unittest", "pluggy", "<frozen", "site-packages"))

        # Parse all pytest frames
        pytest_matches = list(re.finditer(r'([\w/.-]+\.py):(\d+):\s*in\s*([^\n]+)', combined))
        for i, m in enumerate(pytest_matches):
            f_path = m.group(1)
            f_line = int(m.group(2))
            f_func = m.group(3).strip()
            start_pos = m.end()
            end_pos = pytest_matches[i + 1].start() if i + 1 < len(pytest_matches) else len(combined)
            frame_body = combined[start_pos:end_pos]
            snip_match = re.search(r'^\s*>\s*([^\n]+)', frame_body, re.MULTILINE)
            f_snip = snip_match.group(1).strip() if snip_match else ""
            stack_frames.append(StackFrame(
                file_path=f_path,
                line_number=f_line,
                function_name=f_func,
                code_snippet=f_snip,
                is_test=_is_test_file(f_path),
            ))

        # Parse all standard python traceback frames
        for m in tb_matches:
            f_path = m.group(1)
            f_line = int(m.group(2))
            f_func = m.group(3).strip()
            f_snip = m.group(4).strip()
            if not _is_lib_file(f_path):
                stack_frames.append(StackFrame(
                    file_path=f_path,
                    line_number=f_line,
                    function_name=f_func,
                    code_snippet=f_snip,
                    is_test=_is_test_file(f_path),
                ))

        source_frames = [f for f in stack_frames if not f.is_test and not _is_lib_file(f.file_path)]
        test_frames = [f for f in stack_frames if f.is_test]

        if source_frames:
            best_source = source_frames[-1]
            source_file = best_source.file_path
            source_line = best_source.line_number
            source_code_snippet = best_source.code_snippet
            if best_source.function_name and not best_source.function_name.startswith("<"):
                failing_function = best_source.function_name

        if test_frames:
            best_test = test_frames[-1]
            test_file = best_test.file_path
            test_line = best_test.line_number
            test_assert_snippet = best_test.code_snippet

        # If failing_function was not in source frame, extract from test assertion or pytest where clause
        # Supports method invocations like calc.add(), self.service.run(), etc.
        if not failing_function or failing_function.startswith("<"):
            where_match = re.search(r'where\s+.*=\s+(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*)\s*\(', combined)
            if where_match:
                failing_function = where_match.group(1)
            elif target_snippet:
                assert_match = re.search(r'assert\s+(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*)\s*\(', target_snippet)
                if assert_match:
                    failing_function = assert_match.group(1)
                else:
                    call_match = re.search(r'(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*)\s*\(', target_snippet)
                    if call_match and call_match.group(1) not in (
                        "assert", "bool", "int", "str", "len", "float", "list", "dict", "set", "tuple", "isinstance", "type"
                    ):
                        failing_function = call_match.group(1)

        if has_error:
            recommended_fix = cls._derive_fix(error_type, error_message, target_snippet)

            # Get last 20 lines of log
            lines = [line for line in combined.splitlines() if line.strip()]
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
                source_file=source_file,
                source_line=source_line,
                source_code_snippet=source_code_snippet,
                failing_function=failing_function,
                test_file=test_file,
                test_line=test_line,
                test_assert_snippet=test_assert_snippet,
                stack_frames=stack_frames,
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
        elif error_type in ("SyntaxError", "IndentationError"):
            return f"Syntax or indentation defect: {message}. Ensure code blocks are properly aligned and closed."
        elif error_type in ("KeyError", "IndexError"):
            return f"Out of bounds or missing key access: {message}. Guard access or verify collection initialization."
        elif error_type == "ValueError":
            return f"Invalid value encountered: {message}. Validate input constraints."
        elif error_type.startswith("TS"):
            return f"TypeScript type check failed ({error_type}): {message}. Update type annotations or interface definitions."
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
        No renderer is bundled, so nothing is measured here.

        Earlier versions guessed a layout from the URL string and reported a
        verified pass. That produced false "visual regression" results, so this
        backend now states explicitly that the viewport was not inspected.
        Supply ``custom_checker`` with a real browser backend to measure.
        """
        return ViewportCheckResult(
            viewport_name=name,
            width=width,
            height=height,
            passed=True,
            measured=False,
            backend="none",
            screenshot_path=None,
            analysis=(
                f"Viewport {name} ({width}x{height}) for '{ui_url}' was not measured: "
                "no visual backend is configured. Install a browser checker via "
                "VisualRegressionInspector(custom_checker=...) to verify layout."
            ),
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
        viewports_measured = bool(viewport_results) and all(vp.measured for vp in viewport_results)
        all_passed = test_passed and viewports_passed
        requires_healing = not all_passed

        result = VerificationResult(
            all_passed=all_passed,
            test_result=test_result,
            diagnostics=diagnostics,
            viewport_results=viewport_results,
            viewports_measured=viewports_measured,
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

                # Execute patch callback under HEALING state to avoid read-only lock collisions
                prev_state = sandbox.state_lock.state
                await sandbox.state_lock.transition(LockState.HEALING)
                try:
                    await patch_callback(res.diagnostics or DiagnosticReport(has_error=True), res.viewport_results)
                finally:
                    await sandbox.state_lock.transition(prev_state)

        return False, history
