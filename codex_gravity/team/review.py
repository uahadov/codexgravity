"""
Review a diff with two independent agents — the "gate" mode.

    codex-gravity-review --base origin/main --test-cmd "pytest -q"

Each reviewer returns the same structured verdict used by the team engine
(``approve``/``revise``/``reject`` with blocking issues). Nothing is approved
without a parsed approval, and an optional command is really executed.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from codex_gravity.event_bus import EventBus
from codex_gravity.team.agents import AgentTurn, TeamAgent, default_agents
from codex_gravity.team.consensus import (
    ConsensusDecision,
    ReviewFinding,
    Verdict,
    diff_review_prompt,
    parse_review,
    tally,
)
from codex_gravity.team.protocol import ALL, Kind, Role, TeamChannel, TeamError, TeamTranscript, new_session_id
from codex_gravity.team.session import VerificationRecord, run_verification

MAX_DIFF = 60_000
ReviewStatus = Literal["approved", "changes_requested", "unavailable", "failed"]


class DiffError(TeamError):
    """The diff cannot be collected (not a repo, nothing to review…)."""


class DiffReviewConfig(BaseModel):
    workspace: str
    title: str = "Review this change"
    base: Optional[str] = None            # e.g. "origin/main", "HEAD~1"
    staged: bool = False                  # review the index instead of the worktree
    include_untracked: bool = False
    reviewers: list[str] = Field(default_factory=lambda: ["codex-cli", "antigravity-cli"])
    reviewer_weights: dict[str, float] = Field(default_factory=dict)
    veto_agents: list[str] = Field(default_factory=list)
    require_unanimous: bool = True
    majority_threshold: float = 0.5
    test_command: Optional[str] = None
    timeout: float = 180.0
    max_diff_chars: int = MAX_DIFF
    codex_thread: Optional[str] = None
    session_id: Optional[str] = None

    @field_validator("workspace")
    @classmethod
    def _workspace(cls, value: str) -> str:
        root = Path(value).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("workspace must be an existing absolute directory")
        return str(root.resolve())

    @field_validator("reviewers")
    @classmethod
    def _reviewers(cls, value: list[str]) -> list[str]:
        names = [name.strip() for name in value if name and name.strip()]
        if not names:
            raise ValueError("at least one reviewer is required")
        return list(dict.fromkeys(names))

    @field_validator("timeout")
    @classmethod
    def _timeout(cls, value: float) -> float:
        if not 5 <= value <= 1800:
            raise ValueError("timeout must be between 5 and 1800 seconds")
        return value

    @field_validator("max_diff_chars")
    @classmethod
    def _max_chars(cls, value: int) -> int:
        if not 1000 <= value <= 400_000:
            raise ValueError("max_diff_chars must be between 1000 and 400000")
        return value


class DiffReviewResult(BaseModel):
    status: ReviewStatus
    title: str
    session_id: str
    diff_chars: int = 0
    diff_truncated: bool = False
    findings: list[ReviewFinding] = Field(default_factory=list)
    decision: ConsensusDecision
    verification: Optional[VerificationRecord] = None
    agents: dict[str, dict] = Field(default_factory=dict)
    transcript_path: str = ""
    report_path: Optional[str] = None
    next_actions: list[str] = Field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def exit_code(self) -> int:
        return {"approved": 0, "changes_requested": 1, "failed": 1, "unavailable": 2}[self.status]

    @property
    def blockers(self) -> list[str]:
        return self.decision.blockers


def _git(workspace: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DiffError(f"git failed: {error}") from error
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()[-3:]
        raise DiffError(f"git {' '.join(args)} exited {result.returncode}: {' / '.join(message)}")
    return result.stdout


def collect_diff(
    workspace: str | Path,
    *,
    base: Optional[str] = None,
    staged: bool = False,
    include_untracked: bool = False,
    max_chars: int = MAX_DIFF,
) -> tuple[str, int, bool]:
    """Return ``(diff_text, original_chars, truncated)``."""
    root = Path(workspace).expanduser().resolve()
    if not (root / ".git").exists():
        raise DiffError(f"{root} is not a git repository.")

    args = ["diff", "--no-color", "--no-ext-diff"]
    if staged:
        args.append("--cached")
    if base:
        args.append(base)
    text = _git(root, args)

    if include_untracked:
        untracked = [line for line in _git(root, ["ls-files", "-o", "--exclude-standard"]).splitlines() if line.strip()]
        for name in untracked[:20]:
            path = root / name
            if not path.is_file() or path.stat().st_size > 200_000:
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            text += f"\n--- untracked file: {name} ---\n" + "\n".join(body.splitlines()[:200]) + "\n"

    original = len(text)
    truncated = original > max_chars
    if truncated:
        text = text[:max_chars] + f"\n\n[diff truncated: {original} chars total]\n"
    return text, original, truncated


class DiffReviewer:
    """Runs one review pass over a diff and, optionally, the verification command."""

    def __init__(self, agents: Optional[dict[str, TeamAgent]] = None, event_bus: Optional[EventBus] = None) -> None:
        self._injected = dict(agents or {})
        self.event_bus = event_bus

    def _pool(self, config: DiffReviewConfig) -> dict[str, TeamAgent]:
        pool = default_agents(turn_timeout=config.timeout, codex_thread=config.codex_thread)
        pool.update(self._injected)
        return pool

    async def review(self, config: DiffReviewConfig) -> DiffReviewResult:
        started = time.perf_counter()
        diff, original_chars, truncated = collect_diff(
            config.workspace,
            base=config.base,
            staged=config.staged,
            include_untracked=config.include_untracked,
            max_chars=config.max_diff_chars,
        )
        if not diff.strip():
            raise DiffError(
                "Nothing to review: the selected diff is empty. "
                "Stage changes, pass --base <rev>, or use --untracked."
            )

        pool = self._pool(config)
        reviewers: list[TeamAgent] = []
        for name in config.reviewers:
            agent = pool.get(name)
            if agent is None:
                raise TeamError(f"Unknown reviewer '{name}'. Available: {', '.join(sorted(pool))}.")
            reviewers.append(agent)

        session_id = config.session_id or new_session_id()
        transcript = TeamTranscript(config.workspace, session_id)
        channel = TeamChannel(transcript, self.event_bus)
        descriptions = {name: pool[name].describe() for name in config.reviewers}

        await channel.send(
            "orchestrator", ALL, Kind.TASK,
            f"{config.title}\n\n{len(diff)} chars of diff"
            + (f" (truncated from {original_chars})" if truncated else "")
            + (f"\nreviewers: {', '.join(config.reviewers)}" if config.reviewers else ""),
            subject="diff review",
        )

        findings: list[ReviewFinding] = []
        for reviewer in reviewers:
            turn = AgentTurn(
                task=config.title,
                instruction=diff_review_prompt(config.title, diff, config.test_command),
                context=transcript.render(),
                workspace=transcript.workspace,
                role=Role.REVIEWER,
                round=1,
                session_id=session_id,
                timeout=config.timeout,
            )
            reply = await reviewer.respond(turn)
            finding = parse_review(
                reply.text if reply.ok else "",
                reviewer.name,
                weight=float(config.reviewer_weights.get(reviewer.name, 1.0)),
                veto=reviewer.name in config.veto_agents,
            )
            if not reply.ok:
                finding.verdict = Verdict.UNAVAILABLE
                finding.error = reply.error
                await channel.send(
                    reviewer.name, ALL, Kind.STATUS,
                    f"Reviewer '{reviewer.name}' unavailable: {reply.error}",
                    round=1, role=Role.SYSTEM,
                )
            else:
                kind = Kind.APPROVAL if finding.verdict == Verdict.APPROVE else (
                    Kind.REJECTION if finding.verdict == Verdict.REJECT else Kind.CRITIQUE
                )
                body = f"verdict: {finding.verdict.value}"
                if finding.summary:
                    body += f"\n\n{finding.summary}"
                if finding.blocking_issues:
                    body += "\n\nblocking issues:\n" + "\n".join(f"- {item}" for item in finding.blocking_issues)
                await channel.send(
                    reviewer.name, "author", kind, body, round=1, role=Role.REVIEWER,
                    subject=f"{finding.verdict.value} ({reviewer.name})",
                    metadata={"engine": reply.engine, "parsed": finding.parsed},
                )
            findings.append(finding)

        decision = tally(
            findings,
            require_unanimous=config.require_unanimous,
            majority_threshold=config.majority_threshold,
        )

        verification: Optional[VerificationRecord] = None
        if config.test_command:
            verification = await run_verification(config.test_command, transcript.workspace, config.timeout * 2)
            await channel.send(
                "verifier", ALL, Kind.VERIFICATION,
                f"`{config.test_command}` exited {verification.exit_code} "
                f"({'PASS' if verification.passed else 'FAIL'}) in {verification.duration_seconds}s",
                round=1, role=Role.SYSTEM, metadata=verification.model_dump(),
            )

        if all(finding.verdict == Verdict.UNAVAILABLE for finding in findings):
            status: ReviewStatus = "unavailable"
            next_actions = ["Make at least one reviewer reachable (see `codex-gravity-team --check`) and rerun."]
        elif not decision.achieved:
            status = "changes_requested"
            next_actions = ["Fix the blocking issues, then rerun the review."]
        elif verification is not None and not verification.passed:
            status = "failed"
            next_actions = ["The diff was approved but the verification command failed; inspect its output."]
        else:
            status = "approved"
            next_actions = ["Merge when you are ready; this review is a gate, not a merge."]

        body = (
            f"status: {status}\nreviewers: {', '.join(config.reviewers)}\n"
            f"decision: {decision.reason}\n"
            + ("blocking issues:\n" + "\n".join(f"- {item}" for item in decision.blockers) + "\n" if decision.blockers else "")
            + (f"verification: `{verification.command}` exit {verification.exit_code}\n" if verification else "")
        )
        await channel.send(
            "orchestrator", ALL, Kind.CONSENSUS if status == "approved" else Kind.ESCALATION,
            body, round=1, role=Role.ORCHESTRATOR,
            subject="diff review result", metadata={"status": status},
        )

        report_path = None
        header = (
            f"# Diff review — {config.title}\n\n"
            f"- Status: **{status}**\n- Session: `{session_id}`\n"
            f"- Reviewers: {', '.join(config.reviewers)}\n"
            f"- Diff: {len(diff)} chars{'(truncated)' if truncated else ''}\n"
            f"- Decision: {decision.reason}\n"
        )
        report = transcript.write_report("review.md", header + "\n---\n\n" + transcript.to_markdown("Diff review transcript"))
        report_path = str(report)

        return DiffReviewResult(
            status=status,
            title=config.title,
            session_id=session_id,
            diff_chars=original_chars,
            diff_truncated=truncated,
            findings=findings,
            decision=decision,
            verification=verification,
            agents=descriptions,
            transcript_path=str(transcript.path),
            report_path=report_path,
            next_actions=next_actions,
            elapsed_seconds=round(time.perf_counter() - started, 2),
        )


def default_test_command(workspace: str | Path) -> Optional[str]:
    """A cheap, real default: syntax-check Python files changed in the diff."""
    root = Path(workspace).expanduser().resolve()
    try:
        staged = _git(root, ["diff", "--name-only", "--diff-filter=ACM"])
    except DiffError:
        return None
    files = [line for line in staged.splitlines() if line.endswith(".py") and (root / line).is_file()]
    if not files:
        return None
    quoted = " ".join(f"'{name}'" for name in files[:20])
    return f'"{sys.executable}" -m py_compile {quoted}'
