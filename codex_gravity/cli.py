"""
Command-Line Interface for Codex-Gravity.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from codex_gravity.router import RoutingMode, SmartRouter, TaskRequest


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex-Gravity CLI")
    parser.add_argument("prompt", type=str, help="Omni-prompt describing the task")
    parser.add_argument("--file", "-f", type=str, required=True, help="Target file path")
    parser.add_argument("--mode", "-m", choices=["adaptive", "eco", "power"], default="adaptive", help="Routing mode")
    parser.add_argument("--test-cmd", "-t", type=str, default=None, help="Test command to verify (e.g. pytest)")
    parser.add_argument("--ui-url", "-u", type=str, default=None, help="Local web UI URL to verify responsive viewports")
    parser.add_argument("--auto-merge", action="store_true", default=False, help="Automatically merge into main if tests pass")
    parser.add_argument(
        "--allow-heuristic-repair",
        action="store_true",
        default=False,
        help=(
            "Opt in to semantics-blind repairs (flipping operators/booleans) when no LLM is available. "
            "Off by default; such patches are never a verified fix."
        ),
    )

    args = parser.parse_args()

    if not args.auto_merge and sys.stdin.isatty():
        try:
            ans = input("Auto-merge into main branch upon test success? [y/N]: ").strip().lower()
            if ans in ("y", "yes"):
                args.auto_merge = True
        except (EOFError, KeyboardInterrupt):
            args.auto_merge = False

    if args.auto_merge and not args.test_cmd and not args.ui_url:
        # The router enforces this too; warn early so the run is not mistaken
        # for a merge candidate. An unverified run is never merged.
        print(
            "Note: --auto-merge without --test-cmd/--ui-url cannot merge; the run will be reported as unverified.",
            file=sys.stderr,
        )

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
        ui_url=args.ui_url,
        auto_merge=args.auto_merge,
        allow_heuristic_repair=args.allow_heuristic_repair,
    )

    router = SmartRouter()
    print(f"🚀 Launching Codex-Gravity in [{args.mode.upper()}] mode for {args.file}...")
    summary = asyncio.run(router.execute_task(req))

    print("\n" + "=" * 60)
    print(f"Status: {'✔ PASSED' if summary.success else ('⚠ UNVERIFIED' if not summary.verified else '❌ FAILED')}")
    if summary.verification is not None:
        print(f"Verification: {summary.verification.summary_text}")
    if getattr(summary, "is_mock", False):
        print("Engine Mode: ⚠️ MOCK / OFFLINE (API keys absent)")
    if summary.heuristic_repair_used:
        print("⚠️ Heuristic repair applied: source semantics were changed without model verification.")
    print(f"Sandbox Merged to Main: {summary.merged}")
    print(f"Tokens Saved: ${summary.arbitrage.dollars_saved} ({summary.arbitrage.savings_percentage}%) — ESTIMATED")
    print(f"Execution Time: {summary.arbitrage.generation_time_ms:.1f}ms")
    if summary.circuit_breaker_triggered:
        print("⚠️ Circuit breaker triggered: failed over to DeepCoder")
    print("=" * 60)
    print("\n--- Walkie-Talkie Backchannel ---")
    print(summary.walkie_talkie_transcript)


if __name__ == "__main__":
    main()
