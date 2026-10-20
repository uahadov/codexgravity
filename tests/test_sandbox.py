import asyncio
import pytest
from pathlib import Path
import tempfile
import subprocess

from codex_gravity.sandbox import SandboxManager, StateLock, LockState, CommandResult


@pytest.fixture
def temp_git_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir).resolve()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
        (repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True, capture_output=True)
        yield repo_dir


@pytest.mark.asyncio
async def test_statelock_single_writer_token():
    lock = StateLock()
    assert lock.state == LockState.IDLE
    assert lock.active_writer is None

    # Agent A acquires write token
    acquired = await lock.acquire_write_token("codex", state=LockState.CODING)
    assert acquired is True
    assert lock.state == LockState.CODING
    assert lock.active_writer == "codex"
    assert lock.can_write("codex") is True
    assert lock.can_write("antigravity") is False

    # Agent B should fail to acquire token with timeout
    with pytest.raises(TimeoutError):
        await lock.acquire_write_token("antigravity", timeout=0.1)

    # Agent A transitions state
    await lock.transition(LockState.VERIFYING, agent_id="codex")
    assert lock.state == LockState.VERIFYING

    # Agent A releases token
    await lock.release_write_token("codex")
    assert lock.state == LockState.IDLE
    assert lock.active_writer is None

    # Now Agent B can acquire
    acquired_b = await lock.acquire_write_token("antigravity", state=LockState.HEALING)
    assert acquired_b is True
    assert lock.active_writer == "antigravity"
    await lock.release_write_token("antigravity")


@pytest.mark.asyncio
async def test_sandbox_lifecycle_isolation(temp_git_repo):
    sandbox = SandboxManager(repo_root=temp_git_repo, task_id="test-task-1")
    try:
        sandbox_path = await sandbox.initialize()
        assert sandbox_path.exists()
        assert sandbox.is_created is True
        assert sandbox.sandbox_base.exists()

        # Write file in sandbox
        written_path = await sandbox.write_file("src/module.py", "x = 42\n")
        assert written_path.exists()
        assert await sandbox.read_file("src/module.py") == "x = 42\n"

        # Main repo must remain untouched (zero risk!)
        assert not (temp_git_repo / "src" / "module.py").exists()

        # Run command inside sandbox
        cmd_res = await sandbox.run_command(["python3", "-c", "import sys; sys.path.insert(0, '.'); from src.module import x; print(x)"])
        assert cmd_res.success is True
        assert cmd_res.stdout.strip() == "42"

        # List files
        files = await sandbox.list_files()
        assert "src/module.py" in files

        # Commit and Merge to main
        commit_sha = await sandbox.commit_changes("add module.py")
        assert commit_sha is not None

        merged = await sandbox.merge_to_main()
        assert merged is True
        assert (temp_git_repo / "src" / "module.py").exists()
    finally:
        await sandbox.cleanup()
        assert not sandbox.sandbox_dir.exists()


@pytest.mark.asyncio
async def test_sandbox_path_traversal_prevention(temp_git_repo):
    sandbox = SandboxManager(repo_root=temp_git_repo, task_id="security-test")
    await sandbox.initialize()
    try:
        with pytest.raises(ValueError, match="Path traversal detected"):
            sandbox.resolve_path("../../etc/passwd")

        with pytest.raises(ValueError, match="Path traversal detected"):
            await sandbox.write_file("../sneaky.txt", "exploit")
    finally:
        await sandbox.cleanup()


@pytest.mark.asyncio
async def test_statelock_read_only_during_verification(temp_git_repo):
    sandbox = SandboxManager(repo_root=temp_git_repo, task_id="readonly-test")
    await sandbox.initialize()
    try:
        # Acquire write token and write code
        await sandbox.state_lock.acquire_write_token("codex", state=LockState.CODING)
        await sandbox.write_file("test.py", "x = 1\n", writer_id="codex")

        # Transition to VERIFYING (release token)
        await sandbox.state_lock.release_write_token("codex", next_state=LockState.VERIFYING)
        assert sandbox.state_lock.state == LockState.VERIFYING

        # Attempts to write during verification must raise PermissionError
        with pytest.raises(PermissionError, match="Filesystem is read-only during VERIFYING phase"):
            await sandbox.write_file("test.py", "x = 2\n")

        with pytest.raises(PermissionError, match="Filesystem is read-only during VERIFYING phase"):
            await sandbox.write_file("test.py", "x = 2\n", writer_id="codex")
    finally:
        await sandbox.cleanup()


@pytest.mark.asyncio
async def test_sandbox_lifecycle_on_master_branch():
    """
    Tests that a repository with 'master' branch (instead of 'main')
    is dynamically detected and merged cleanly.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir).resolve()
        subprocess.run(["git", "init", "-b", "master"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
        (repo_dir / "README.md").write_text("# Master Branch Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init master"], cwd=repo_dir, check=True, capture_output=True)

        sandbox = SandboxManager(repo_root=repo_dir, task_id="master-branch-test")
        await sandbox.initialize()
        try:
            assert sandbox.base_branch == "master"
            await sandbox.write_file("master_file.py", "Y = 100\n")
            await sandbox.commit_changes("add master file")
            merged = await sandbox.merge_to_main()
            assert merged is True
            assert (repo_dir / "master_file.py").exists()
        finally:
            await sandbox.cleanup()
