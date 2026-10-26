"""
Sütun 4: Shadow Git Worktree Sandbox Manager.
Provides isolated execution in .shadow_sandbox/<uuid>, ensures zero risk to the main repo,
and performs atomic merges only upon verified pass.
"""

from __future__ import annotations

import atexit
import asyncio
from contextlib import asynccontextmanager
import os
import re
import shutil
import signal
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Optional


class LockState(str, Enum):
    IDLE = "IDLE"
    PLANNING = "PLANNING"
    CODING = "CODING"
    VERIFYING = "VERIFYING"
    HEALING = "HEALING"
    MERGING = "MERGING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class StateLock:
    """
    Transactional State Lock & Single-Writer Token.
    Prevents race conditions between Codex and Antigravity.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state: LockState = LockState.IDLE
        self._active_writer: Optional[str] = None
        self._lock_time: Optional[float] = None

    @property
    def state(self) -> LockState:
        return self._state

    @property
    def active_writer(self) -> Optional[str]:
        return self._active_writer

    async def acquire_write_token(self, agent_id: str, state: LockState = LockState.CODING, timeout: float = 30.0) -> bool:
        start = time.time()
        while True:
            async with self._lock:
                if self._active_writer is None or self._active_writer == agent_id:
                    self._active_writer = agent_id
                    self._state = state
                    self._lock_time = time.time()
                    return True
            if time.time() - start >= timeout:
                break
            await asyncio.sleep(0.05)
        raise TimeoutError(f"Failed to acquire write token for '{agent_id}' within {timeout}s; held by '{self._active_writer}'")

    @asynccontextmanager
    async def write_transaction(
        self,
        agent_id: str,
        state: LockState = LockState.CODING,
        timeout: float = 30.0,
        next_state: Optional[LockState] = None,
    ):
        """
        Transactional context manager ensuring the write lock is reliably released
        even if an unhandled exception or cancellation occurs.
        """
        await self.acquire_write_token(agent_id, state=state, timeout=timeout)
        try:
            yield self
        finally:
            await self.release_write_token(agent_id, next_state=next_state)

    async def transition(self, new_state: LockState, agent_id: Optional[str] = None) -> None:
        async with self._lock:
            if agent_id and self._active_writer and self._active_writer != agent_id:
                raise PermissionError(f"Agent '{agent_id}' cannot transition state. Write lock is held by '{self._active_writer}'")
            self._state = new_state

    async def release_write_token(self, agent_id: str, next_state: Optional[LockState] = None) -> None:
        async with self._lock:
            if self._active_writer == agent_id:
                self._active_writer = None
                self._state = next_state or LockState.IDLE
                self._lock_time = None

    def can_write(self, agent_id: Optional[str] = None) -> bool:
        if self._state in (LockState.VERIFYING, LockState.MERGING, LockState.COMPLETED, LockState.FAILED):
            return False
        if self._active_writer is not None:
            return agent_id == self._active_writer
        return True


class CommandResult:
    def __init__(
        self,
        command: str | list[str],
        exit_code: int,
        stdout: str,
        stderr: str,
        duration_seconds: float,
    ) -> None:
        self.command = command
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.duration_seconds = duration_seconds

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    def __repr__(self) -> str:
        status = "SUCCESS" if self.success else f"FAILED({self.exit_code})"
        return f"<CommandResult {status} in {self.duration_seconds:.2f}s>"


_ACTIVE_SANDBOXES: set[SandboxManager] = set()


def _cleanup_dangling_worktrees() -> None:
    for sb in list(_ACTIVE_SANDBOXES):
        if sb.is_created and sb.sandbox_dir.exists():
            shutil.rmtree(sb.sandbox_dir, ignore_errors=True)


atexit.register(_cleanup_dangling_worktrees)


class SandboxManager:
    """
    Shadow Git Worktree Sandbox Manager.
    Creates an isolated git worktree under .shadow_sandbox/<uuid>,
    runs operations with absolute safety, and merges atomically to main upon success.
    """

    def __init__(
        self,
        repo_root: Optional[str | Path] = None,
        task_id: Optional[str] = None,
        base_branch: str = "main",
    ) -> None:
        self.repo_root = Path(repo_root or Path.cwd()).resolve()
        raw_task_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        clean_task_id = re.sub(r"[^a-zA-Z0-9_-]", "", raw_task_id)
        if not clean_task_id:
            clean_task_id = f"task-{uuid.uuid4().hex[:8]}"
        self.task_id = clean_task_id
        self.base_branch = base_branch
        self.branch_name = f"shadow/{self.task_id}"
        self.sandbox_base = (self.repo_root / ".shadow_sandbox").resolve()
        self.sandbox_dir = (self.sandbox_base / self.task_id).resolve()
        try:
            self.sandbox_dir.relative_to(self.sandbox_base)
        except ValueError:
            raise ValueError(f"Invalid task_id '{raw_task_id}': escapes sandbox base")
        self.state_lock = StateLock()
        self.is_created = False
        self.is_merged = False
        _ACTIVE_SANDBOXES.add(self)

    async def initialize(self) -> Path:
        """
        Creates the shadow git worktree.
        """
        if self.is_created:
            return self.sandbox_dir

        # Discover repository root using git rev-parse --show-toplevel
        top_res = await self._run_git(["rev-parse", "--show-toplevel"], cwd=self.repo_root)
        if top_res.success and top_res.stdout.strip():
            self.repo_root = Path(top_res.stdout.strip()).resolve()
            self.sandbox_base = (self.repo_root / ".shadow_sandbox").resolve()
            self.sandbox_dir = (self.sandbox_base / self.task_id).resolve()
        elif not (self.repo_root / ".git").exists():
            raise RuntimeError(f"Repository root is not a git repository: {self.repo_root}")

        self.sandbox_base.mkdir(parents=True, exist_ok=True)

        # Ensure .shadow_sandbox is excluded from git operations in repo_root
        git_dir = self.repo_root / ".git"
        info_dir = git_dir / "info" if git_dir.is_dir() else None
        if info_dir and info_dir.exists():
            exclude_file = info_dir / "exclude"
            try:
                content = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
                if ".shadow_sandbox" not in content:
                    sep = "\n" if content and not content.endswith("\n") else ""
                    exclude_file.write_text(content + sep + ".shadow_sandbox/\n", encoding="utf-8")
            except Exception:
                pass

        # Check git head exists
        check_head = await self._run_git(["rev-parse", "--verify", "HEAD"], cwd=self.repo_root)
        if not check_head.success:
            raise RuntimeError(f"Git HEAD could not be resolved in {self.repo_root}. Please ensure at least one commit exists.")

        # Detect active repository branch to avoid hardcoding "main" on repos with master/other branch
        active_branch = await self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo_root)
        if active_branch.success and active_branch.stdout.strip() and active_branch.stdout.strip() != "HEAD":
            if self.base_branch == "main":
                self.base_branch = active_branch.stdout.strip()

        # Create worktree with dedicated branch
        cmd = ["worktree", "add", "-b", self.branch_name, str(self.sandbox_dir), "HEAD"]
        res = await self._run_git(cmd, cwd=self.repo_root)
        if not res.success:
            # Check if branch exists and retry without -b if necessary
            if "already exists" in res.stderr:
                cmd_existing = ["worktree", "add", str(self.sandbox_dir), self.branch_name]
                res_existing = await self._run_git(cmd_existing, cwd=self.repo_root)
                if not res_existing.success:
                    raise RuntimeError(f"Failed to create worktree: {res_existing.stderr}")
            else:
                raise RuntimeError(f"Failed to create worktree: {res.stderr}")

        self.is_created = True
        return self.sandbox_dir

    def resolve_path(self, rel_path: str | Path) -> Path:
        """
        Resolves a relative path inside the sandbox and prevents path traversal attacks.
        """
        target = (self.sandbox_dir / rel_path).resolve()
        try:
            target.relative_to(self.sandbox_dir.resolve())
        except ValueError:
            raise ValueError(f"Path traversal detected: {rel_path} escapes sandbox {self.sandbox_dir}")
        return target

    async def write_file(self, rel_path: str | Path, content: str, writer_id: Optional[str] = None) -> Path:
        """
        Safely writes content to a file inside the isolated sandbox.
        """
        if not self.is_created:
            await self.initialize()

        if self.state_lock.state in (LockState.VERIFYING, LockState.MERGING, LockState.COMPLETED, LockState.FAILED):
            raise PermissionError(f"Filesystem is read-only during {self.state_lock.state.value} phase")

        if not self.state_lock.can_write(writer_id):
            active = self.state_lock.active_writer
            raise PermissionError(f"Agent '{writer_id}' cannot write: active write lock held by '{active}'")

        target_file = self.resolve_path(rel_path)
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(content, encoding="utf-8")
        return target_file

    async def read_file(self, rel_path: str | Path) -> str:
        """
        Reads a file from the isolated sandbox.
        """
        target_file = self.resolve_path(rel_path)
        if not target_file.exists():
            raise FileNotFoundError(f"File not found in sandbox: {rel_path}")
        return target_file.read_text(encoding="utf-8")

    def file_exists(self, rel_path: str | Path) -> bool:
        try:
            target_file = self.resolve_path(rel_path)
            return target_file.exists()
        except ValueError:
            return False

    async def list_files(self, rel_dir: str | Path = ".") -> list[str]:
        """
        Lists relative paths of all files in the sandbox.
        """
        target_dir = self.resolve_path(rel_dir)
        if not target_dir.exists():
            return []

        rel_paths: list[str] = []
        for path in target_dir.rglob("*"):
            if path.is_file():
                if ".git" in path.parts:
                    continue
                rel_paths.append(str(path.relative_to(self.sandbox_dir)))
        return sorted(rel_paths)

    async def run_command(
        self,
        command: str | list[str],
        timeout: float = 120.0,
        env: Optional[dict[str, str]] = None,
    ) -> CommandResult:
        """
        Executes a command inside the isolated sandbox directory with process group isolation.
        Zero risk to the main repo. Prevents zombie and orphan processes.
        """
        if not self.is_created:
            await self.initialize()

        full_env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for k, v in os.environ.items():
            if k in ("USER", "SHELL", "TERM", "VIRTUAL_ENV", "PYTHONPATH") or k.startswith(("GIT_", "PYTEST_")):
                full_env[k] = v
        if env:
            full_env.update(env)

        start = time.time()
        if isinstance(command, str):
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.sandbox_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=full_env,
                start_new_session=True,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.sandbox_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=full_env,
                start_new_session=True,
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout)
            duration = time.time() - start
            return CommandResult(
                command=command,
                exit_code=process.returncode or 0,
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
                duration_seconds=duration,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except Exception:
                pass
            duration = time.time() - start
            return CommandResult(
                command=command,
                exit_code=-1,
                stdout="",
                stderr=f"Command timed out or cancelled after {timeout} seconds",
                duration_seconds=duration,
            )

    async def commit_changes(self, message: str = "chore: sandbox patch") -> Optional[str]:
        """
        Stages and commits changes in the sandbox. Returns commit hash or None if clean.
        """
        if not self.is_created:
            return None

        await self._run_git(["add", "-A"], cwd=self.sandbox_dir)
        status_res = await self._run_git(["status", "--porcelain"], cwd=self.sandbox_dir)
        if not status_res.stdout.strip():
            return None  # nothing to commit

        commit_res = await self._run_git(["commit", "-m", message], cwd=self.sandbox_dir)
        if not commit_res.success:
            raise RuntimeError(f"Git commit failed in sandbox: {commit_res.stderr}")

        rev_res = await self._run_git(["rev-parse", "HEAD"], cwd=self.sandbox_dir)
        return rev_res.stdout.strip()

    async def merge_to_main(self, target_branch: Optional[str] = None, ff_only: bool = True) -> bool:
        """
        Merges sandbox branch into target branch (main) atomically.
        Only called when verification passes 100%.
        Guards main repo against dirty working copy corruption.
        """
        target = target_branch or self.base_branch
        await self.state_lock.transition(LockState.MERGING)

        # 1. Record the active branch or commit ref before switching
        branch_res = await self._run_git(["branch", "--show-current"], cwd=self.repo_root)
        original_branch = branch_res.stdout.strip()
        original_ref = original_branch
        if not original_ref:
            rev_res = await self._run_git(["rev-parse", "HEAD"], cwd=self.repo_root)
            if rev_res.success:
                original_ref = rev_res.stdout.strip()

        # 2. Check for uncommitted changes in repo_root and stash cleanly
        stashed = False
        dirty_check = await self._run_git(["status", "--porcelain"], cwd=self.repo_root)
        dirty_lines = [line for line in dirty_check.stdout.splitlines() if ".shadow_sandbox" not in line]
        if dirty_lines:
            stash_res = await self._run_git(
                ["stash", "push", "--include-untracked", "-m", f"codex_gravity_pre_merge_{self.task_id}"],
                cwd=self.repo_root,
            )
            if stash_res.success and "No local changes to save" not in stash_res.stdout:
                stashed = True
            elif not stash_res.success:
                await self.state_lock.transition(LockState.FAILED)
                raise RuntimeError(
                    f"Cannot merge: Failed to stash uncommitted changes in '{self.repo_root}': {stash_res.stderr}"
                )

        try:
            # First commit any uncommitted changes in sandbox
            await self.commit_changes("chore: finalize verified sandbox changes")

            # Checkout target branch in repo_root if not already on it
            curr_res = await self._run_git(["branch", "--show-current"], cwd=self.repo_root)
            if curr_res.stdout.strip() != target:
                co_res = await self._run_git(["checkout", target], cwd=self.repo_root)
                if not co_res.success:
                    await self.state_lock.transition(LockState.FAILED)
                    raise RuntimeError(f"Failed to checkout {target}: {co_res.stderr}")

            # Attempt merge
            merge_cmd = ["merge", "--ff-only", self.branch_name] if ff_only else ["merge", "-m", f"Merge {self.branch_name}", self.branch_name]
            merge_res = await self._run_git(merge_cmd, cwd=self.repo_root)
            if not merge_res.success:
                await self.state_lock.transition(LockState.FAILED)
                await self._run_git(["merge", "--abort"], cwd=self.repo_root)
                raise RuntimeError(f"Failed to merge {self.branch_name} into {target}: {merge_res.stderr}")

            self.is_merged = True
            await self.state_lock.transition(LockState.COMPLETED)
            return True
        finally:
            # Restore active branch / ref prior to merge
            if original_ref:
                curr_branch = (await self._run_git(["branch", "--show-current"], cwd=self.repo_root)).stdout.strip()
                if curr_branch != original_ref:
                    await self._run_git(["checkout", original_ref], cwd=self.repo_root)

            # Restore stashed changes cleanly
            if stashed:
                await self._run_git(["stash", "pop"], cwd=self.repo_root)

    async def cleanup(self, delete_branch: bool = True) -> None:
        """
        Removes the worktree and cleans up the shadow branch.
        """
        _ACTIVE_SANDBOXES.discard(self)
        if not self.is_created and not self.sandbox_dir.exists():
            return

        # 1. Remove worktree via git
        await self._run_git(["worktree", "remove", "--force", str(self.sandbox_dir)], cwd=self.repo_root)

        # 2. Remove leftover directory if git didn't remove it (safety check inside sandbox_base)
        if self.sandbox_dir.exists():
            try:
                self.sandbox_dir.relative_to(self.sandbox_base)
                shutil.rmtree(self.sandbox_dir, ignore_errors=True)
            except ValueError:
                pass

        # 3. Prune worktrees in git
        await self._run_git(["worktree", "prune"], cwd=self.repo_root)

        # 4. Delete shadow branch if requested (preserve if merge failed for inspection)
        if delete_branch and (self.is_merged or self.state_lock.state != LockState.FAILED):
            await self._run_git(["branch", "-D", self.branch_name], cwd=self.repo_root)

        self.is_created = False

    async def _run_git(self, args: list[str], cwd: Path) -> CommandResult:
        full_cmd = ["git"] + args
        start = time.time()
        process = await asyncio.create_subprocess_exec(
            *full_cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await process.communicate()
        return CommandResult(
            command=full_cmd,
            exit_code=process.returncode or 0,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            duration_seconds=time.time() - start,
        )

    async def __aenter__(self) -> SandboxManager:
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if not self.is_merged:
            await self.cleanup()
