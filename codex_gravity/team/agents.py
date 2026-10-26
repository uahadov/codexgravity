"""
Agent adapters: the real transports a team turn can use.

Every adapter reports failure instead of inventing an answer. A turn that cannot
reach its model returns ``ok=False`` with the reason, and the team records that
agent as unavailable for the round.

Transports
----------
``codex``            signed-in Codex CLI (read-only, ephemeral turn)
``codex-gui``        the ChatGPT/Codex desktop app, via its own app-tools catalogue
``antigravity``      Antigravity ``agentapi`` binary, else Google GenAI SDK
``antigravity-gui``  the visible Antigravity desktop GUI (opt-in; drives a real chat)
"""

from __future__ import annotations

import asyncio
import os
import shlex
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from codex_gravity.codex_bridge import BridgeError, codex_binary, run_codex_architect
from codex_gravity.codex_gui import CodexDesktopChat, desktop_availability
from codex_gravity.speculative import call_agentapi, find_agentapi_bin, get_gemini_client
from codex_gravity.team.protocol import Role, TeamError


@dataclass
class AgentTurn:
    """One request from the orchestrator to an agent."""

    task: str
    instruction: str
    context: str
    workspace: Path
    role: Role
    round: int
    session_id: str
    timeout: float = 180.0


@dataclass
class AgentReply:
    ok: bool
    text: str = ""
    error: str = ""
    engine: str = "unknown"
    duration_ms: float = 0.0
    meta: dict = field(default_factory=dict)


class TeamAgent(ABC):
    """A teammate. Subclasses implement :meth:`_ask` and expose ``name``."""

    name: str = "agent"

    def __init__(self, *, timeout: float = 180.0) -> None:
        self.timeout = timeout

    def availability(self) -> tuple[bool, str]:
        """Cheap transport probe used for pre-flight reporting (not a success claim)."""
        return True, ""

    def describe(self) -> dict:
        ok, reason = self.availability()
        return {"name": self.name, "transport_available": ok, "reason": reason}

    async def respond(self, turn: AgentTurn) -> AgentReply:
        timeout = min(turn.timeout, self.timeout) if self.timeout else turn.timeout
        if timeout <= 0:
            return AgentReply(ok=False, error="Turn timeout must be positive.", engine=self.name)
        start = time.perf_counter()
        try:
            reply = await asyncio.wait_for(self._ask(turn, timeout), timeout=timeout)
        except asyncio.TimeoutError:
            return AgentReply(
                ok=False,
                error=f"{self.name} did not answer within {timeout:.0f}s.",
                engine=self.name,
                duration_ms=round((time.perf_counter() - start) * 1000, 1),
            )
        except TeamError as error:
            return AgentReply(ok=False, error=str(error), engine=self.name)
        except (BridgeError, OSError, ValueError) as error:
            return AgentReply(ok=False, error=str(error) or type(error).__name__, engine=self.name)
        except Exception as error:  # a broken transport must not kill the session
            return AgentReply(ok=False, error=f"{type(error).__name__}: {error}", engine=self.name)
        reply.duration_ms = round((time.perf_counter() - start) * 1000, 1)
        return reply

    @abstractmethod
    async def _ask(self, turn: AgentTurn, timeout: float) -> AgentReply:
        raise NotImplementedError


def _prompt(turn: AgentTurn) -> str:
    return (
        f"{turn.instruction}\n\nTASK:\n{turn.task}\n\n"
        f"SHARED TEAM TRANSCRIPT (most recent last):\n{turn.context}"
    )


class CodexAgent(TeamAgent):
    """Signed-in Codex CLI, ephemeral read-only turn."""

    name = "codex"

    def availability(self) -> tuple[bool, str]:
        try:
            codex_binary()
        except BridgeError as error:
            return False, str(error)
        return True, "codex CLI found (sign-in is verified on the first turn)"

    async def _ask(self, turn: AgentTurn, timeout: float) -> AgentReply:
        data = await run_codex_architect(
            _prompt(turn),
            code_context="",
            workspace=str(turn.workspace),
            timeout=timeout,
        )
        answer = str(data.get("answer") or "").strip()
        if not answer:
            return AgentReply(ok=False, error="Codex returned an empty answer.", engine="codex-cli")
        return AgentReply(ok=True, text=answer, engine=str(data.get("engine", "signed-in-codex-cli")))


class CodexGUIAgent(TeamAgent):
    """
    The ChatGPT/Codex desktop app as a teammate — no CLI involved.

    One turn = one message into an exact existing chat + waiting for a new
    completed reply, observed through the app's own tool catalogue. If the app
    cannot read the thread back, the turn fails instead of assuming an answer.
    """

    name = "codex-gui"

    def __init__(
        self,
        *,
        destination: Optional[str] = None,
        timeout: float = 300.0,
        poll_interval: float = 2.0,
        chat: Optional[CodexDesktopChat] = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.destination = (
            destination
            or os.environ.get("CODEX_GRAVITY_CODEX_THREAD_ID")
            or os.environ.get("CODEX_THREAD_ID")
        )
        self.poll_interval = max(0.2, poll_interval)
        self._chat = chat

    def availability(self) -> tuple[bool, str]:
        return desktop_availability(self.destination)

    def _session(self) -> CodexDesktopChat:
        if self._chat is None:
            if not self.destination:
                raise TeamError(
                    "No desktop chat id: pass --codex-thread or set "
                    "CODEX_GRAVITY_CODEX_THREAD_ID / CODEX_THREAD_ID."
                )
            self._chat = CodexDesktopChat(self.destination, poll_interval=self.poll_interval)
        return self._chat

    async def _ask(self, turn: AgentTurn, timeout: float) -> AgentReply:
        reply = await self._session().ask(_prompt(turn), timeout=timeout)
        if not reply.ok:
            return AgentReply(ok=False, error=reply.error, engine=self.name, meta={"tools": reply.tools})
        return AgentReply(
            ok=True,
            text=reply.text,
            engine="codex-desktop-gui",
            meta={
                "turns_before": reply.turns_before,
                "turns_after": reply.turns_after,
                "waited_seconds": reply.waited_seconds,
            },
        )


class AntigravityCLIAgent(TeamAgent):
    """
    Antigravity **command line only** (``agentapi`` / ``agy``), no SDK fallback.

    This is the transport for headless servers: it shells out to the installed
    CLI and never silently switches to a cloud SDK. A custom command template
    (``CODEX_GRAVITY_ANTIGRAVITY_CLI_CMD='agy run {prompt}'``) is supported for
    distributions whose CLI flags differ.
    """

    name = "antigravity-cli"

    def __init__(
        self,
        *,
        timeout: float = 180.0,
        model: str = "flash",
        binary: Optional[str] = None,
        command_template: Optional[str] = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.model = model
        self.binary = Path(binary).expanduser() if binary else None
        self.command_template = command_template or os.environ.get("CODEX_GRAVITY_ANTIGRAVITY_CLI_CMD")

    def _binary(self) -> Optional[Path]:
        binary = self.binary or find_agentapi_bin()
        if binary and Path(binary).is_file() and os.access(binary, os.X_OK):
            return Path(binary)
        return None

    def availability(self) -> tuple[bool, str]:
        if self.command_template:
            return True, f"custom CLI command template: {self.command_template.split()[0]}…"
        binary = self._binary()
        if binary:
            return True, f"antigravity CLI: {binary}"
        return False, (
            "antigravity/agentapi CLI not found: install it, or set "
            "CODEX_GRAVITY_ANTIGRAVITY_CLI_BIN / CODEX_GRAVITY_ANTIGRAVITY_CLI_CMD"
        )

    async def _ask(self, turn: AgentTurn, timeout: float) -> AgentReply:
        prompt = _prompt(turn)
        if self.command_template:
            text, error = await _run_cli_template(self.command_template, prompt, timeout)
            if text:
                return AgentReply(ok=True, text=text, engine="antigravity-cli (custom template)")
            return AgentReply(ok=False, error=error, engine="antigravity-cli")

        binary = self._binary()
        if binary is None:
            return AgentReply(ok=False, error=self.availability()[1], engine="antigravity-cli")
        text = await call_agentapi(prompt, model=self.model, timeout_sec=timeout, binary=binary)
        if text and text.strip():
            return AgentReply(ok=True, text=text.strip(), engine=f"antigravity-cli ({binary.name})")
        return AgentReply(
            ok=False,
            error="The antigravity CLI produced no answer (new-conversation failed or timed out).",
            engine="antigravity-cli",
        )


async def _run_cli_template(template: str, prompt: str, timeout: float) -> tuple[str, str]:
    """Run a user-supplied command template, substituting ``{prompt}`` safely."""
    command = template.replace("{prompt}", shlex.quote(prompt))
    if "{prompt}" not in template:
        command = f"{template} {shlex.quote(prompt)}"
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            os.killpg(os.getpgid(process.pid), 9)
        except (ProcessLookupError, PermissionError):
            process.kill()
        return "", f"The CLI command timed out after {timeout:.0f}s."
    if process.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-3:]
        return "", f"The CLI command exited {process.returncode}: {' / '.join(tail)}"
    text = out.decode("utf-8", "replace").strip()
    return (text, "") if text else ("", "The CLI command produced no output.")


class AntigravityAgent(TeamAgent):
    """Google Antigravity / Gemini through ``agentapi`` or the GenAI SDK."""

    name = "antigravity"

    def __init__(self, *, timeout: float = 120.0, model: Optional[str] = None) -> None:
        super().__init__(timeout=timeout)
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    def availability(self) -> tuple[bool, str]:
        if find_agentapi_bin():
            return True, "Antigravity agentapi binary found"
        if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            return True, f"Google GenAI SDK configured ({self.model})"
        return False, "no agentapi binary and no GEMINI_API_KEY/GOOGLE_API_KEY"

    async def _ask(self, turn: AgentTurn, timeout: float) -> AgentReply:
        prompt = _prompt(turn)
        text = await call_agentapi(prompt, model="flash", timeout_sec=min(timeout, 120.0))
        if text and text.strip():
            return AgentReply(ok=True, text=text.strip(), engine="antigravity-agentapi")

        client = get_gemini_client()
        if client is not None:
            response = await asyncio.to_thread(
                client.models.generate_content, model=self.model, contents=prompt
            )
            body = (getattr(response, "text", "") or "").strip()
            if body:
                return AgentReply(ok=True, text=body, engine=f"google-genai ({self.model})")
            return AgentReply(ok=False, error="Gemini returned an empty response.", engine="google-genai")

        return AgentReply(
            ok=False,
            error=(
                "No Antigravity transport available: install the agentapi binary or set "
                "GEMINI_API_KEY/GOOGLE_API_KEY, or use the opt-in 'antigravity-gui' agent."
            ),
            engine="antigravity",
        )


class AntigravityGUIAgent(TeamAgent):
    """
    Drive the visible Antigravity desktop conversation (opt-in).

    This is the real desktop teammate: it opens a fresh conversation in the
    selected project, waits for the turn to finish and returns the GUI response.
    It refuses a different model, a busy chat, or an unsent draft instead of
    hijacking the operator's conversation.
    """

    name = "antigravity-gui"

    def __init__(
        self,
        *,
        timeout: float = 600.0,
        expected_model: str = "Gemini 3.8 Flash High",
        poll_interval: float = 2.0,
        gui=None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.expected_model = expected_model
        self.poll_interval = max(0.2, poll_interval)
        self._gui = gui

    def _get_gui(self):
        if self._gui is None:
            from codex_gravity.gui import AntigravityGUI
            self._gui = AntigravityGUI()
        return self._gui

    def availability(self) -> tuple[bool, str]:
        from codex_gravity.gui import GUIError, discover_endpoint
        try:
            return True, f"desktop DevTools endpoint: {discover_endpoint()}"
        except GUIError as error:
            return False, str(error)

    async def _ask(self, turn: AgentTurn, timeout: float) -> AgentReply:
        gui = self._get_gui()
        record = await gui.start(
            prompt=_prompt(turn),
            workspace_path=str(turn.workspace),
            expected_model=self.expected_model,
            new_conversation=True,
        )
        task_id = record.get("task_id")
        if not task_id:
            return AgentReply(ok=False, error="The GUI did not return a task id.", engine="antigravity-gui")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_interval)
            state = await gui.poll(str(turn.workspace), task_id)
            status = state.get("state")
            if status == "ready_for_review":
                body = str(state.get("response") or "").strip()
                if not body:
                    return AgentReply(ok=False, error="The GUI turn ended without a response.", engine="antigravity-gui")
                return AgentReply(ok=True, text=body, engine="antigravity-gui", meta={"gui_task_id": task_id})
            if status in ("needs_attention", "unconfirmed"):
                return AgentReply(
                    ok=False,
                    error=str(state.get("reason") or f"GUI task needs attention ({status})."),
                    engine="antigravity-gui",
                )
        return AgentReply(ok=False, error="The GUI turn did not finish before the timeout.", engine="antigravity-gui")


def default_agents(
    *,
    turn_timeout: float = 180.0,
    gui_agent: bool = False,
    codex_thread: Optional[str] = None,
) -> dict[str, TeamAgent]:
    """
    The agents available to a team unless the caller injects its own.

    ``codex-gui`` and the ``*-cli`` aliases are always listed so ``--check`` can
    explain why they are (not) usable; a name only matters if a session routes a
    turn to it.
    """
    codex_cli = CodexAgent(timeout=turn_timeout)
    agents: dict[str, TeamAgent] = {
        # Command-line transports (headless servers, CI).
        "codex": codex_cli,
        "codex-cli": codex_cli,
        "antigravity-cli": AntigravityCLIAgent(timeout=turn_timeout),
        # Desktop transports.
        "codex-gui": CodexGUIAgent(
            destination=codex_thread,
            timeout=max(turn_timeout, 300.0),
        ),
        # Best-effort transport: local agentapi first, then the GenAI SDK.
        "antigravity": AntigravityAgent(timeout=min(turn_timeout, 120.0)),
    }
    if gui_agent:
        agents["antigravity-gui"] = AntigravityGUIAgent(timeout=max(turn_timeout, 600.0))
    return agents


def resolve_agents(names: list[str], agents: dict[str, TeamAgent]) -> list[TeamAgent]:
    resolved: list[TeamAgent] = []
    for name in names:
        agent = agents.get(name)
        if agent is None:
            raise TeamError(f"Unknown agent '{name}'. Available: {', '.join(sorted(agents))}.")
        resolved.append(agent)
    return resolved
