import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("action_comment", ROOT / "scripts" / "action_comment.py")
action_comment = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(action_comment)


def review(**overrides):
    data = {
        "status": "changes_requested",
        "title": "Harden add()",
        "session_id": "9f3c1ae0b7d2",
        "diff_chars": 812,
        "diff_truncated": False,
        "decision": {
            "achieved": False,
            "reason": "At least one reviewer has not approved.",
            "approvals": ["codex-cli"],
            "blockers": ["No test for the None case."],
            "dissent": ["antigravity: needs a test"],
        },
        "findings": [
            {"reviewer": "codex-cli", "verdict": "approve", "summary": "Safe."},
            {"reviewer": "antigravity", "verdict": "revise", "summary": "Needs a test."},
        ],
        "verification": {"command": "pytest -q", "passed": False, "exit_code": 1, "duration_seconds": 2.5},
    }
    data.update(overrides)
    return data


def test_comment_contains_status_findings_and_blockers():
    body = action_comment.build_comment(review())
    assert action_comment.MARKER in body
    assert "changes requested" in body
    assert "| `codex-cli` | ✅ approve | Safe. |" in body
    assert "| `antigravity` | 🛑 revise | Needs a test. |" in body
    assert "No test for the None case." in body
    assert "pytest -q" in body and "FAIL" in body
    assert "Dissent" in body
    assert "a review gate, not a merge" in body


def test_comment_survives_missing_fields():
    body = action_comment.build_comment({})
    assert action_comment.MARKER in body
    assert "unknown" in body


def test_comment_escapes_table_pipes():
    body = action_comment.build_comment(review(
        findings=[{"reviewer": "codex-cli", "verdict": "approve", "summary": "a | b"}]
    ))
    assert "a \\| b" in body


def test_upsert_creates_then_updates():
    calls = []

    def fake_request(method, url, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET":
            return [{"id": 7, "body": "unrelated"}, {"id": 9, "body": action_comment.MARKER + "\nold"}]
        return {}

    action = action_comment.upsert_comment("o/r", 12, "new body", "t", request=fake_request)
    assert action == "updated"
    assert calls[-1][0] == "PATCH" and calls[-1][1].endswith("/issues/comments/9")

    calls.clear()

    def fake_empty(method, url, token, payload=None):
        calls.append((method, url, payload))
        return [] if method == "GET" else {}

    assert action_comment.upsert_comment("o/r", 12, "body", "t", request=fake_empty) == "created"
    assert calls[-1][0] == "POST" and calls[-1][1].endswith("/issues/12/comments")


def test_pull_request_number_from_event(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 34}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.delenv("GITHUB_PR_NUMBER", raising=False)
    assert action_comment._pull_request_number() == 34

    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert action_comment._pull_request_number() is None


def test_main_skips_without_a_token(tmp_path, monkeypatch, capsys):
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review()), encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert action_comment.main([str(path)]) == 0

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert action_comment.main([str(path)]) == 1
    assert "not posting" in capsys.readouterr().err


def test_main_reports_a_missing_file(tmp_path, capsys):
    assert action_comment.main([str(tmp_path / "missing.json")]) == 1
    assert "no review file" in capsys.readouterr().err
