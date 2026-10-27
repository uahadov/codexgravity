"""
Project policy: ``.codex-gravity/team.json``.

A team policy is committed next to the code so every run uses the same roles,
weights, test command and budget. Command-line flags override the file; the file
overrides the built-in defaults.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from codex_gravity.team.protocol import TeamError

POLICY_DIR = ".codex-gravity"
POLICY_FILENAME = "team.json"

DEFAULT_POLICY: dict[str, Any] = {
    "plan_with": "codex",
    "implement_with": "antigravity",
    "review_with": ["codex", "antigravity"],
    "judge_with": None,
    "reviewer_weights": {},
    "veto_agents": [],
    "max_rounds": 3,
    "require_unanimous": True,
    "test_command": None,
    "turn_timeout": 180,
    "verification_retries": 1,
    "codex_thread": None,
    "max_agent_calls": 0,
    "budget_minutes": 0,
    "remember_decisions": True,
}

_ALLOWED = set(DEFAULT_POLICY)


def policy_path(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / POLICY_DIR / POLICY_FILENAME


def _validate(policy: dict[str, Any], source: str) -> dict[str, Any]:
    unknown = sorted(set(policy) - _ALLOWED)
    if unknown:
        raise TeamError(
            f"{source}: unknown setting(s) {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(_ALLOWED))}."
        )
    merged = {**DEFAULT_POLICY, **policy}
    if not isinstance(merged["review_with"], list) or not merged["review_with"]:
        raise TeamError(f"{source}: review_with must be a non-empty list.")
    if not isinstance(merged["max_rounds"], int) or not 1 <= merged["max_rounds"] <= 10:
        raise TeamError(f"{source}: max_rounds must be an integer between 1 and 10.")
    if not isinstance(merged["turn_timeout"], (int, float)) or merged["turn_timeout"] < 5:
        raise TeamError(f"{source}: turn_timeout must be at least 5 seconds.")
    for key in ("max_agent_calls",):
        if not isinstance(merged[key], int) or merged[key] < 0:
            raise TeamError(f"{source}: {key} must be 0 (unlimited) or a positive integer.")
    if not isinstance(merged["budget_minutes"], (int, float)) or merged["budget_minutes"] < 0:
        raise TeamError(f"{source}: budget_minutes must be 0 (unlimited) or positive.")
    return merged


def load_policy(workspace: str | Path) -> dict[str, Any]:
    """Defaults overlaid with the project's policy file (when it exists)."""
    path = policy_path(workspace)
    if not path.is_file():
        return dict(DEFAULT_POLICY)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TeamError(f"Cannot read {path}: {error}") from error
    if not isinstance(data, dict):
        raise TeamError(f"{path} must contain a JSON object.")
    return _validate(data, str(path))


def write_policy(
    workspace: str | Path,
    *,
    force: bool = False,
    overrides: Optional[dict[str, Any]] = None,
) -> Path:
    """Create the policy file; refuses to overwrite unless ``force``."""
    path = policy_path(workspace)
    if path.exists() and not force:
        raise TeamError(f"{path} already exists (use --force to overwrite).")
    policy = _validate(dict(overrides or {}), "init")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(policy, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return path


def policy_summary(policy: dict[str, Any]) -> str:
    """One-screen description used by ``--dry-run``."""
    lines = [
        f"plan      {policy['plan_with']}",
        f"implement {policy['implement_with']}",
        f"review    {', '.join(policy['review_with'])}",
        f"judge     {policy['judge_with'] or '—'}",
        f"rounds    {policy['max_rounds']} ({'unanimous' if policy['require_unanimous'] else 'weighted majority'})",
        f"verify    {policy['test_command'] or '—'}",
        f"budget    {policy['max_agent_calls'] or '∞'} calls · {policy['budget_minutes'] or '∞'} minutes",
        f"remember  {'yes' if policy['remember_decisions'] else 'no'}",
    ]
    if policy["veto_agents"]:
        lines.append(f"veto      {', '.join(policy['veto_agents'])}")
    if policy["reviewer_weights"]:
        lines.append("weights   " + ", ".join(f"{k}={v}" for k, v in policy["reviewer_weights"].items()))
    if policy["codex_thread"]:
        lines.append(f"codex app {policy['codex_thread']}")
    return "\n".join(lines)
