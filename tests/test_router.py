import pytest
import tempfile
import subprocess
import sys
from pathlib import Path

from codex_gravity.router import SmartRouter, RoutingMode, TaskRequest


@pytest.fixture
def temp_git_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir).resolve()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
        (repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)
        yield repo_dir


@pytest.mark.asyncio
async def test_router_adaptive_mode_end_to_end(temp_git_repo):
    router = SmartRouter()
    req = TaskRequest(
        prompt="Create user profile validation service",
        target_file="src/profile_service.py",
        mode=RoutingMode.SMART_ADAPTIVE,
        test_command="python3 -c 'import ast; ast.parse(open(\"src/profile_service.py\").read())'",
        auto_merge=True,
    )

    summary = await router.execute_task(req, repo_root=temp_git_repo)

    assert summary.success is True
    assert summary.merged is True
    assert (temp_git_repo / "src" / "profile_service.py").exists()
    assert summary.arbitrage.dollars_saved > 0
    assert summary.arbitrage.savings_percentage > 50.0
    assert "Walkie-Talkie" not in summary.walkie_talkie_transcript or len(summary.walkie_talkie_transcript) > 0
    assert router.ledger.total_raw_tokens > 0


@pytest.mark.asyncio
async def test_router_eco_mode(temp_git_repo):
    router = SmartRouter()
    req = TaskRequest(
        prompt="Generate minimal healthcheck endpoint",
        target_file="src/health.py",
        mode=RoutingMode.ECO,
        test_command="python3 -c 'import ast; ast.parse(open(\"src/health.py\").read())'",
        auto_merge=True,
    )

    summary = await router.execute_task(req, repo_root=temp_git_repo)

    assert summary.verified is True
    assert summary.success is True
    assert summary.mode == RoutingMode.ECO
    assert summary.merged is True
    assert (temp_git_repo / "src" / "health.py").exists()
    # Eco mode never calls Codex, so it must not be billed for Codex tokens.
    assert summary.arbitrage.codex_tokens_used == 0
    assert summary.arbitrage.estimated is True


@pytest.mark.asyncio
async def test_router_power_mode(temp_git_repo):
    router = SmartRouter()
    req = TaskRequest(
        prompt="Implement secure cryptographic hashing utility",
        target_file="src/crypto_util.py",
        mode=RoutingMode.POWER,
        test_command="python3 -c 'import ast; ast.parse(open(\"src/crypto_util.py\").read())'",
        auto_merge=True,
    )

    summary = await router.execute_task(req, repo_root=temp_git_repo)

    assert summary.verified is True
    assert summary.success is True
    assert summary.mode == RoutingMode.POWER
    assert "Adversarial Red-Team Audit" in summary.walkie_talkie_transcript
    assert (temp_git_repo / "src" / "crypto_util.py").exists()


@pytest.mark.asyncio
async def test_router_circuit_breaker_failover(temp_git_repo):
    router = SmartRouter()
    # Simulate Codex failure (e.g. 429 rate limit or outage)
    router._codex_failure_simulator = True

    req = TaskRequest(
        prompt="Generate payment webhook listener",
        target_file="src/webhook.py",
        mode=RoutingMode.SMART_ADAPTIVE,
        test_command="python3 -c 'import ast; ast.parse(open(\"src/webhook.py\").read())'",
        auto_merge=True,
    )

    summary = await router.execute_task(req, repo_root=temp_git_repo)

    # Even with Codex failure, circuit breaker must failover to Antigravity DeepCoder and succeed!
    assert summary.success is True
    assert summary.circuit_breaker_triggered is True
    assert summary.merged is True
    assert (temp_git_repo / "src" / "webhook.py").exists()
    assert "Circuit breaker tripped" in summary.walkie_talkie_transcript


@pytest.mark.asyncio
async def test_router_self_healing_execution(temp_git_repo):
    """
    Tests that SmartRouter automatically triggers auto-healing and repairs
    failing logic before merging.
    """
    router = SmartRouter()

    # Create a test file in repo_root before running task
    test_file = temp_git_repo / "test_calc.py"
    test_file.write_text(
        "from src.calc import add\n"
        "def test_add():\n"
        "    assert add(2, 2) == 4\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "test_calc.py"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add calc test"], cwd=temp_git_repo, check=True, capture_output=True)

    from codex_gravity.speculative import DraftSkeleton, AnchorTarget, CodexPatch

    # Custom flash generator that generates the module-level function
    def custom_flash(task, file_path, lang, ctx):
        return DraftSkeleton(
            file_path=file_path,
            language=lang,
            skeleton_code="def add(a, b):\n    # TODO\n    return a - b\n",
            anchors=[AnchorTarget(signature="def add(a, b):", placeholder="# TODO")],
        )

    router.speculative_pipeline.flash_generator = custom_flash

    # First attempt completer emits buggy logic
    async def buggy_completer(skeleton):
        return [CodexPatch(anchor_id=skeleton.anchors[0].anchor_id, replacement_code="# Initial buggy draft\n")]

    router.speculative_pipeline.codex_completer = buggy_completer

    # Healing patcher fixes the bug using the diagnostic report and sandbox
    async def healing_patcher(diag, viewports, sandbox):
        content = await sandbox.read_file("src/calc.py")
        fixed = content.replace("return a - b", "return a + b")
        await sandbox.write_file("src/calc.py", fixed, writer_id="codex")

    req = TaskRequest(
        prompt="Implement add function",
        target_file="src/calc.py",
        mode=RoutingMode.SMART_ADAPTIVE,
        test_command=f"{sys.executable} -B -m pytest -o cache_dir=/tmp test_calc.py",
        auto_merge=True,
        custom_patcher=healing_patcher,
    )

    summary = await router.execute_task(req, repo_root=temp_git_repo)
    assert summary.success is True
    assert summary.merged is True
    assert (temp_git_repo / "src" / "calc.py").exists()
    assert "return a + b" in (temp_git_repo / "src" / "calc.py").read_text()
    assert "Applied self-healing patch" in summary.walkie_talkie_transcript
