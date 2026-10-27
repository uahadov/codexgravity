"""
Live team console (TUI) — see the debate as it happens, and replay it later.

No third-party dependency. When stdout is a terminal it renders colors, badges, a
round indicator and a keep-alive spinner; when it is piped (CI, server logs) it
falls back to clean plain lines. The same module powers ``codex-gravity-tui``:

    codex-gravity-tui --workspace .                    # latest session, replay
    codex-gravity-tui --workspace . --session 9f3c1ae0b7d2 --follow
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional, TextIO

from codex_gravity.event_bus import Event
from codex_gravity.team.protocol import Kind, TeamMessage, TeamTranscript
from codex_gravity.team.session import TeamResult

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CLEAR_LINE = "\r\033[K"

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

SENDER_COLORS = {
    "codex": "\033[38;5;215m",
    "codex-cli": "\033[38;5;215m",
    "codex-gui": "\033[38;5;216m",
    "antigravity": "\033[38;5;45m",
    "antigravity-cli": "\033[38;5;45m",
    "antigravity-gui": "\033[38;5;51m",
    "orchestrator": "\033[38;5;220m",
    "verifier": "\033[38;5;114m",
    "human": "\033[38;5;111m",
}
COLOR_ORANGE = "\033[38;5;208m"
COLOR_GREEN = "\033[38;5;114m"
COLOR_RED = "\033[38;5;203m"
COLOR_GREY = "\033[38;5;244m"

BADGES = {
    Kind.TASK: ("TASK", COLOR_GREY),
    Kind.PROPOSAL: ("PLAN", COLOR_ORANGE),
    Kind.CRITIQUE: ("CRIT", COLOR_RED),
    Kind.QUESTION: ("ASK", "\033[38;5;220m"),
    Kind.ANSWER: ("ANS", "\033[38;5;45m"),
    Kind.REVISION: ("REV", COLOR_ORANGE),
    Kind.APPROVAL: ("PASS", COLOR_GREEN),
    Kind.REJECTION: ("FAIL", COLOR_RED),
    Kind.NOTE: ("NOTE", "\033[38;5;111m"),
    Kind.CONSENSUS: ("DONE", COLOR_GREEN),
    Kind.ESCALATION: ("ESC", COLOR_RED),
    Kind.VERIFICATION: ("TEST", "\033[38;5;114m"),
    Kind.STATUS: ("..", COLOR_GREY),
}

STATUS_COLORS = {
    "consensus": COLOR_GREEN,
    "verified": COLOR_GREEN,
    "deadlocked": COLOR_RED,
    "failed": COLOR_RED,
    "unavailable": COLOR_RED,
    "cancelled": "\033[38;5;220m",
}


def _plain(body: str, limit_lines: int = 8, width: int = 110) -> list[str]:
    lines = [line.rstrip() for line in (body or "").strip().splitlines() if line.strip()]
    out = [line[:width] + ("…" if len(line) > width else "") for line in lines[:limit_lines]]
    if len(lines) > limit_lines:
        out.append(f"… (+{len(lines) - limit_lines} lines)")
    return out


class BaseUi:
    """Shared rendering logic; subclasses decide how lines are written."""

    def __init__(self, stream: Optional[TextIO] = None, *, color: bool = False) -> None:
        self.stream = stream or sys.stdout
        self.color = color
        self.session_id: Optional[str] = None
        self.round_no = 0
        self.messages = 0
        self.started = time.perf_counter()

    # -- color helpers -----------------------------------------------------
    def paint(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.color else text

    def width(self) -> int:
        try:
            return max(60, min(120, os.get_terminal_size(self.stream.fileno()).columns))
        except (OSError, ValueError, AttributeError):
            return 100

    # -- lifecycle ---------------------------------------------------------
    def header(self, task: str, workspace: str, session: Optional[str], agents: dict[str, Any]) -> None:
        line = "─" * self.width()
        self.write(f"┌ {self.paint('Codex · Gravity — team session', BOLD)} {line[: max(0, self.width() - 32)]}")
        self.write(f"│ task      {task.strip()[: self.width() - 12]}")
        self.write(f"│ workspace {workspace}")
        self.write(f"│ session   {session or '(generated at start)'}")
        if agents:
            parts = []
            for name, agent in sorted(agents.items()):
                ok = agent.availability()[0] if hasattr(agent, "availability") else True
                mark = self.paint("✓", COLOR_GREEN) if ok else self.paint("✗", COLOR_GREY)
                parts.append(f"{name} {mark}")
            self.write("│ agents    " + " · ".join(parts))
        self.write("└" + line[: self.width()])

    def on_event(self, event: Event) -> None:
        payload = event.payload or {}
        session = payload.get("team_session")
        if not session:
            return
        if self.session_id is None:
            self.session_id = session
        self.on_message(
            session_id=str(session),
            round_no=int(payload.get("round") or 0),
            kind=str(payload.get("kind") or "note"),
            sender=event.sender,
            recipient=event.recipient,
            body=str(payload.get("body") or event.message),
            at=event.timestamp.timestamp(),
        )

    def on_message(self, *, session_id: str, round_no: int, kind: str, sender: str,
                   recipient: str, body: str, at: float) -> None:
        if session_id != self.session_id:
            return
        if round_no and round_no != self.round_no:
            self.round_no = round_no
            self.write(self.paint(f"── round {round_no} " + "─" * max(0, self.width() - 12), DIM))
        self.clear_status()
        self.messages += 1
        self.write(self.format_message(kind=kind, sender=sender, recipient=recipient, body=body, at=at))

    def format_message(self, *, kind: str, sender: str, recipient: str, body: str, at: float) -> str:
        try:
            badge, badge_color = BADGES[Kind(kind)]
        except (ValueError, KeyError):
            badge, badge_color = kind.upper()[:4], COLOR_GREY
        stamp = time.strftime("%H:%M:%S", time.localtime(at))
        who = self.paint(f"{sender} → {recipient}", SENDER_COLORS.get(sender, ""))
        head = f"{self.paint(stamp, DIM)}  {self.paint(badge, badge_color)}  {who}"
        detail = _plain(body, width=self.width() - 8)
        if not detail:
            return head
        lines = [f"{head}  {detail[0]}"] + [f"        {self.paint(line, DIM)}" for line in detail[1:]]
        return "\n".join(lines)

    def render_transcript(self, messages: list[TeamMessage]) -> None:
        for message in messages:
            self.session_id = self.session_id or message.session_id
            self.on_message(
                session_id=message.session_id,
                round_no=message.round,
                kind=message.kind.value,
                sender=message.sender,
                recipient=message.recipient,
                body=message.body,
                at=message.created_at,
            )

    # -- spinner -----------------------------------------------------------
    def animation_frame(self, frame: int, label: str = "waiting for the next turn") -> str:
        elapsed = time.perf_counter() - self.started
        return f"{SPINNER[frame % len(SPINNER)]} {label} · {elapsed:4.0f}s"

    # -- output ------------------------------------------------------------
    def write(self, text: str) -> None:
        raise NotImplementedError

    def status(self, text: str) -> None:
        raise NotImplementedError

    def clear_status(self) -> None:
        raise NotImplementedError

    def start_animation(self, label: str = "waiting for the next turn") -> Any:
        """No-op hook; interactive UIs override this."""
        return None

    def stop_animation(self) -> None:
        """No-op hook; interactive UIs override this."""
        return None

    async def wait_close(self, timeout: Optional[float] = None) -> None:
        """No-op hook; full-screen UIs keep the result visible until a key."""
        return None

    def summary(self, result: TeamResult) -> None:
        color = STATUS_COLORS.get(result.status, COLOR_GREY)
        line = "─" * self.width()
        self.clear_status()
        self.write("")
        self.write(f"┌ {self.paint(result.status.upper(), color + BOLD)} {line[: max(0, self.width() - len(result.status) - 6)]}")
        self.write(f"│ decision    {result.decision.reason}")
        self.write(f"│ approved by {', '.join(result.decision.approvals) or '—'}")
        if result.decision.blockers:
            self.write(f"│ blockers    {len(result.decision.blockers)} (see transcript)")
        if result.verification:
            mark = self.paint("PASS", COLOR_GREEN) if result.verification.passed else self.paint("FAIL", COLOR_RED)
            self.write(
                f"│ verification {mark}  `{result.verification.command}` exit {result.verification.exit_code}"
                f" in {result.verification.duration_seconds}s"
            )
        self.write(f"│ rounds      {result.rounds} · messages {result.transcript_messages} · {result.elapsed_seconds}s")
        self.write(f"│ transcript  {result.transcript_path}")
        if result.report_path:
            self.write(f"│ report      {result.report_path}")
        self.write("└" + line[: self.width()])


def _quit_on_broken_pipe(stream: Optional[TextIO] = None) -> None:
    """
    Exit quietly when the reader goes away (e.g. ``codex-gravity-tui | head``).

    The real stdout is closed so the interpreter's final flush cannot print a
    second traceback. A custom stream (pytest capture, another UI's sink) is
    never touched.
    """
    if stream is None or stream is sys.stdout:
        try:
            sys.stdout.close()
        except (OSError, ValueError):
            pass
    raise SystemExit(0)


class PlainUi(BaseUi):
    """Piped/CI output: no escape codes, one line per message."""

    def write(self, text: str) -> None:
        try:
            print(text, file=self.stream, flush=True)
        except BrokenPipeError:
            _quit_on_broken_pipe(self.stream)

    def status(self, text: str) -> None:
        try:
            print(f"[{text}]", file=self.stream, flush=True)
        except BrokenPipeError:
            _quit_on_broken_pipe(self.stream)

    def clear_status(self) -> None:
        return None


class AnsiUi(BaseUi):
    """Interactive terminal: colors, badges, a spinner on the last line."""

    def __init__(self, stream: Optional[TextIO] = None, *, force_color: bool = False) -> None:
        stream = stream or sys.stdout
        color = (
            force_color
            or bool(getattr(stream, "isatty", lambda: False)())
        ) and not os.environ.get("NO_COLOR")
        super().__init__(stream, color=color)
        self._animation: Optional[asyncio.Task] = None
        self._status_visible = False

    def write(self, text: str) -> None:
        if self._status_visible:
            self.stream.write(CLEAR_LINE)
            self._status_visible = False
        try:
            self.stream.write(text + "\n")
            self.stream.flush()
        except BrokenPipeError:
            _quit_on_broken_pipe(self.stream)

    def status(self, text: str) -> None:
        try:
            self.stream.write(f"{CLEAR_LINE}{self.paint(text, DIM)}")
            self.stream.flush()
        except BrokenPipeError:
            _quit_on_broken_pipe(self.stream)
        self._status_visible = True

    def clear_status(self) -> None:
        if self._status_visible:
            try:
                self.stream.write(CLEAR_LINE)
                self.stream.flush()
            except BrokenPipeError:
                _quit_on_broken_pipe(self.stream)
            self._status_visible = False

    async def animate(self, label: str = "waiting for the next turn") -> None:
        frame = 0
        try:
            while True:
                self.status(self.animation_frame(frame, label))
                frame += 1
                await asyncio.sleep(0.12)
        except asyncio.CancelledError:
            self.clear_status()

    def start_animation(self, label: str = "waiting for the next turn") -> asyncio.Task:
        self._animation = asyncio.create_task(self.animate(label))
        return self._animation

    def stop_animation(self) -> None:
        if self._animation is not None:
            self._animation.cancel()
            self._animation = None
        self.clear_status()


def make_ui(mode: str = "auto", stream: Optional[TextIO] = None) -> BaseUi:
    """
    ``auto`` → full-screen dashboard on a real terminal, plain when piped.

    ``gui``/``curses`` request the btop-style dashboard explicitly and fall back
    to plain output when the stream is not a terminal.
    """
    stream = stream or sys.stdout
    is_tty = bool(getattr(stream, "isatty", lambda: False)())
    if mode == "plain":
        return PlainUi(stream)
    if mode == "ansi":
        # Explicitly requested: keep colors even when the output is piped.
        return AnsiUi(stream, force_color=True)
    if mode in ("gui", "curses"):
        if not is_tty:
            return PlainUi(stream)
        from codex_gravity.tui_screen import CursesDashboard
        return CursesDashboard(stream)
    if is_tty and os.environ.get("TERM", "xterm") not in ("", "dumb"):
        from codex_gravity.tui_screen import CursesDashboard
        return CursesDashboard(stream)
    return AnsiUi(stream) if is_tty else PlainUi(stream)


# ---------------------------------------------------------------------------
# Standalone viewer: replay or follow a session from disk
# ---------------------------------------------------------------------------

def latest_session(workspace: Path) -> Optional[str]:
    base = workspace / ".codex-gravity" / "team"
    if not base.is_dir():
        return None
    sessions = [p for p in base.iterdir() if p.is_dir() and p.name != "tmp"]
    if not sessions:
        return None
    return max(sessions, key=lambda p: p.stat().st_mtime).name


def _load_result(workspace: Path, session_id: str) -> Optional[TeamResult]:
    path = workspace / ".codex-gravity" / "team" / session_id / "result.json"
    if not path.exists():
        return None
    try:
        return TeamResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


async def follow(workspace: Path, session_id: str, ui: BaseUi, timeout: float = 3600) -> None:
    transcript = TeamTranscript(workspace, session_id)
    seen = 0
    deadline = time.monotonic() + timeout
    ui.session_id = session_id
    while time.monotonic() < deadline:
        messages = transcript.messages()
        if len(messages) > seen:
            ui.render_transcript(messages[seen:])
            seen = len(messages)
            if messages and messages[-1].kind in (Kind.CONSENSUS, Kind.ESCALATION):
                break
        await asyncio.sleep(0.5)
    result = _load_result(workspace, session_id)
    if result is not None:
        ui.summary(result)


async def _replay(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    session_id = args.session or latest_session(workspace)
    if not session_id:
        print(f"No team sessions under {workspace}/.codex-gravity/team", file=sys.stderr)
        return 2

    ui = make_ui(args.ui)
    transcript = TeamTranscript(workspace, session_id)
    ui.header(
        task=f"replay of session {session_id}",
        workspace=str(workspace),
        session=session_id,
        agents={},
    )
    ui.start_animation("replaying session")
    try:
        if args.follow:
            await follow(workspace, session_id, ui, args.timeout)
        else:
            ui.render_transcript(transcript.messages())
            result = _load_result(workspace, session_id)
            if result is not None:
                ui.summary(result)
            else:
                ui.status("no result.json yet — rerun with --follow")
        await ui.wait_close()
    finally:
        ui.stop_animation()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codex-gravity-tui",
        description="Watch (or replay) a Codex-Gravity team session.",
    )
    parser.add_argument("-w", "--workspace", default=".", help="Project directory")
    parser.add_argument("--session", default=None, help="12-hex session id (default: the latest)")
    parser.add_argument("--follow", action="store_true", help="Keep tailing until consensus/escalation")
    parser.add_argument("--timeout", type=float, default=3600, help="Follow timeout in seconds")
    parser.add_argument(
        "--ui",
        choices=["auto", "gui", "curses", "ansi", "plain"],
        default="auto",
        help="auto/gui → full-screen dashboard on a terminal; ansi/plain → streaming lines",
    )
    raise SystemExit(asyncio.run(_replay(parser.parse_args())))


if __name__ == "__main__":
    main()
