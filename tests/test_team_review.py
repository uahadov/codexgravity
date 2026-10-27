import argparse
import json
import subprocess

import pytest

from codex_gravity.event_bus import EventBus
from codex_gravity.team import review_cli
from codex_gravity.team.protocol import TeamError
from codex_gravity.team.review import (
    DiffError,
    DiffReviewConfig,
    DiffReviewer,
    collect_diff,
    default_test_command,
)
from tests.team_fakes import ScriptedAgent, approve, revise


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    for key, value in (("user.email", "t@t.t"), ("user.name", "t")):
        subprocess.run(["git", "config", key, value], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def change(path):
    (path / "a.py").write_text(
        "def add(a, b):\n    if a is None or b is None:\n        raise ValueError('missing operand')\n    return a + b\n",
        encoding="utf-8",
    )


def config(repo, **overrides) -> DiffReviewConfig:
    data = {"workspace": str(repo), "title": "Harden add()", "reviewers": ["codex-cli", "antigravity"]}
    data.update(overrides)
    return DiffReviewConfig(**data)


def agents(codex_reply, agy_reply):
    return {
        "codex-cli": ScriptedAgent("codex-cli", [codex_reply]),
        "antigravity": ScriptedAgent("antigravity", [agy_reply]),
    }


# -- diff collection --------------------------------------------------------

def test_collect_working_and_staged_diff(repo):
    change(repo)
    diff, original, truncated = collect_diff(repo)
    assert "raise ValueError" in diff
    assert "a.py" in diff
    assert original > 0 and truncated is False

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    staged, _, _ = collect_diff(repo, staged=True)
    assert "raise ValueError" in staged


def test_collect_range_diff(repo):
    change(repo)
    subprocess.run(["git", "commit", "-am", "harden"], cwd=repo, check=True, capture_output=True)
    diff, _, _ = collect_diff(repo, base="HEAD~1")
    assert "+    if a is None or b is None:" in diff


def test_collect_untracked_files(repo):
    (repo / "new.py").write_text("VALUE = 42\n", encoding="utf-8")
    diff, _, _ = collect_diff(repo, include_untracked=True)
    assert "untracked file: new.py" in diff
    assert "VALUE = 42" in diff


def test_collect_truncates_large_diffs(repo):
    (repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(5000)), encoding="utf-8")
    diff, original, truncated = collect_diff(repo, include_untracked=True, max_chars=500)
    assert truncated is True
    assert original > 500
    assert "diff truncated" in diff


def test_collect_rejects_a_non_repo(tmp_path):
    with pytest.raises(DiffError):
        collect_diff(tmp_path)


def test_default_test_command_targets_changed_python(repo):
    change(repo)
    command = default_test_command(repo)
    assert command is not None and "py_compile" in command and "a.py" in command


# -- review pass ------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_reviewers_approve(repo):
    change(repo)
    result = await DiffReviewer(agents=agents(approve("Safe."), approve("Looks good.")), event_bus=EventBus()).review(
        config(repo)
    )
    assert result.status == "approved"
    assert result.exit_code == 0
    assert sorted(result.decision.approvals) == ["antigravity", "codex-cli"]
    assert result.diff_chars > 0
    assert result.report_path and result.report_path.endswith("review.md")


@pytest.mark.asyncio
async def test_one_blocker_blocks_the_change(repo):
    change(repo)
    result = await DiffReviewer(
        agents=agents(approve(), revise("No test for the None case.")),
        event_bus=EventBus(),
    ).review(config(repo))
    assert result.status == "changes_requested"
    assert result.exit_code == 1
    assert any("None case" in item for item in result.blockers)


@pytest.mark.asyncio
async def test_unreachable_reviewers_are_reported(repo):
    change(repo)
    pool = {
        "codex-cli": ScriptedAgent("codex-cli", [], available=False),
        "antigravity": ScriptedAgent("antigravity", [], available=False),
    }
    result = await DiffReviewer(agents=pool).review(config(repo))
    assert result.status == "unavailable"
    assert result.exit_code == 2
    assert set(result.decision.unavailable) == {"antigravity", "codex-cli"}


@pytest.mark.asyncio
async def test_failed_verification_marks_the_review_failed(repo):
    change(repo)
    result = await DiffReviewer(agents=agents(approve(), approve())).review(
        config(repo, test_command='python3 -c "raise SystemExit(3)"')
    )
    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.verification is not None and result.verification.exit_code == 3
    assert result.decision.achieved is True  # the code was approved; the build was not


@pytest.mark.asyncio
async def test_empty_diff_is_an_error(repo):
    with pytest.raises(DiffError) as error:
        await DiffReviewer(agents=agents(approve(), approve())).review(config(repo))
    assert "Nothing to review" in str(error.value)


@pytest.mark.asyncio
async def test_unknown_reviewer_is_a_team_error(repo):
    change(repo)
    with pytest.raises(TeamError):
        await DiffReviewer(agents=agents(approve(), approve())).review(
            config(repo, reviewers=["does-not-exist"])
        )


# -- CLI --------------------------------------------------------------------

def cli_args(repo, **overrides) -> argparse.Namespace:
    base = dict(
        workspace=str(repo), base=None, staged=False, untracked=False,
        title="Harden add()", review_with="codex-cli,antigravity", weight="", veto="",
        majority=False, threshold=0.5, test_cmd=None, auto_test=False, max_diff=60000,
        timeout=30.0, codex_thread=None, session_id=None, ui="plain", print_diff=False, json=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
async def test_cli_exit_codes_and_json(repo, capsys):
    change(repo)
    code = await review_cli._run(cli_args(repo), agents=agents(approve(), approve()))
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "approved"

    change(repo)
    code = await review_cli._run(cli_args(repo), agents=agents(approve(), revise("Needs a test.")))
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "changes_requested"


@pytest.mark.asyncio
async def test_cli_print_diff(repo, capsys):
    change(repo)
    code = await review_cli._run(cli_args(repo, print_diff=True))
    assert code == 0
    assert "raise ValueError" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cli_reports_an_empty_diff(repo, capsys):
    code = await review_cli._run(cli_args(repo), agents=agents(approve(), approve()))
    assert code == 2
    assert "Nothing to review" in capsys.readouterr().err
