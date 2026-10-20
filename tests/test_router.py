import pytest
import tempfile
import subprocess
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
        auto_merge=True,
    )

    summary = await router.execute_task(req, repo_root=temp_git_repo)

    assert summary.success is True
    assert summary.mode == RoutingMode.ECO
    assert summary.merged is True
    assert (temp_git_repo / "src" / "health.py").exists()
    assert summary.arbitrage.codex_tokens_used <= 50  # Eco mode enforces minimal Codex usage


@pytest.mark.asyncio
async def test_router_power_mode(temp_git_repo):
    router = SmartRouter()
    req = TaskRequest(
        prompt="Implement secure cryptographic hashing utility",
        target_file="src/crypto_util.py",
        mode=RoutingMode.POWER,
        auto_merge=True,
    )

    summary = await router.execute_task(req, repo_root=temp_git_repo)

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
        auto_merge=True,
    )

    summary = await router.execute_task(req, repo_root=temp_git_repo)

    # Even with Codex failure, circuit breaker must failover to Antigravity DeepCoder and succeed!
    assert summary.success is True
    assert summary.circuit_breaker_triggered is True
    assert summary.merged is True
    assert (temp_git_repo / "src" / "webhook.py").exists()
    assert "Circuit breaker tripped" in summary.walkie_talkie_transcript
