"""
Sütun 4: Shadow Git Worktree Sandbox Manager.
Provides isolated execution in .shadow_sandbox/<uuid>, ensures zero risk to the main repo,
and performs atomic merges only upon verified pass.
"""

from __future__ import annotations

import asyncio
import os
import shutil
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
        while time.time() - start < timeout:
            async with self._lock:
                if self._active_writer is None or self._active_writer == agent_id:
                    self._active_writer = agent_id
                    self._state = state
                    self._lock_time = time.time()
                    return True
            await asyncio.sleep(0.05)
        raise TimeoutError(f"Failed to acquire write token for '{agent_id}' within {timeout}s; held by '{self._active_writer}'")

    async def transition(self, new_state: LockState, agent_id: Optional[str] = None) -> None:
        async with self._lock:
            if agent_id and self._active_writer and self._active_writer != agent_id:
                raise PermissionError(f"Agent '{agent_id}' cannot transition state. Write lock is held by '{self._active_writer}'")
            self._state = new_state

    async def release_write_token(self, agent_id: str) -> None:
        async with self._lock:
            if self._active_writer == agent_id:
                self._active_writer = None
                self._state = LockState.IDLE
                self._lock_time = None

    def can_write(self, agent_id: str) -> bool:
        return self._active_writer == agent_id or self._active_writer is None


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
        self.task_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        self.base_branch = base_branch
        self.branch_name = f"shadow/{self.task_id}"
        self.sandbox_base = self.repo_root / ".shadow_sandbox"
        self.sandbox_dir = self.sandbox_base / self.task_id
        self.state_lock = StateLock()
        self.is_created = False
        self.is_merged = False

    async def initialize(self) -> Path:
        """
        Creates the shadow git worktree.
        """
        if self.is_created:
            return self.sandbox_dir

        if not (self.repo_root / ".git").exists():
            raise RuntimeError(f"Repository root is not a git repository: {self.repo_root}")

        self.sandbox_base.mkdir(parents=True, exist_ok=True)

        # Check git head exists
        check_head = await self._run_git(["rev-parse", "--verify", "HEAD"], cwd=self.repo_root)
        if not check_head.success:
            raise RuntimeError(f"Git HEAD could not be resolved in {self.repo_root}. Please ensure at least one commit exists.")

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

        if writer_id and not self.state_lock.can_write(writer_id):
            raise PermissionError(f"Agent '{writer_id}' does not hold the active write lock")

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
        Executes a command inside the isolated sandbox directory.
        Zero risk to the main repo.
        """
        if not self.is_created:
            await self.initialize()

        full_env = os.environ.copy()
        full_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
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
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.sandbox_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=full_env,
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
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            duration = time.time() - start
            return CommandResult(
                command=command,
                exit_code=-1,
                stdout="",
                stderr=f"Command timed out after {timeout} seconds",
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
        """
        target = target_branch or self.base_branch
        await self.state_lock.transition(LockState.MERGING)

        # First commit any uncommitted changes in sandbox
        await self.commit_changes("chore: finalize verified sandbox changes")

        # Checkout target branch in repo_root
        co_res = await self._run_git(["checkout", target], cwd=self.repo_root)
        if not co_res.success:
            await self.state_lock.transition(LockState.FAILED)
            raise RuntimeError(f"Failed to checkout {target}: {co_res.stderr}")

        # Attempt merge
        merge_cmd = ["merge", "--ff-only", self.branch_name] if ff_only else ["merge", "-m", f"Merge {self.branch_name}", self.branch_name]
        merge_res = await self._run_git(merge_cmd, cwd=self.repo_root)
        if not merge_res.success:
            await self.state_lock.transition(LockState.FAILED)
            raise RuntimeError(f"Failed to merge {self.branch_name} into {target}: {merge_res.stderr}")

        self.is_merged = True
        await self.state_lock.transition(LockState.COMPLETED)
        return True

    async def cleanup(self, delete_branch: bool = True) -> None:
        """
        Removes the worktree and cleans up the shadow branch.
        """
        if not self.is_created:
            return

        # Remove worktree
        await self._run_git(["worktree", "remove", "--force", str(self.sandbox_dir)], cwd=self.repo_root)
        await self._run_git(["worktree", "prune"], cwd=self.repo_root)

        # Delete shadow branch if requested
        if delete_branch:
            await self._run_git(["branch", "-D", self.branch_name], cwd=self.repo_root)

        # Remove leftover directory if git didn't remove it
        if self.sandbox_dir.exists():
            shutil.rmtree(self.sandbox_dir, ignore_errors=True)

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
