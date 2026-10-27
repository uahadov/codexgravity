"""
Team orchestration: plan → adversarial review → cross-examination → revision →
consensus → optional real verification.

The loop is symmetric: every agent can be a planner, implementer, reviewer or
judge, and reviewers can send questions back to the implementer (and receive
answers) before approving. Nothing is reported as approved unless a structured
approval actually arrived.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from codex_gravity.event_bus import EventBus
from codex_gravity.team.agents import AgentReply, AgentTurn, TeamAgent, default_agents
from codex_gravity.team.consensus import (
    ConsensusDecision,
    ReviewFinding,
    Verdict,
    answer_prompt,
    judge_prompt,
    parse_review,
    proposal_prompt,
    review_prompt,
    revision_prompt,
    tally,
    verification_prompt,
)
from codex_gravity.team.memory import append_decision, load_decisions
from codex_gravity.team.protocol import (
    ALL,
    Kind,
    Role,
    TeamChannel,
    TeamError,
    TeamMessage,
    TeamTranscript,
    new_session_id,
)

TeamStatus = Literal[
    "consensus", "verified", "deadlocked", "unavailable", "failed", "cancelled", "budget_exhausted",
]


class TeamConfig(BaseModel):
    """Everything a team run needs. All fields are validated before any turn."""

    task: str
    workspace: str
    plan_with: str = "codex"
    implement_with: str = "antigravity"
    review_with: list[str] = Field(default_factory=lambda: ["codex", "antigravity"])
    judge_with: Optional[str] = None
    # Exact existing ChatGPT/Codex desktop chat UUID used by the "codex-gui" agent.
    codex_thread: Optional[str] = None
    reviewer_weights: dict[str, float] = Field(default_factory=dict)
    veto_agents: list[str] = Field(default_factory=list)
    max_rounds: int = 3
    require_unanimous: bool = True
    majority_threshold: float = 0.5
    max_questions: int = 2
    turn_timeout: float = 180.0
    human_notes: list[str] = Field(default_factory=list)
    test_command: Optional[str] = None
    verification_retries: int = 1
    verification_timeout: float = 300.0
    session_id: Optional[str] = None
    write_report: bool = True
    # Run budget: 0 means unlimited. Exhaustion stops the run without a verdict.
    max_agent_calls: int = 0
    budget_minutes: float = 0.0
    remember_decisions: bool = True

    @field_validator("task")
    @classmethod
    def _task(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("task must not be empty")
        if len(text) > 100_000:
            raise ValueError("task must be under 100000 characters")
        return text

    @field_validator("workspace")
    @classmethod
    def _workspace(cls, value: str) -> str:
        root = Path(value).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("workspace must be an existing absolute directory")
        return str(root.resolve())

    @field_validator("review_with")
    @classmethod
    def _reviewers(cls, value: list[str]) -> list[str]:
        names = [name.strip() for name in value if name and name.strip()]
        if not names:
            raise ValueError("review_with needs at least one reviewer")
        return list(dict.fromkeys(names))

    @field_validator("max_rounds")
    @classmethod
    def _rounds(cls, value: int) -> int:
        if not 1 <= value <= 10:
            raise ValueError("max_rounds must be between 1 and 10")
        return value

    @field_validator("turn_timeout")
    @classmethod
    def _timeout(cls, value: float) -> float:
        if not 5 <= value <= 1800:
            raise ValueError("turn_timeout must be between 5 and 1800 seconds")
        return value

    @field_validator("verification_retries")
    @classmethod
    def _retries(cls, value: int) -> int:
        if not 0 <= value <= 3:
            raise ValueError("verification_retries must be between 0 and 3")
        return value

    @field_validator("max_agent_calls")
    @classmethod
    def _calls(cls, value: int) -> int:
        if not 0 <= value <= 500:
            raise ValueError("max_agent_calls must be between 0 (unlimited) and 500")
        return value

    @field_validator("budget_minutes")
    @classmethod
    def _budget(cls, value: float) -> float:
        if not 0 <= value <= 600:
            raise ValueError("budget_minutes must be between 0 (unlimited) and 600")
        return value


class VerificationRecord(BaseModel):
    command: str
    exit_code: int
    passed: bool
    duration_seconds: float
    stdout_tail: str = ""
    stderr_tail: str = ""
    timed_out: bool = False
    isolation: str = "workspace (not a sandbox worktree)"


class TeamResult(BaseModel):
    session_id: str
    status: TeamStatus
    rounds: int
    task: str
    workspace: str
    proposal: str
    decision: ConsensusDecision
    reviews: list[ReviewFinding] = Field(default_factory=list)
    verification: Optional[VerificationRecord] = None
    agents: dict[str, dict] = Field(default_factory=dict)
    transcript_messages: int = 0
    transcript_path: str = ""
    report_path: Optional[str] = None
    decision_memory: Optional[str] = None
    next_actions: list[str] = Field(default_factory=list)
    elapsed_seconds: float = 0.0


async def run_verification(command: str, workspace: Path, timeout: float) -> VerificationRecord:
    """Run the verification command in the workspace with process-group isolation."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key, value in os.environ.items():
        if key in ("USER", "SHELL", "TERM", "VIRTUAL_ENV", "PYTHONPATH") or key.startswith(("GIT_", "PYTEST_")):
            env[key] = value

    start = time.perf_counter()
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
        return VerificationRecord(
            command=command,
            exit_code=process.returncode or 0,
            passed=process.returncode == 0,
            duration_seconds=round(time.perf_counter() - start, 3),
            stdout_tail="\n".join(out.decode("utf-8", "replace").splitlines()[-20:]),
            stderr_tail="\n".join(err.decode("utf-8", "replace").splitlines()[-20:]),
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except Exception:
            pass
        return VerificationRecord(
            command=command,
            exit_code=-1,
            passed=False,
            duration_seconds=round(time.perf_counter() - start, 3),
            timed_out=True,
            stderr_tail=f"Command timed out after {timeout:.0f}s",
        )


def _format_finding(finding: ReviewFinding) -> str:
    parts = [f"verdict: {finding.verdict.value}"]
    if finding.summary:
        parts.append(finding.summary)
    if finding.blocking_issues:
        parts.append("blocking issues:\n" + "\n".join(f"- {item}" for item in finding.blocking_issues))
    if finding.questions:
        parts.append("questions:\n" + "\n".join(f"- {item}" for item in finding.questions))
    if not finding.parsed:
        parts.append("(structured verdict could not be parsed; treated as revise)")
    return "\n\n".join(parts)


class _BudgetedAgent:
    """Agent proxy that charges every turn against the run budget.

    Exhaustion is not an exception and not a fake answer: the turn fails with an
    explicit error, every later turn fails the same way, and the run ends as
    ``budget_exhausted`` instead of pretending a review happened.
    """

    def __init__(
        self,
        agent: TeamAgent,
        budget: dict,
        max_calls: int,
        deadline: Optional[float],
    ) -> None:
        self._agent = agent
        self._budget = budget
        self._max_calls = max_calls
        self._deadline = deadline

    @property
    def name(self) -> str:
        return self._agent.name

    def describe(self) -> dict:
        return self._agent.describe()

    async def respond(self, turn: AgentTurn) -> AgentReply:
        if self._budget["stopped"]:
            return AgentReply(ok=False, error=self._budget["stopped"], engine=self.name)
        reason = ""
        if self._max_calls and self._budget["calls"] >= self._max_calls:
            reason = f"call budget exhausted ({self._max_calls} agent calls)"
        elif self._deadline is not None and time.monotonic() > self._deadline:
            reason = "run deadline reached"
        if reason:
            self._budget["stopped"] = reason
            return AgentReply(ok=False, error=reason, engine=self.name)
        self._budget["calls"] += 1
        return await self._agent.respond(turn)


class TeamOrchestrator:
    """Runs a Codex + Antigravity team session and exposes it over CLI/MCP."""

    def __init__(self, agents: Optional[dict[str, TeamAgent]] = None, event_bus: Optional[EventBus] = None) -> None:
        self._injected = dict(agents or {})
        self.event_bus = event_bus
        self._active: dict[tuple[str, str], TeamChannel] = {}

    # -- helpers -----------------------------------------------------------
    def _pool(self, config: TeamConfig) -> dict[str, TeamAgent]:
        pool = default_agents(
            turn_timeout=config.turn_timeout,
            codex_thread=config.codex_thread,
        )
        pool.update(self._injected)
        return pool

    @staticmethod
    def _agent(pool: dict[str, TeamAgent], name: str) -> TeamAgent:
        agent = pool.get(name)
        if agent is None:
            raise TeamError(f"Unknown agent '{name}'. Available: {', '.join(sorted(pool))}.")
        return agent

    @staticmethod
    def _weight(config: TeamConfig, name: str) -> float:
        return float(config.reviewer_weights.get(name, 1.0))

    async def _notes(self, channel: TeamChannel, config: TeamConfig) -> str:
        drained = [m.body for m in channel.drain(ALL)]
        return "\n".join([*config.human_notes, *drained]).strip()

    def _finish(
        self,
        *,
        transcript: TeamTranscript,
        config: TeamConfig,
        status: TeamStatus,
        rounds: int,
        proposal: str,
        decision: ConsensusDecision,
        findings: list[ReviewFinding],
        agents: dict[str, dict],
        verification: Optional[VerificationRecord],
        next_actions: list[str],
        started: float,
        decision_memory: Optional[str] = None,
    ) -> TeamResult:
        report_path = None
        if config.write_report:
            header = (
                f"# Team session {transcript.session_id}\n\n"
                f"- Task: {config.task}\n- Status: **{status}**\n- Rounds: {rounds}\n"
                f"- Decision: {decision.reason}\n"
                f"- Approved by: {', '.join(decision.approvals) or '—'}\n"
                f"- Blocking issues: {len(decision.blockers)}\n"
                f"- Verification: {verification.command if verification else 'not requested'}\n"
            )
            path = transcript.write_report("report.md", header + "\n---\n\n" + transcript.to_markdown())
            report_path = str(path)
        result = TeamResult(
            session_id=transcript.session_id,
            status=status,
            rounds=rounds,
            task=config.task,
            workspace=str(transcript.workspace),
            proposal=proposal,
            decision=decision,
            reviews=findings,
            verification=verification,
            agents=agents,
            transcript_messages=len(transcript.messages()),
            transcript_path=str(transcript.path),
            report_path=report_path,
            decision_memory=decision_memory,
            next_actions=next_actions,
            elapsed_seconds=round(time.perf_counter() - started, 2),
        )
        transcript.write_report("result.json", result.model_dump_json(indent=2))
        return result

    # -- main loop ---------------------------------------------------------
    async def run(self, config: TeamConfig, agents: Optional[dict[str, TeamAgent]] = None) -> TeamResult:
        started = time.perf_counter()
        pool = self._pool(config)
        if agents:
            pool.update(agents)

        planner = self._agent(pool, config.plan_with)
        implementer = self._agent(pool, config.implement_with)
        reviewers = [self._agent(pool, name) for name in config.review_with]

        session_id = config.session_id or new_session_id()
        transcript = TeamTranscript(config.workspace, session_id)
        channel = TeamChannel(transcript, self.event_bus)
        key = (str(transcript.workspace), session_id)
        self._active[key] = channel

        budget: dict = {"calls": 0, "stopped": ""}
        deadline = time.monotonic() + config.budget_minutes * 60 if config.budget_minutes else None

        def charge(agent: TeamAgent) -> _BudgetedAgent:
            return _BudgetedAgent(agent, budget, config.max_agent_calls, deadline)

        planner = charge(planner)
        implementer = charge(implementer)
        reviewers = [charge(reviewer) for reviewer in reviewers]
        decisions_text = load_decisions(transcript.workspace) if config.remember_decisions else ""
        descriptions = {name: agent.describe() for name, agent in pool.items() if name in {
            config.plan_with, config.implement_with, *config.review_with,
            *([config.judge_with] if config.judge_with else []),
        }}

        try:
            await channel.send("orchestrator", ALL, Kind.TASK, config.task, subject="team task")
            for note in config.human_notes:
                await channel.send("human", ALL, Kind.NOTE, note, role=Role.HUMAN)

            # -- round 0: proposal ---------------------------------------
            plan_reply = await planner.respond(
                AgentTurn(
                    task=config.task,
                    instruction=proposal_prompt(config.task, decisions=decisions_text),
                    context=transcript.render(),
                    workspace=transcript.workspace,
                    role=Role.PLANNER,
                    round=0,
                    session_id=session_id,
                    timeout=config.turn_timeout,
                )
            )
            if not plan_reply.ok:
                await channel.send(
                    planner.name, ALL, Kind.STATUS,
                    f"Planner '{planner.name}' unavailable: {plan_reply.error}",
                    role=Role.SYSTEM, metadata={"error": plan_reply.error},
                )
                decision = ConsensusDecision(
                    achieved=False,
                    reason=f"No proposal could be produced: {plan_reply.error}",
                    unavailable=[planner.name],
                )
                return self._finish(
                    transcript=transcript, config=config, status=(
                        "budget_exhausted" if budget["stopped"] else "unavailable"
                    ), rounds=0,
                    proposal="", decision=decision, findings=[], agents=descriptions,
                    verification=None,
                    next_actions=[
                        f"Make '{planner.name}' reachable (see agents in the result) and rerun.",
                        "Nothing was approved; no proposal was invented.",
                    ],
                    started=started,
                )

            proposal = plan_reply.text.strip()
            await channel.send(
                planner.name, ALL, Kind.PROPOSAL, proposal, role=Role.PLANNER,
                subject="initial proposal", metadata={"engine": plan_reply.engine, "duration_ms": plan_reply.duration_ms},
            )

            findings: list[ReviewFinding] = []
            decision = ConsensusDecision(achieved=False, reason="No review round ran yet.")
            rounds = 0
            for round_number in range(1, config.max_rounds + 1):
                rounds = round_number
                if channel.cancelled:
                    break
                notes = await self._notes(channel, config)
                findings = []
                for reviewer in reviewers:
                    turn = AgentTurn(
                        task=config.task,
                        instruction=review_prompt(config.task, proposal, notes, decisions=decisions_text),
                        context=transcript.render(),
                        workspace=transcript.workspace,
                        role=Role.REVIEWER,
                        round=round_number,
                        session_id=session_id,
                        timeout=config.turn_timeout,
                    )
                    reply = await reviewer.respond(turn)
                    finding = parse_review(
                        reply.text if reply.ok else "",
                        reviewer.name,
                        weight=self._weight(config, reviewer.name),
                        veto=reviewer.name in config.veto_agents,
                    )
                    if not reply.ok:
                        finding.verdict = Verdict.UNAVAILABLE
                        finding.error = reply.error
                        await channel.send(
                            reviewer.name, ALL, Kind.STATUS,
                            f"Reviewer '{reviewer.name}' unavailable: {reply.error}",
                            round=round_number, role=Role.SYSTEM,
                        )
                    else:
                        kind = {
                            Verdict.APPROVE: Kind.APPROVAL,
                            Verdict.REJECT: Kind.REJECTION,
                        }.get(finding.verdict, Kind.CRITIQUE)
                        await channel.send(
                            reviewer.name, implementer.name, kind, _format_finding(finding),
                            round=round_number, role=Role.REVIEWER,
                            subject=f"{finding.verdict.value} ({finding.reviewer})",
                            metadata={"engine": reply.engine, "parsed": finding.parsed,
                                      "duration_ms": reply.duration_ms},
                        )
                    findings.append(finding)

                decision = tally(
                    findings,
                    require_unanimous=config.require_unanimous,
                    majority_threshold=config.majority_threshold,
                )
                if decision.achieved or round_number == config.max_rounds:
                    break

                # -- cross-examination: reviewers ask, implementer answers --
                asked = [
                    (finding.reviewer, question)
                    for finding in findings if finding.blocks_consensus
                    for question in finding.questions
                ][: max(0, config.max_questions)]
                answers: list[tuple[str, str]] = []
                for asker, question in asked:
                    question_msg = await channel.send(
                        asker, implementer.name, Kind.QUESTION, question,
                        round=round_number, role=Role.REVIEWER, subject="cross-examination",
                    )
                    answer_reply = await implementer.respond(
                        AgentTurn(
                            task=config.task,
                            instruction=answer_prompt(config.task, question, proposal),
                            context=transcript.render(),
                            workspace=transcript.workspace,
                            role=Role.IMPLEMENTER,
                            round=round_number,
                            session_id=session_id,
                            timeout=config.turn_timeout,
                        )
                    )
                    answer_text = answer_reply.text.strip() if answer_reply.ok else f"(no answer: {answer_reply.error})"
                    await channel.send(
                        implementer.name, asker, Kind.ANSWER, answer_text,
                        round=round_number, role=Role.IMPLEMENTER,
                        reply_to=question_msg.id, correlation_id=question_msg.correlation_id,
                        metadata={"engine": answer_reply.engine},
                    )
                    answers.append((question, answer_text))

                # -- revision -------------------------------------------
                revise_reply = await implementer.respond(
                    AgentTurn(
                        task=config.task,
                        instruction=revision_prompt(
                            config.task, proposal, findings, answers, notes, decisions=decisions_text
                        ),
                        context=transcript.render(),
                        workspace=transcript.workspace,
                        role=Role.IMPLEMENTER,
                        round=round_number,
                        session_id=session_id,
                        timeout=config.turn_timeout,
                    )
                )
                if revise_reply.ok and revise_reply.text.strip():
                    proposal = revise_reply.text.strip()
                    await channel.send(
                        implementer.name, ALL, Kind.REVISION, proposal, round=round_number,
                        role=Role.IMPLEMENTER, subject=f"revision after round {round_number}",
                        metadata={"engine": revise_reply.engine, "duration_ms": revise_reply.duration_ms},
                    )
                else:
                    await channel.send(
                        implementer.name, ALL, Kind.STATUS,
                        f"Revision failed: {revise_reply.error or 'empty revision'}",
                        round=round_number, role=Role.SYSTEM,
                    )

            # -- optional judge on deadlock ------------------------------
            if not decision.achieved and config.judge_with and not budget["stopped"]:
                judge = charge(self._agent(pool, config.judge_with))
                judge_reply = await judge.respond(
                    AgentTurn(
                        task=config.task,
                        instruction=judge_prompt(config.task, findings, transcript.render(limit=20)),
                        context=transcript.render(),
                        workspace=transcript.workspace,
                        role=Role.JUDGE,
                        round=rounds,
                        session_id=session_id,
                        timeout=config.turn_timeout,
                    )
                )
                if judge_reply.ok:
                    ruling = parse_review(
                        judge_reply.text, judge.name, weight=1.0, role=Role.JUDGE
                    )
                    verdict_text = judge_reply.text.strip()[:1200]
                    if '"consensus"' in verdict_text:
                        decision = decision.model_copy(update={
                            "achieved": True,
                            "reason": f"Judge '{judge.name}' resolved the deadlock.",
                        })
                        await channel.send(
                            judge.name, ALL, Kind.CONSENSUS, verdict_text,
                            round=rounds, role=Role.JUDGE, subject="judge ruling",
                        )
                    else:
                        await channel.send(
                            judge.name, ALL, Kind.ESCALATION, verdict_text,
                            round=rounds, role=Role.JUDGE, subject=f"judge ruling ({ruling.verdict.value})",
                        )
                else:
                    await channel.send(
                        judge.name, ALL, Kind.STATUS,
                        f"Judge unavailable: {judge_reply.error}", round=rounds, role=Role.SYSTEM,
                    )

            # -- real verification --------------------------------------
            verification: Optional[VerificationRecord] = None
            if config.test_command and decision.achieved and not budget["stopped"]:
                verification = await run_verification(
                    config.test_command, transcript.workspace, config.verification_timeout
                )
                await channel.send(
                    "verifier", ALL, Kind.VERIFICATION,
                    f"`{config.test_command}` exited {verification.exit_code} "
                    f"({'PASS' if verification.passed else 'FAIL'}) in {verification.duration_seconds}s",
                    round=rounds, role=Role.SYSTEM,
                    metadata=verification.model_dump(),
                )
                attempts = 0
                while not verification.passed and attempts < config.verification_retries:
                    attempts += 1
                    output = f"{verification.stdout_tail}\n{verification.stderr_tail}".strip()
                    repair = await implementer.respond(
                        AgentTurn(
                            task=config.task,
                            instruction=verification_prompt(config.task, config.test_command, output),
                            context=transcript.render(),
                            workspace=transcript.workspace,
                            role=Role.IMPLEMENTER,
                            round=rounds,
                            session_id=session_id,
                            timeout=config.turn_timeout,
                        )
                    )
                    if not repair.ok:
                        break
                    proposal = repair.text.strip()
                    await channel.send(
                        implementer.name, ALL, Kind.REVISION, proposal, round=rounds,
                        role=Role.IMPLEMENTER,
                        subject=f"repair after failed verification (attempt {attempts})",
                    )
                    verification = await run_verification(
                        config.test_command, transcript.workspace, config.verification_timeout
                    )
                    await channel.send(
                        "verifier", ALL, Kind.VERIFICATION,
                        f"re-run exited {verification.exit_code} "
                        f"({'PASS' if verification.passed else 'FAIL'})",
                        round=rounds, role=Role.SYSTEM, metadata=verification.model_dump(),
                    )

            # -- status --------------------------------------------------
            if budget["stopped"]:
                status = "budget_exhausted"
                next_actions = [
                    f"The run stopped before a verdict: {budget['stopped']}.",
                    "Nothing beyond the recorded rounds was agreed; raise the budget or split the task.",
                ]
            elif channel.cancelled:
                status: TeamStatus = "cancelled"
                next_actions = ["The session was cancelled; rerun to reach a decision."]
            elif not decision.achieved:
                status = "deadlocked"
                next_actions = [
                    "Review the blocking issues in the transcript and either change the task or add a judge.",
                    "No approval was claimed; nothing was merged.",
                ]
            elif verification is not None and not verification.passed:
                status = "failed"
                next_actions = [
                    "Verification failed after the repair budget; inspect the recorded output.",
                    "No approval of a passing build is claimed.",
                ]
            else:
                status = "verified" if verification is not None else "consensus"
                next_actions = [
                    "Apply/review the agreed proposal.",
                    "Keep the transcript for audit; it records every review and answer.",
                ]

            # Side notes first, so the verdict is the last line of the transcript.
            if budget["stopped"]:
                await channel.send(
                    "orchestrator", ALL, Kind.STATUS,
                    f"Stopped by budget: {budget['stopped']} (after {budget['calls']} agent calls).",
                    round=rounds, role=Role.SYSTEM,
                )

            memory_path: Optional[str] = None
            if config.remember_decisions and status in ("consensus", "verified"):
                try:
                    memory_path = str(append_decision(
                        transcript.workspace,
                        task=config.task,
                        session_id=session_id,
                        status=status,
                        approvals=decision.approvals,
                        proposal=proposal,
                        verification=config.test_command if verification is not None else None,
                    ))
                    await channel.send(
                        "orchestrator", ALL, Kind.STATUS,
                        f"Decision recorded in {memory_path}",
                        round=rounds, role=Role.SYSTEM,
                    )
                except TeamError as error:
                    await channel.send(
                        "orchestrator", ALL, Kind.STATUS,
                        f"Decision memory not written: {error}", round=rounds, role=Role.SYSTEM,
                    )

            if decision.achieved:
                await channel.send(
                    "orchestrator", ALL, Kind.CONSENSUS,
                    f"{decision.reason}\n\nApproved by: {', '.join(decision.approvals) or '—'}\n"
                    f"Status: {status}",
                    round=rounds, role=Role.ORCHESTRATOR, subject="team consensus",
                    metadata={"status": status},
                )
            else:
                await channel.send(
                    "orchestrator", ALL, Kind.ESCALATION,
                    f"No consensus after {rounds} round(s): {decision.reason}\n\n"
                    + ("\n".join(f"- {item}" for item in decision.blockers) or "- (no structured blockers)"),
                    round=rounds, role=Role.ORCHESTRATOR, subject="escalation to human",
                )

            return self._finish(
                transcript=transcript, config=config, status=status, rounds=rounds,
                proposal=proposal, decision=decision, findings=findings,
                agents=descriptions, verification=verification,
                next_actions=next_actions, started=started, decision_memory=memory_path,
            )
        finally:
            self._active.pop(key, None)

    # -- control surface ---------------------------------------------------
    def status(self, workspace: str, session_id: str) -> dict:
        transcript = TeamTranscript(workspace, session_id)
        messages = transcript.messages()
        if not messages:
            return {"session_id": session_id, "status": "unknown", "messages": 0}
        channel = self._active.get((str(transcript.workspace), session_id))
        last = messages[-1]
        finished = [m for m in messages if m.kind in (Kind.CONSENSUS, Kind.ESCALATION)]
        state = "running" if channel is not None else ("finished" if finished else "incomplete")
        return {
            "session_id": session_id,
            "status": state,
            "messages": len(messages),
            "round": last.round,
            "last": {"kind": last.kind.value, "sender": last.sender, "recipient": last.recipient},
            "decision": (finished[-1].body[:2000] if finished else None),
            "transcript_path": str(transcript.path),
            "workspace": str(transcript.workspace),
        }

    async def send_message(
        self,
        workspace: str,
        session_id: str,
        sender: str,
        recipient: str,
        kind: str,
        body: str,
        *,
        round: int = 0,
    ) -> dict:
        transcript = TeamTranscript(workspace, session_id)
        if not transcript.path.exists():
            raise TeamError("Unknown team session; start it before sending messages.")
        try:
            message_kind = Kind(kind)
        except ValueError:
            raise TeamError(f"Unknown message kind '{kind}'. Use one of: {', '.join(k.value for k in Kind)}.") from None
        role = Role.HUMAN if sender == "human" else Role.ORCHESTRATOR
        channel = self._active.get((str(transcript.workspace), session_id))
        if channel is not None:
            message = await channel.send(sender, recipient, message_kind, body, round=round, role=role)
        else:
            message = TeamMessage(
                session_id=session_id, round=round, kind=message_kind, sender=sender,
                recipient=recipient, role=role, body=body,
            )
            transcript.append(message)
        return {
            "status": "delivered" if channel is not None else "stored",
            "message": json.loads(message.model_dump_json()),
            "note": None if channel is not None else "Session is not running in this process; the message is stored for audit.",
        }

    def cancel(self, workspace: str, session_id: str) -> bool:
        channel = self._active.get((str(Path(workspace).expanduser().resolve()), session_id))
        if channel is None:
            return False
        channel.cancel()
        return True
