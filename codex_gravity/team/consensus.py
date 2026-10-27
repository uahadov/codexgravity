"""
Consensus primitives: structured verdicts, weighted tallying and prompt contracts.

The engine deliberately fails closed. An unparsable, empty or unavailable review
is never counted as approval, and an ``approve`` that still lists blocking
issues is demoted to ``revise``.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Iterable, Optional

from pydantic import BaseModel, Field

from codex_gravity.team.protocol import Role


class Verdict(str, Enum):
    APPROVE = "approve"
    REVISE = "revise"
    REJECT = "reject"
    ABSTAIN = "abstain"
    UNAVAILABLE = "unavailable"


_APPROVE = {"approve", "approved", "accept", "accepted", "lgtm", "ok", "ship", "yes"}
_REVISE = {"revise", "revised", "changes_requested", "change", "request_changes", "needs_work", "rework"}
_REJECT = {"reject", "rejected", "block", "blocked", "no", "deny"}
_ABSTAIN = {"abstain", "skip", "neutral"}


class ReviewFinding(BaseModel):
    reviewer: str
    verdict: Verdict
    summary: str = ""
    blocking_issues: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    parsed: bool = True
    weight: float = 1.0
    veto: bool = False
    error: str = ""
    role: Role = Role.REVIEWER

    @property
    def blocks_consensus(self) -> bool:
        return self.verdict != Verdict.APPROVE


class ConsensusDecision(BaseModel):
    achieved: bool
    reason: str
    approvals: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    dissent: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)
    approval_weight: float = 0.0
    total_weight: float = 0.0


def _dedupe(items: Iterable[str], limit: int = 12) -> list[str]:
    seen: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return seen


def _extract_json(text: str) -> Optional[dict]:
    """Find the first decodable JSON object, preferring fenced blocks."""
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    for start in [i for i, char in enumerate(text) if char == "{"][:40]:
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:index + 1])
                    except ValueError:
                        break
                    if isinstance(data, dict):
                        return data
                    break
    return None


def _normalize_verdict(value: object) -> Optional[Verdict]:
    if isinstance(value, str):
        token = re.sub(r"[^a-z_ ]", "", value.strip().lower())
        if token in _APPROVE:
            return Verdict.APPROVE
        if token in _REVISE:
            return Verdict.REVISE
        if token in _REJECT:
            return Verdict.REJECT
        if token in _ABSTAIN:
            return Verdict.ABSTAIN
    return None


def _as_text_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip(" -*\t") for part in value.splitlines() if part.strip(" -*\t")]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def parse_review(
    raw: str,
    reviewer: str,
    *,
    weight: float = 1.0,
    veto: bool = False,
    role: Role = Role.REVIEWER,
) -> ReviewFinding:
    """
    Turn a model reply into a structured finding.

    Any reply that does not carry a recognizable verdict becomes a ``revise``
    finding that quotes the raw text, so an unreadable answer can never approve
    a change.
    """
    text = (raw or "").strip()
    data = _extract_json(text) if text else None
    if data is None:
        return ReviewFinding(
            reviewer=reviewer,
            verdict=Verdict.REVISE,
            summary="Review could not be parsed as a structured verdict.",
            blocking_issues=[text[:1200]] if text else ["Reviewer returned an empty response."],
            parsed=False,
            weight=weight,
            veto=veto,
            role=role,
            error="unparsed" if text else "empty",
        )

    verdict = _normalize_verdict(data.get("verdict") or data.get("decision") or data.get("status"))
    blockers = _as_text_list(data.get("blocking_issues") or data.get("issues") or data.get("blockers"))
    questions = _as_text_list(data.get("questions") or data.get("open_questions"))[:4]
    summary = str(data.get("summary") or data.get("rationale") or "").strip()

    parsed_ok = True
    if verdict is None:
        verdict = Verdict.REVISE
        parsed_ok = False
        blockers = _dedupe(blockers + ["Reviewer returned an unrecognized verdict."])
    if verdict == Verdict.APPROVE and blockers:
        # Contradiction: an approval that still lists blockers is not an approval.
        verdict = Verdict.REVISE
        parsed_ok = False
        summary = summary or "Approval contained unresolved blocking issues; demoted to revise."

    return ReviewFinding(
        reviewer=reviewer,
        verdict=verdict,
        summary=summary,
        blocking_issues=_dedupe(blockers),
        questions=questions,
        parsed=parsed_ok,
        weight=max(0.0, float(weight)),
        veto=veto,
        role=role,
    )


def tally(
    findings: list[ReviewFinding],
    *,
    require_unanimous: bool = True,
    majority_threshold: float = 0.5,
) -> ConsensusDecision:
    """Aggregate findings into a decision using unanimity or weighted majority."""
    if not findings:
        return ConsensusDecision(achieved=False, reason="No reviewer produced a finding.")

    available = [f for f in findings if f.verdict != Verdict.UNAVAILABLE]
    unavailable = [f.reviewer for f in findings if f.verdict == Verdict.UNAVAILABLE]
    if not available:
        return ConsensusDecision(
            achieved=False,
            reason="No reviewer was available; consensus cannot be claimed.",
            unavailable=unavailable,
        )

    total_weight = round(sum(f.weight for f in available), 4)
    approval_weight = round(sum(f.weight for f in available if f.verdict == Verdict.APPROVE), 4)
    blockers = _dedupe(issue for f in available if f.blocks_consensus for issue in f.blocking_issues)
    questions = _dedupe(question for f in available if f.blocks_consensus for question in f.questions)
    approvals = [f.reviewer for f in available if f.verdict == Verdict.APPROVE]
    dissent = _dedupe(
        f"{f.reviewer}: {f.summary or f.verdict.value}" for f in available if f.blocks_consensus
    )

    vetoed = [f.reviewer for f in available if f.veto and f.blocks_consensus]
    if vetoed:
        return ConsensusDecision(
            achieved=False,
            reason=f"Veto exercised by {', '.join(vetoed)}.",
            approvals=approvals, blockers=blockers, dissent=dissent,
            questions=questions, unavailable=unavailable,
            approval_weight=approval_weight, total_weight=total_weight,
        )

    if require_unanimous:
        achieved = not blockers and len(approvals) == len(available)
        reason = "Unanimous approval." if achieved else "At least one reviewer has not approved."
    else:
        ratio = (approval_weight / total_weight) if total_weight else 0.0
        achieved = not blockers and ratio > majority_threshold
        reason = (
            f"Weighted approval {ratio:.2f} exceeds threshold {majority_threshold:.2f}."
            if achieved else f"Weighted approval {ratio:.2f} does not exceed {majority_threshold:.2f}."
        )

    return ConsensusDecision(
        achieved=achieved,
        reason=reason,
        approvals=approvals,
        blockers=blockers,
        dissent=dissent,
        questions=questions,
        unavailable=unavailable,
        approval_weight=approval_weight,
        total_weight=total_weight,
    )


# ---------------------------------------------------------------------------
# Prompt contracts shared by every agent adapter
# ---------------------------------------------------------------------------

REVIEW_CONTRACT = """Reply with ONE fenced json block and nothing else:
```json
{{"verdict": "approve|revise|reject",
  "summary": "one or two sentences",
  "blocking_issues": ["concrete, testable defect that must be fixed"],
  "questions": ["a question the implementer must answer before approval"]}}
```
Rules: `approve` is only valid when `blocking_issues` is empty. List real
defects (wrong logic, missing error handling, security or test gaps), not style
preferences. If you cannot judge, use `revise` and say what evidence you need."""


def _decisions_block(decisions: str) -> str:
    text = (decisions or "").strip()
    if not text:
        return ""
    return (
        "\nPROJECT DECISIONS ALREADY MADE (.codex-gravity/decisions.md):\n"
        f"{text}\n"
        "Treat these as settled context: do not re-open them unless the task explicitly\n"
        "contradicts them, and say so if it does.\n"
    )


def proposal_prompt(task: str, decisions: str = "") -> str:
    return (
        "You are the planner on a two-agent engineering team (Codex + Antigravity).\n"
        "Produce a concrete, implementable proposal for the task below.\n"
        "Include: the approach, the files/functions to touch, edge cases, the test strategy,\n"
        "and the risks you expect the reviewer to attack.\n"
        "Do not write a final patch; write the plan the team will implement and review.\n"
        f"{_decisions_block(decisions)}\n"
        f"TASK:\n{task}"
    )


def review_prompt(task: str, proposal: str, notes: str = "", decisions: str = "") -> str:
    extra = f"\nHUMAN/TEAM NOTES SINCE THE LAST ROUND:\n{notes}\n" if notes.strip() else ""
    return (
        "You are an independent reviewer on a two-agent engineering team.\n"
        "Be adversarial but fair: your approval is the gate, so do not approve a\n"
        "proposal that cannot be implemented and tested as written.\n"
        f"{REVIEW_CONTRACT}\n"
        f"{_decisions_block(decisions)}"
        f"TASK:\n{task}\n\nPROPOSAL UNDER REVIEW:\n{proposal}{extra}"
    )


def answer_prompt(task: str, question: str, proposal: str) -> str:
    return (
        "You are the implementer on a two-agent engineering team.\n"
        "Answer the reviewer's question precisely. Cite the part of the proposal you\n"
        "would change if the answer reveals a gap. Keep it under 200 words.\n\n"
        f"TASK:\n{task}\n\nREVIEWER QUESTION:\n{question}\n\nCURRENT PROPOSAL:\n{proposal}"
    )


def revision_prompt(
    task: str,
    proposal: str,
    findings: list[ReviewFinding],
    answers: list[tuple[str, str]],
    notes: str = "",
    decisions: str = "",
) -> str:
    blockers = _dedupe(issue for finding in findings if finding.blocks_consensus for issue in finding.blocking_issues)
    dissent = _dedupe(f"{f.reviewer}: {f.summary}" for f in findings if f.blocks_consensus)
    answer_text = "\n".join(f"Q: {q}\nA: {a}" for q, a in answers) or "(none)"
    note_text = notes.strip() or "(none)"
    issues = "\n".join(f"- {item}" for item in blockers) or "- (reviewer gave no structured issue)"
    objections = "\n".join(f"- {item}" for item in dissent) or "- (none)"
    return (
        "You are the implementer on a two-agent engineering team.\n"
        "Revise the proposal so every blocking issue is resolved. Do not silently drop\n"
        "an objection: either fix it or explain in one line why it does not apply.\n"
        "Return the complete revised proposal (no diff, no fences).\n\n"
        f"TASK:\n{task}\n\nCURRENT PROPOSAL:\n{proposal}\n\n"
        f"BLOCKING ISSUES:\n{issues}\n\n"
        f"REVIEWER OBJECTIONS:\n{objections}\n\n"
        f"CROSS-EXAMINATION ANSWERS:\n{answer_text}\n\n"
        f"HUMAN/TEAM NOTES:\n{note_text}"
        f"{_decisions_block(decisions)}"
    )


def judge_prompt(task: str, findings: list[ReviewFinding], transcript: str) -> str:
    return (
        "You are the impartial judge on a two-agent engineering team. The debate reached\n"
        "its round limit without unanimous approval. Decide whether the current proposal\n"
        "may proceed, using only the transcript and the structured findings.\n"
        "Reply with ONE fenced json block:\n"
        '```json\n{"decision": "consensus|deadlock", "rationale": "why", '
        '"must_fix": ["issue"]}\n```\n'
        f"TASK:\n{task}\n\nFINDINGS:\n{json.dumps([f.model_dump(mode='json') for f in findings], ensure_ascii=False, indent=1)}\n\n"
        f"TRANSCRIPT:\n{transcript}"
    )


def verification_prompt(task: str, command: str, output: str) -> str:
    return (
        "You are the implementer on a two-agent engineering team. The team's plan was\n"
        "accepted, but the real verification command failed. Repair the cause, not the test.\n"
        "Return the complete corrected plan/patch description and a one-line explanation.\n\n"
        f"TASK:\n{task}\n\nCOMMAND:\n{command}\n\nOUTPUT:\n{output[:4000]}"
    )


def diff_review_prompt(title: str, diff: str, test_command: Optional[str] = None) -> str:
    """Gate prompt: judge a concrete diff, not a plan."""
    ci = f"\nThe CI command that will run afterwards: `{test_command}`\n" if test_command else ""
    return (
        "You are an independent reviewer gating a change before merge.\n"
        "Judge the actual diff below: correctness, edge cases, security, error handling\n"
        "and whether the tests in the diff actually cover the change.\n"
        "Quote a file/line when you block something. Ignore pure style preferences.\n"
        f"{REVIEW_CONTRACT}\n"
        f"CHANGE: {title}{ci}\n"
        f"DIFF:\n{diff}"
    )

