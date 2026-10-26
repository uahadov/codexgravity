from codex_gravity.team.consensus import (
    ReviewFinding,
    Verdict,
    parse_review,
    review_prompt,
    revision_prompt,
    tally,
)
from codex_gravity.team.protocol import Role


def test_parse_fenced_approval():
    finding = parse_review(
        'Sure.\n```json\n{"verdict": "approve", "summary": "ok", "blocking_issues": [], "questions": []}\n```',
        "codex",
    )
    assert finding.verdict is Verdict.APPROVE
    assert finding.parsed is True
    assert finding.summary == "ok"


def test_parse_revise_with_questions_and_issues():
    finding = parse_review(
        '```json\n{"verdict":"changes_requested","summary":"needs work",'
        '"blocking_issues":["no timeout handling"],"questions":["Which queue?"]}\n```',
        "antigravity",
    )
    assert finding.verdict is Verdict.REVISE
    assert finding.blocking_issues == ["no timeout handling"]
    assert finding.questions == ["Which queue?"]


def test_unparsable_reply_is_never_an_approval():
    finding = parse_review("I think it is probably fine, ship it!", "codex")
    assert finding.verdict is Verdict.REVISE
    assert finding.parsed is False
    assert finding.blocking_issues
    assert "ship it!" in finding.blocking_issues[0]


def test_empty_reply_is_not_an_approval():
    finding = parse_review("", "codex")
    assert finding.verdict is Verdict.REVISE
    assert finding.parsed is False
    assert finding.error == "empty"


def test_approval_with_blockers_is_demoted():
    finding = parse_review(
        '{"verdict":"approve","summary":"fine","blocking_issues":["still leaks tokens"]}',
        "antigravity",
    )
    assert finding.verdict is Verdict.REVISE
    assert finding.parsed is False


def test_tally_unanimous_requires_every_reviewer():
    approve = parse_review('{"verdict":"approve"}', "codex")
    revise = parse_review('{"verdict":"revise","blocking_issues":["x"]}', "antigravity")

    blocked = tally([approve, revise], require_unanimous=True)
    assert blocked.achieved is False
    assert blocked.blockers == ["x"]
    assert blocked.approvals == ["codex"]
    assert blocked.dissent

    passed = tally([approve, parse_review('{"verdict":"approve"}', "antigravity")])
    assert passed.achieved is True
    assert sorted(passed.approvals) == ["antigravity", "codex"]


def test_tally_weighted_majority():
    codex = parse_review('{"verdict":"approve"}', "codex", weight=3.0)
    antigravity = parse_review('{"verdict":"revise","blocking_issues":["x"]}', "antigravity", weight=1.0)

    # A blocker gates consensus even when the heavier reviewer approves.
    assert tally([codex, antigravity], require_unanimous=False, majority_threshold=0.5).achieved is False

    # With no concrete blocker, the heavier vote carries - and dissent is kept.
    codex2 = parse_review('{"verdict":"approve"}', "codex", weight=3.0)
    antigravity2 = parse_review('{"verdict":"revise"}', "antigravity", weight=1.0)
    decision = tally([codex2, antigravity2], require_unanimous=False)
    assert decision.achieved is True
    assert decision.approvals == ["codex"]
    assert decision.dissent == ["antigravity: revise"]

    # Unanimity (the default) refuses the same split regardless of weight.
    assert tally([codex2, antigravity2], require_unanimous=True).achieved is False


def test_veto_blocks_consensus():
    codex = parse_review('{"verdict":"approve"}', "codex")
    critic = parse_review('{"verdict":"reject","blocking_issues":["unsafe"]}', "critic", veto=True)
    decision = tally([codex, critic], require_unanimous=False, majority_threshold=0.1)
    assert decision.achieved is False
    assert "Veto" in decision.reason


def test_tally_reports_unavailable_reviewers():
    unavailable = ReviewFinding(reviewer="codex", verdict=Verdict.UNAVAILABLE, error="no cli")
    decision = tally([unavailable])
    assert decision.achieved is False
    assert decision.unavailable == ["codex"]
    assert "available" in decision.reason


def test_prompts_carry_the_review_contract():
    review = review_prompt("task", "proposal")
    assert "blocking_issues" in review
    assert "approve` is only valid when" in review

    finding = parse_review('{"verdict":"revise","blocking_issues":["no rollback"]}', "codex")
    revision = revision_prompt("task", "proposal", [finding], [("Why?", "Because.")])
    assert "no rollback" in revision
    assert "Q: Why?" in revision
    assert finding.role is Role.REVIEWER
