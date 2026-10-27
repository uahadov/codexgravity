"""
Project decision memory: ``.codex-gravity/decisions.md``.

Accepted sessions append a short entry; later sessions read the recent entries and
hand them to the agents, so the team does not re-litigate settled decisions.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterable, Optional

from codex_gravity.team.protocol import TeamError

DECISIONS_DIR = ".codex-gravity"
DECISIONS_FILENAME = "decisions.md"
MAX_ENTRIES = 8
MAX_ENTRY_CHARS = 900


def decisions_path(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / DECISIONS_DIR / DECISIONS_FILENAME


def _split_entries(text: str) -> list[str]:
    entries = re.split(r"(?m)^##\s+", text)
    return [entry.strip() for entry in entries if entry.strip()]


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def proposal_points(proposal: str, limit: int = 3) -> list[str]:
    """The most decision-like lines of a proposal (bullets, numbered steps)."""
    points: list[str] = []
    for raw in (proposal or "").splitlines():
        line = raw.strip().lstrip("-*•").strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^\d+[.)]\s+", line):
            line = re.sub(r"^\d+[.)]\s+", "", line)
        points.append(_clip(line, 160))
        if len(points) >= limit:
            break
    return points


def load_decisions(workspace: str | Path, limit: int = MAX_ENTRIES) -> str:
    """Recent decision entries as prompt-ready text (empty when none)."""
    path = decisions_path(workspace)
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    entries = _split_entries(text)[-max(1, limit):]
    return "\n\n".join(entries)


def append_decision(
    workspace: str | Path,
    *,
    task: str,
    session_id: str,
    status: str,
    approvals: Iterable[str],
    proposal: str,
    verification: Optional[str] = None,
    when: Optional[float] = None,
) -> Path:
    """Append one dated entry; returns the decisions file path."""
    path = decisions_path(workspace)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d", time.localtime(when or time.time()))
    approved = ", ".join(sorted(approvals)) or "—"
    header = f"## {stamp} · {_clip(task, 80)}"
    body = [
        header,
        f"- session `{session_id}` · status **{status}** · approved by {approved}"
        + (f" · `{verification}`" if verification else ""),
    ]
    points = proposal_points(proposal)
    body += [f"- {point}" for point in points]
    entry = "\n".join(body)[: MAX_ENTRY_CHARS + 200].rstrip() + "\n\n"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(entry)
    except OSError as error:
        raise TeamError(f"Cannot write {path}: {error}") from error
    return path


def decisions_count(workspace: str | Path) -> int:
    path = decisions_path(workspace)
    if not path.is_file():
        return 0
    try:
        return len(_split_entries(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return 0
