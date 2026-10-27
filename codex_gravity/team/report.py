"""
Session report: one self-contained HTML file per session.

    codex-gravity-report -w .                 # latest session
    codex-gravity-report -w . --session 9f3c…# writes .codex-gravity/team/<id>/report.html

Paper-collage styling, no external assets, no scripts: safe to attach to a PR,
send to a colleague or open offline. Works for team sessions and diff reviews.
"""

from __future__ import annotations

import argparse
import html
import json
import time
from pathlib import Path
from typing import Any, Optional

from codex_gravity.team.protocol import Kind, TeamTranscript

BADGES = {
    "task": "TASK", "proposal": "PLAN", "critique": "CRIT", "question": "ASK",
    "answer": "ANS", "revision": "REV", "approval": "PASS", "rejection": "FAIL",
    "note": "NOTE", "consensus": "DONE", "escalation": "ESC",
    "verification": "TEST", "status": "..",
}

STATUS_TONE = {
    "verified": "ok", "consensus": "ok", "approved": "ok",
    "changes_requested": "bad", "failed": "bad", "deadlocked": "bad",
    "unavailable": "warn", "budget_exhausted": "warn", "cancelled": "warn",
}

TONE_COLOR = {"ok": "#1f7a4d", "bad": "#b3261e", "warn": "#b26a00", "idle": "#6f6657"}


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def latest_session(workspace: Path) -> Optional[str]:
    base = workspace / ".codex-gravity" / "team"
    if not base.is_dir():
        return None
    sessions = [p for p in base.iterdir() if p.is_dir() and p.name != "tmp"]
    if not sessions:
        return None
    return max(sessions, key=lambda p: p.stat().st_mtime).name


def load_result(workspace: Path, session_id: str) -> dict[str, Any]:
    path = workspace / ".codex-gravity" / "team" / session_id / "result.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _title(result: dict[str, Any], messages: list) -> str:
    for key in ("task", "title"):
        if result.get(key):
            return str(result[key])
    for message in messages:
        if message.kind is Kind.TASK:
            return (message.body or "").splitlines()[0][:160] if message.body else "Session"
    return "Session report"


def _blockers(result: dict[str, Any]) -> list[str]:
    decision = result.get("decision") or {}
    return [str(item) for item in (decision.get("blockers") or [])]


def _approvals(result: dict[str, Any]) -> list[str]:
    decision = result.get("decision") or {}
    return [str(item) for item in (decision.get("approvals") or [])]


def _findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("findings", "reviews"):
        items = result.get(key)
        if isinstance(items, list) and items:
            return [item for item in items if isinstance(item, dict)]
    return []


def render_session_html(
    workspace: str | Path,
    session_id: str,
    *,
    limit: int = 500,
    body_limit: int = 4000,
) -> str:
    root = Path(workspace).expanduser().resolve()
    transcript = TeamTranscript(root, session_id)
    messages = transcript.messages()
    result = load_result(root, session_id)

    status = str(result.get("status") or ("incomplete" if messages else "unknown"))
    tone = STATUS_TONE.get(status, "idle")
    color = TONE_COLOR[tone]
    title = _title(result, messages)
    decision = result.get("decision") or {}
    verification = result.get("verification") or {}
    findings = _findings(result)
    blockers = _blockers(result)
    approvals = _approvals(result)

    meta = [
        f"{len(messages)} messages",
        f"rounds {result.get('rounds', '—')}",
        f"{result.get('elapsed_seconds', '—')}s",
        f"session {session_id}",
    ]
    if result.get("diff_chars"):
        meta.append(f"diff {result['diff_chars']} chars")

    parts: list[str] = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>{_esc(title)} — Codex-Gravity</title>",
        "<style>",
        """
:root{--paper:#f4f0e9;--ink:#14120f;--muted:#6f6657;--line:rgba(20,18,15,.13);--card:#fffdf8}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--paper);color:var(--ink);font:16px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
     padding:48px 24px 72px;background-image:linear-gradient(to right,rgba(20,18,15,.04) 1px,transparent 1px),
     linear-gradient(to bottom,rgba(20,18,15,.04) 1px,transparent 1px);background-size:64px 64px}
.wrap{max-width:900px;margin:0 auto}
.eyebrow{font-size:11.5px;letter-spacing:.18em;text-transform:uppercase;color:#7a7264}
h1{font-size:44px;line-height:1.02;letter-spacing:-.03em;margin:14px 0 10px;font-weight:800}
.meta{font-size:12.5px;color:var(--muted);letter-spacing:.04em}
.rule{height:1px;background:var(--line);margin:26px 0}
.verdict{border:1px solid var(--line);background:var(--card);border-left:6px solid currentColor;
         padding:20px 22px;box-shadow:0 24px 40px -30px rgba(40,30,15,.5)}
.status{font-size:13px;letter-spacing:.16em;text-transform:uppercase;font-weight:700;color:currentColor}
.reason{margin-top:8px;font-size:18px;font-weight:600}
.sub{font-size:13.5px;color:var(--muted);margin-top:8px}
ul{margin:10px 0 0 18px}li{margin:4px 0;font-size:14px}
.verify{border:1px solid var(--line);background:#0d0c0b;color:#f7f3ea;padding:18px 20px;margin-top:18px}
.verify .cmd{font:13.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:#c9c1b2}
.verify .exit{font-size:26px;font-weight:800;letter-spacing:-.02em;margin-top:6px}
.verify pre{margin-top:12px;font:12px/1.5 ui-monospace,Menlo,monospace;color:#a9a193;white-space:pre-wrap;max-height:220px;overflow:auto}
table{width:100%;border-collapse:collapse;margin-top:18px;font-size:13.5px;background:var(--card);border:1px solid var(--line)}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.msg{display:grid;grid-template-columns:104px 74px 1fr;gap:12px;padding:12px 0;border-bottom:1px solid var(--line)}
.time{font:11.5px/1.6 ui-monospace,Menlo,monospace;color:var(--muted)}
.badge{font:11px/1.6 ui-monospace,Menlo,monospace;letter-spacing:.1em;color:#7a3f12}
.who{font-size:12.5px;color:var(--muted);letter-spacing:.03em}
.body{grid-column:1/-1;font:13px/1.6 ui-monospace,Menlo,monospace;white-space:pre-wrap;margin-top:6px;
      background:#fbf8f1;border:1px solid var(--line);padding:10px 12px}
footer{margin-top:36px;font-size:11.5px;color:#8b8272;letter-spacing:.05em}
""",
        "</style></head><body><div class=\"wrap\">",
        f"<div class=\"eyebrow\">Codex-Gravity · session report · {_esc(time.strftime('%Y-%m-%d'))}</div>",
        f"<h1>{_esc(title)}</h1>",
        f"<div class=\"meta\">{_esc(' · '.join(meta))}</div>",
        "<div class=\"rule\"></div>",
        f"<div class=\"verdict\" style=\"color:{color}\">",
        f"<div class=\"status\">{_esc(status.replace('_', ' '))}</div>",
        f"<div class=\"reason\">{_esc(decision.get('reason') or 'No decision was recorded.')}</div>",
    ]
    if approvals:
        parts.append(f"<div class=\"sub\">Approved by {_esc(', '.join(approvals))}</div>")
    if blockers:
        parts.append("<div class=\"sub\">Blocking issues</div><ul>")
        parts += [f"<li>{_esc(_clip(item, 500))}</li>" for item in blockers[:20]]
        parts.append("</ul>")
    parts.append("</div>")

    if findings:
        parts.append("<table><thead><tr><th>reviewer</th><th>verdict</th><th>summary</th></tr></thead><tbody>")
        for finding in findings[:20]:
            verdict = str(finding.get("verdict", ""))
            summary = finding.get("summary") or finding.get("error") or ""
            parts.append(
                f"<tr><td>{_esc(finding.get('reviewer', '?'))}</td>"
                f"<td>{_esc(verdict)}</td><td>{_esc(_clip(str(summary), 300))}</td></tr>"
            )
        parts.append("</tbody></table>")

    if verification:
        passed = bool(verification.get("passed"))
        exit_color = TONE_COLOR["ok"] if passed else TONE_COLOR["bad"]
        output = (verification.get("stdout_tail") or "") + ("\n" + verification.get("stderr_tail", "") if verification.get("stderr_tail") else "")
        parts += [
            "<div class=\"verify\">",
            "<div class=\"eyebrow\" style=\"color:#f26419\">verification</div>",
            f"<div class=\"cmd\">$ {_esc(verification.get('command', ''))}</div>",
            f"<div class=\"exit\" style=\"color:{exit_color}\">exit {_esc(verification.get('exit_code'))}"
            f" <span style=\"font-size:13px;color:#8a8172\">{_esc(verification.get('duration_seconds', ''))}s</span></div>",
        ]
        if output.strip():
            parts.append(f"<pre>{_esc(_clip(output.strip(), 4000))}</pre>")
        parts.append("</div>")

    parts.append("<div class=\"rule\"></div><div class=\"eyebrow\">transcript</div>")
    shown = messages[-max(1, limit):]
    if len(messages) > len(shown):
        parts.append(f"<div class=\"meta\">showing the last {len(shown)} of {len(messages)} messages</div>")
    for message in shown:
        stamp = time.strftime("%H:%M:%S", time.localtime(message.created_at))
        badge = BADGES.get(message.kind.value, message.kind.value.upper()[:4])
        body = _clip(message.body or "", body_limit)
        parts += [
            "<div class=\"msg\">",
            f"<div class=\"time\">r{message.round} · {_esc(stamp)}</div>",
            f"<div class=\"badge\">{_esc(badge)}</div>",
            f"<div class=\"who\">{_esc(message.sender)} → {_esc(message.recipient)}</div>",
            f"<div class=\"body\">{_esc(body)}</div>",
            "</div>",
        ]

    parts += [
        f"<footer>Generated by Codex-Gravity · session {_esc(session_id)} · "
        f"{_esc(str(result.get('transcript_messages', len(messages))))} messages · "
        "this is a review gate, not a merge.</footer>",
        "</div></body></html>",
    ]
    return "\n".join(parts)


def write_report(
    workspace: str | Path,
    session_id: str,
    output: Optional[str | Path] = None,
    *,
    limit: int = 500,
) -> Path:
    root = Path(workspace).expanduser().resolve()
    page = render_session_html(root, session_id, limit=limit)
    target = Path(output).expanduser() if output else root / ".codex-gravity" / "team" / session_id / "report.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codex-gravity-report",
        description="Render a team session (or diff review) as one self-contained HTML file.",
    )
    parser.add_argument("-w", "--workspace", default=".", help="Project directory")
    parser.add_argument("--session", default=None, help="12-hex session id (default: latest)")
    parser.add_argument("-o", "--output", default=None, help="Output path (default: next to the transcript)")
    parser.add_argument("--limit", type=int, default=500, help="Maximum transcript messages to include")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    session_id = args.session or latest_session(workspace)
    if not session_id:
        print(f"No team sessions under {workspace}/.codex-gravity/team", flush=True)
        raise SystemExit(2)
    path = write_report(workspace, session_id, args.output, limit=max(1, args.limit))
    print(f"wrote {path}")
