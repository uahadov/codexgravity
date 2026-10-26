"""
Global pytest fixtures and configuration for Codex-Gravity test suite.
"""

from unittest.mock import AsyncMock, patch
import pytest


@pytest.fixture(autouse=True)
def mock_external_agentapi():
    """
    Standard test fixture replacing PYTEST_CURRENT_TEST sniffing in production code.
    Prevents unit tests from hanging on external language_server / agentapi processes.
    """
    with patch("codex_gravity.mcp_server.call_native_antigravity", new_callable=AsyncMock) as mock_mcp_agy, \
         patch("codex_gravity.speculative.call_agentapi", new_callable=AsyncMock) as mock_spec_agy:
        mock_mcp_agy.return_value = None
        mock_spec_agy.return_value = None
        yield


@pytest.fixture
def temp_git_repo():
    """
    Creates an isolated temporary git repository with user config and initial commit.
    """
    import subprocess
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir).resolve()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
        (repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)
        yield repo_dir
