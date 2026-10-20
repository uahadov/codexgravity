"""
Command-Line Interface for Codex-Gravity.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from codex_gravity.router import RoutingMode, SmartRouter, TaskRequest


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex-Gravity CLI")
    parser.add_argument("prompt", type=str, help="Omni-prompt describing the task")
    parser.add_argument("--file", "-f", type=str, required=True, help="Target file path")
    parser.add_argument("--mode", "-m", choices=["adaptive", "eco", "power"], default="adaptive", help="Routing mode")
    parser.add_argument("--test-cmd", "-t", type=str, default=None, help="Test command to verify (e.g. pytest)")
    parser.add_argument("--no-merge", action="store_true", help="Do not merge into main even if tests pass")

    args = parser.parse_args()

    mode_map = {
        "adaptive": RoutingMode.SMART_ADAPTIVE,
        "eco": RoutingMode.ECO,
        "power": RoutingMode.POWER,
    }

    req = TaskRequest(
        prompt=args.prompt,
        target_file=args.file,
        mode=mode_map[args.mode],
        test_command=args.test_cmd,
        auto_merge=not args.no_merge,
    )

    router = SmartRouter()
    print(f"🚀 Launching Codex-Gravity in [{args.mode.upper()}] mode for {args.file}...")
    summary = asyncio.run(router.execute_task(req))

    print("\n" + "=" * 60)
    print(f"Status: {'✔ SUCCESS' if summary.success else '❌ FAILED'}")
    print(f"Sandbox Merged to Main: {summary.merged}")
    print(f"Tokens Saved: ${summary.arbitrage.dollars_saved} ({summary.arbitrage.savings_percentage}%)")
    print(f"Execution Time: {summary.arbitrage.generation_time_ms:.1f}ms")
    if summary.circuit_breaker_triggered:
        print("⚠️ Circuit breaker triggered: failed over to DeepCoder")
    print("=" * 60)
    print("\n--- Walkie-Talkie Backchannel ---")
    print(summary.walkie_talkie_transcript)


if __name__ == "__main__":
    main()
