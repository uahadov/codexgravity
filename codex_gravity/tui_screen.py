"""
btop-style full-screen dashboard for a team session (curses, no dependencies).

The screen is split into panels that redraw ~10 times per second:

    ┌ Codex · Gravity ────────────────────────── session 9f3c1ae0b7d2 · 00:14 ┐
    │ task  Add retry handling to the HTTP client                            │
    ├ AGENTS ──────────────┬ TRANSCRIPT ────────────────────────────────────┐
    │ ✓ codex        cli   │ 03:07:55  PLAN  codex → all                  │
    │ ✓ antigravity  agent │   # Plan v1                                  │
    │ ✗ codex-gui    no id │ ── round 2 ─────────────────────────────────  │
    │ ─ STATUS ────────────│ 03:07:59  PASS  codex → antigravity           │
    │ round 2 · msgs 9     │   verdict: approve                           │
    │ ─ CONSENSUS ─────────│                                              │
    │ VERIFIED · exit 0    │                                              │
    ├──────────────────────┴──────────────────────────────────────────────┤
    │ q quit · ↑↓ scroll · f follow · ? help · waiting for antigravity ⠹  │
    └─────────────────────────────────────────────────────────────────────┘

Layout is a pure function (:func:`render`) over a plain state dict, so it is
unit-tested without a terminal; :class:`CursesDashboard` only paints it.
"""

from __future__ import annotations

import asyncio
import curses
import locale
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional, TextIO

from codex_gravity.team.protocol import Kind
from codex_gravity.team.session import TeamResult
from codex_gravity.tui import BaseUi, PlainUi, _plain

MIN_WIDTH = 76
MIN_HEIGHT = 18

STYLE_BORDER = "border"
STYLE_TITLE = "title"
STYLE_DIM = "dim"
STYLE_OK = "ok"
STYLE_ERR = "err"
STYLE_WARN = "warn"
STYLE_VALUE = "value"
STYLE_SENDER = "sender"
STYLE_BADGE = "badge"

BADGES = {
    Kind.TASK: ("TASK", STYLE_DIM),
    Kind.PROPOSAL: ("PLAN", STYLE_WARN),
    Kind.CRITIQUE: ("CRIT", STYLE_ERR),
    Kind.QUESTION: ("ASK", STYLE_WARN),
    Kind.ANSWER: ("ANS", STYLE_SENDER),
    Kind.REVISION: ("REV", STYLE_WARN),
    Kind.APPROVAL: ("PASS", STYLE_OK),
    Kind.REJECTION: ("FAIL", STYLE_ERR),
    Kind.NOTE: ("NOTE", STYLE_SENDER),
    Kind.CONSENSUS: ("DONE", STYLE_OK),
    Kind.ESCALATION: ("ESC", STYLE_ERR),
    Kind.VERIFICATION: ("TEST", STYLE_OK),
    Kind.STATUS: ("..", STYLE_DIM),
}

SPARKS = "▁▂▃▄▅▆▇█"


class Canvas:
    """Fixed-size character grid with a style per cell (pure, testable)."""

    def __init__(self, width: int, height: int) -> None:
        self.width = max(1, width)
        self.height = max(1, height)
        self.chars = [[" "] * self.width for _ in range(self.height)]
        self.styles = [[None] * self.width for _ in range(self.height)]

    def put(self, x: int, y: int, text: str, style: Optional[str] = None) -> None:
        if not 0 <= y < self.height:
            return
        for offset, char in enumerate(text):
            column = x + offset
            if 0 <= column < self.width:
                self.chars[y][column] = char
                self.styles[y][column] = style

    def box(self, x: int, y: int, width: int, height: int, title: str = "", style: str = STYLE_BORDER) -> None:
        if width < 2 or height < 2:
            return
        right, bottom = x + width - 1, y + height - 1
        self.put(x, y, "┌" + "─" * (width - 2) + "┐", style)
        self.put(x, bottom, "└" + "─" * (width - 2) + "┘", style)
        for row in range(y + 1, bottom):
            self.put(x, row, "│", style)
            self.put(right, row, "│", style)
        if title:
            label = f" {title} "
            self.put(x + 2, y, label[: max(0, width - 4)], STYLE_TITLE)

    def separator(self, y: int, x: int, width: int, label: str = "") -> None:
        line = ("─ " + label + " " if label else "") + "─" * width
        self.put(x, y, line[:width], STYLE_BORDER)

    def text(self) -> str:
        return "\n".join("".join(row) for row in self.chars)


def wrap(text: str, width: int, limit: Optional[int] = None) -> list[str]:
    width = max(8, width)
    out: list[str] = []
    for raw in (text or "").splitlines() or [""]:
        line = raw.rstrip()
        if not line:
            out.append("")
            continue
        while len(line) > width:
            out.append(line[:width])
            line = line[width:]
        out.append(line)
    if limit is not None and len(out) > limit:
        out = out[:limit]
        out[-1] = out[-1][: max(0, width - 1)] + "…"
    return out


def sparkline(values: list[int], width: int) -> str:
    if not values:
        return ""
    values = values[-width:]
    peak = max(values) or 1
    return "".join(SPARKS[min(len(SPARKS) - 1, int(value / peak * (len(SPARKS) - 1)))] for value in values)


def round_counts(feed: list[dict]) -> list[int]:
    counts: dict[int, int] = {}
    for item in feed:
        if item.get("kind") == "round":
            continue
        counts[item.get("round", 0)] = counts.get(item.get("round", 0), 0) + 1
    return [counts[key] for key in sorted(counts)]


@dataclass
class DashboardState:
    task: str = ""
    workspace: str = ""
    session: Optional[str] = None
    agents: dict[str, Any] = field(default_factory=dict)
    feed: list[dict] = field(default_factory=list)
    round_no: int = 0
    status: str = "waiting"
    started: float = field(default_factory=time.monotonic)
    result: Optional[TeamResult] = None
    scroll: int = 0
    follow: bool = True
    show_help: bool = False
    spinner: int = 0

    def elapsed(self) -> str:
        seconds = int(time.monotonic() - self.started)
        return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _transcript_lines(state: DashboardState, width: int) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for item in state.feed:
        if item.get("kind") == "round":
            rows.append((f"── round {item['round']} " + "─" * width, STYLE_DIM))
            continue
        try:
            badge, style = BADGES[Kind(item["kind"])]
        except (KeyError, ValueError):
            badge, style = str(item.get("kind", "?"))[:4].upper(), STYLE_DIM
        stamp = time.strftime("%H:%M:%S", time.localtime(item.get("at", time.time())))
        head = f"{stamp}  {badge:<4} {item['sender']} → {item['recipient']}"
        rows.append((head[:width], style))
        for line in _plain(item.get("body", ""), limit_lines=6, width=width - 4):
            rows.append(("    " + line, STYLE_VALUE))
    return rows


def _agent_rows(state: DashboardState, width: int) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    counts: dict[str, int] = {}
    for item in state.feed:
        if item.get("kind") != "round":
            counts[item.get("sender", "")] = counts.get(item.get("sender", ""), 0) + 1
    name_width = max(8, width - 8)
    for name, agent in sorted(state.agents.items()):
        ok, reason = agent.availability() if hasattr(agent, "availability") else (True, "")
        mark = "✓" if ok else "✗"
        used = counts.get(name, 0)
        label = f"{mark} {name}"[:name_width].ljust(name_width) + f"{used:>3}"
        rows.append((label[:width], STYLE_OK if ok else STYLE_DIM))
        if not ok and reason:
            limit = max(8, width - 4)
            rows.append(("   " + (reason[:limit] + ("…" if len(reason) > limit else "")), STYLE_DIM))
    return rows


def _consensus_rows(state: DashboardState, width: int) -> list[tuple[str, str]]:
    result = state.result
    if result is None:
        return [("running…", STYLE_DIM)]
    rows: list[tuple[str, str]] = []
    style = STYLE_OK if result.status in ("consensus", "verified") else STYLE_ERR
    rows.append((result.status.upper(), style))
    rows.append((f"approved  {', '.join(result.decision.approvals) or '—'}"[:width], STYLE_VALUE))
    if result.verification:
        mark = "PASS" if result.verification.passed else "FAIL"
        rows.append((f"tests     {mark} exit {result.verification.exit_code}"[:width],
                     STYLE_OK if result.verification.passed else STYLE_ERR))
    rows.append((f"rounds    {result.rounds}  msgs {result.transcript_messages}"[:width], STYLE_VALUE))
    return rows


def render(state: DashboardState, width: int, height: int) -> Canvas:
    """Pure layout: exact width×height grid for the dashboard."""
    canvas = Canvas(width, height)
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        canvas.put(1, 0, "Terminal too small for the dashboard — resize or use --ui plain", STYLE_WARN)
        canvas.put(1, 1, f"need {MIN_WIDTH}x{MIN_HEIGHT}, have {width}x{height}", STYLE_DIM)
        return canvas

    session = state.session or "…"
    title = " Codex · Gravity — team session "
    canvas.put(0, 0, "┌" + title + "─" * (width - len(title) - 2) + "┐", STYLE_BORDER)
    right = f" session {session} · {state.elapsed()} "
    canvas.put(width - len(right) - 1, 0, right, STYLE_DIM)
    canvas.put(0, 1, "│", STYLE_BORDER)
    canvas.put(width - 1, 1, "│", STYLE_BORDER)
    canvas.put(2, 1, f" task  {state.task}"[: width - 3], STYLE_VALUE)

    inner_top = 2
    foot_sep = height - 3
    inner_bottom = foot_sep
    body_h = inner_bottom - inner_top
    left_w = max(30, min(44, width // 3))
    gap = 1
    right_x = left_w + gap
    right_w = width - right_x

    canvas.put(0, inner_top, "├" + "─" * left_w + "┬" + "─" * (right_w - 2) + "┤", STYLE_BORDER)
    for row in range(inner_top + 1, inner_bottom):
        canvas.put(0, row, "│", STYLE_BORDER)
        canvas.put(left_w, row, "│", STYLE_BORDER)
        canvas.put(width - 1, row, "│", STYLE_BORDER)

    # -- left column: agents + status + consensus --------------------------
    row = inner_top + 1
    canvas.separator(row, 1, left_w - 1, "AGENTS")
    row += 1
    for text, style in _agent_rows(state, left_w - 3):
        if row >= inner_bottom - 9:
            break
        canvas.put(2, row, text, style)
        row += 1

    row = max(row, inner_bottom - 9)
    canvas.separator(row, 1, left_w - 1, "STATUS")
    row += 1
    canvas.put(2, row, f"round {state.round_no} · msgs {len(state.feed)}", STYLE_VALUE)
    row += 1
    canvas.put(2, row, "activity " + (sparkline(round_counts(state.feed), left_w - 12) or "…"), STYLE_WARN)
    row += 1
    canvas.put(2, row, f"last  {state.status}"[: left_w - 3], STYLE_VALUE)
    row += 1
    canvas.separator(row, 1, left_w - 1, "CONSENSUS")
    row += 1
    for text, style in _consensus_rows(state, left_w - 3):
        if row >= inner_bottom:
            break
        canvas.put(2, row, text, style)
        row += 1

    # -- transcript panel --------------------------------------------------
    visible = max(1, body_h - 1)
    all_rows = _transcript_lines(state, right_w - 4)
    if state.show_help:
        all_rows = [
            ("KEYS", STYLE_TITLE),
            ("q / Esc      quit", STYLE_VALUE),
            ("↑ ↓ PgUp PgDn scroll transcript", STYLE_VALUE),
            ("Home / End   jump to top / bottom", STYLE_VALUE),
            ("f            follow new messages", STYLE_VALUE),
            ("r            reset scroll", STYLE_VALUE),
            ("?            close this help", STYLE_VALUE),
        ]
    if state.follow or state.scroll <= 0:
        window = all_rows[-visible:]
    else:
        end = max(0, len(all_rows) - state.scroll)
        start = max(0, end - visible)
        window = all_rows[start:end]
    for index, (text, style) in enumerate(window):
        canvas.put(right_x + 1, inner_top + 1 + index, text[: right_w - 2], style)

    # -- footer: keys on the left, workspace on the right -------------------
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[state.spinner % 10]
    hint = f" q quit · ↑↓ scroll · f follow · ? help · {state.status} {spinner} "
    canvas.put(0, foot_sep, "├" + "─" * (width - 2) + "┤", STYLE_BORDER)
    canvas.put(0, foot_sep + 1, "│", STYLE_BORDER)
    canvas.put(width - 1, foot_sep + 1, "│", STYLE_BORDER)
    canvas.put(2, foot_sep + 1, hint[: width - 4], STYLE_DIM)
    if state.workspace:
        room = width - len(hint) - 4
        if room > 12:
            path = state.workspace
            shown = path if len(path) <= room else "…" + path[-(room - 1):]
            canvas.put(width - len(shown) - 2, foot_sep + 1, shown, STYLE_DIM)
    canvas.put(0, height - 1, "└" + "─" * (width - 2) + "┘", STYLE_BORDER)
    return canvas


class CursesDashboard(BaseUi):
    """Paints :func:`render` on the terminal and handles keys/scroll."""

    def __init__(self, stream: Optional[TextIO] = None, *, fallback: Optional[BaseUi] = None) -> None:
        super().__init__(stream)
        self.state = DashboardState()
        self._screen: Any = None
        self._active = False
        self._task: Optional[asyncio.Task] = None
        self._quit = asyncio.Event()
        self._fallback = fallback or PlainUi(stream)
        self._last_size = (0, 0)

    # -- lifecycle ---------------------------------------------------------
    def header(self, task: str, workspace: str, session: Optional[str], agents: dict[str, Any]) -> None:
        self.state.task = task.strip()
        self.state.workspace = workspace
        self.state.session = session
        self.state.agents = dict(agents)
        self.state.started = time.monotonic()

    def start_animation(self, label: str = "waiting for the next turn") -> Any:
        if not self._init_screen():
            return None
        self.state.status = label
        self._task = asyncio.create_task(self._loop())
        return self._task

    def stop_animation(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._shutdown_screen()

    def on_message(self, *, session_id: str, round_no: int, kind: str, sender: str,
                   recipient: str, body: str, at: float) -> None:
        if not self._active:
            if self._fallback.session_id is None:
                self._fallback.session_id = session_id
            self._fallback.on_message(session_id=session_id, round_no=round_no, kind=kind,
                                      sender=sender, recipient=recipient, body=body, at=at)
            return
        if self.session_id is None:
            self.session_id = session_id
        if session_id != self.session_id:
            return
        if round_no and round_no != self.state.round_no:
            self.state.round_no = round_no
            self.state.feed.append({"kind": "round", "round": round_no, "at": at})
        self.messages += 1
        self.state.feed.append({
            "kind": kind, "sender": sender, "recipient": recipient,
            "body": body, "round": round_no, "at": at,
        })
        self.state.status = f"{sender} → {recipient}"

    def summary(self, result: TeamResult) -> None:
        if not self._active:
            self._fallback.summary(result)
            return
        self.state.result = result
        self.state.status = result.status
        self.state.session = result.session_id
        self.state.follow = True

    async def wait_close(self, timeout: Optional[float] = None) -> None:
        """Keep the final dashboard on screen until the user presses q/Esc."""
        if not self._active:
            return
        try:
            await asyncio.wait_for(self._quit.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        self.stop_animation()

    # -- rendering ---------------------------------------------------------
    def write(self, text: str) -> None:  # messages go to the dashboard, not stdout
        if not self._active:
            self._fallback.write(text)

    def status(self, text: str) -> None:
        if not self._active:
            self._fallback.status(text)
            return
        self.state.status = text

    def clear_status(self) -> None:
        if not self._active:
            self._fallback.clear_status()

    def _init_screen(self) -> bool:
        if self._active:
            return True
        try:
            os.environ.setdefault("ESCDELAY", "25")
            try:
                locale.setlocale(locale.LC_ALL, "")
            except locale.Error:
                pass
            self._screen = curses.initscr()
            curses.noecho()
            curses.cbreak()
            self._screen.keypad(True)
            self._screen.nodelay(True)
            curses.start_color()
            curses.use_default_colors()
            self._init_colors()
            curses.curs_set(0)
            self._active = True
            return True
        except Exception as error:  # no TERM, dumb term, not a tty…
            print(f"(dashboard unavailable: {error}) falling back to plain output", file=sys.stderr)
            self._active = False
            return False

    def _init_colors(self) -> None:
        palette = {
            STYLE_BORDER: curses.COLOR_BLUE,
            STYLE_TITLE: curses.COLOR_YELLOW,
            STYLE_DIM: curses.COLOR_WHITE,
            STYLE_OK: curses.COLOR_GREEN,
            STYLE_ERR: curses.COLOR_RED,
            STYLE_WARN: curses.COLOR_YELLOW,
            STYLE_VALUE: curses.COLOR_WHITE,
            STYLE_SENDER: curses.COLOR_CYAN,
            STYLE_BADGE: curses.COLOR_MAGENTA,
        }
        self._pairs = {}
        for index, (style, color) in enumerate(palette.items(), start=1):
            try:
                curses.init_pair(index, color, -1)
            except curses.error:
                continue
            self._pairs[style] = curses.color_pair(index)
        self._pairs[STYLE_DIM] = (self._pairs.get(STYLE_DIM, 0) | curses.A_DIM)
        self._pairs[STYLE_TITLE] = (self._pairs.get(STYLE_TITLE, 0) | curses.A_BOLD)

    def _draw(self) -> None:
        if not self._active:
            return
        height, width = self._screen.getmaxyx()
        self._last_size = (height, width)
        self.state.spinner += 1
        canvas = render(self.state, width, height)
        try:
            self._screen.erase()
            for row in range(height):
                line = "".join(canvas.chars[row])
                styles = canvas.styles[row]
                column = 0
                while column < width:
                    style = styles[column]
                    end = column
                    while end < width and styles[end] == style:
                        end += 1
                    chunk = line[column:end]
                    if chunk.strip() or style:
                        try:
                            self._screen.addstr(row, column, chunk, self._pairs.get(style or STYLE_VALUE, 0))
                        except curses.error:
                            pass
                    column = end
            self._screen.refresh()
        except curses.error:
            pass

    def _handle_key(self, key: int) -> None:
        page = max(1, (self._last_size[0] or 24) - 6)
        if key in (ord("q"), ord("Q"), 27):
            self._quit.set()
        elif key in (curses.KEY_UP, ord("k")):
            self.state.follow = False
            self.state.scroll += 1
        elif key in (curses.KEY_DOWN, ord("j")):
            self.state.scroll = max(0, self.state.scroll - 1)
            if self.state.scroll == 0:
                self.state.follow = True
        elif key == curses.KEY_PPAGE:
            self.state.follow = False
            self.state.scroll += page
        elif key == curses.KEY_NPAGE:
            self.state.scroll = max(0, self.state.scroll - page)
            if self.state.scroll == 0:
                self.state.follow = True
        elif key == curses.KEY_HOME:
            self.state.follow = False
            self.state.scroll = 10_000
        elif key == curses.KEY_END:
            self.state.scroll = 0
            self.state.follow = True
        elif key in (ord("f"), ord("F")):
            self.state.follow = True
            self.state.scroll = 0
        elif key in (ord("r"), ord("R")):
            self.state.scroll = 0
            self.state.follow = True
        elif key == ord("?"):
            self.state.show_help = not self.state.show_help

    async def _loop(self) -> None:
        try:
            while not self._quit.is_set():
                key = -1
                for _ in range(8):
                    key = self._screen.getch()
                    if key == -1:
                        break
                    self._handle_key(key)
                self._draw()
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        finally:
            self._draw()

    def _shutdown_screen(self) -> None:
        if not self._active:
            return
        try:
            self._screen.keypad(False)
            curses.nocbreak()
            curses.echo()
            curses.curs_set(1)
            curses.endwin()
        except Exception:
            pass
        self._active = False
