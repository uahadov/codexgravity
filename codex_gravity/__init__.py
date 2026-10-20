"""
Codex-Gravity: Hybrid Autonomous Coding & Verification Engine.
Uniting OpenAI Codex and Google Antigravity (Gemini 3.8 Flash).
"""

__version__ = "0.1.0"

from codex_gravity.sandbox import SandboxManager, LockState, StateLock
from codex_gravity.event_bus import EventBus, Event, EventType
from codex_gravity.speculative import (
    SpeculativeDraftingPipeline,
    DraftSkeleton,
    AnchorTarget,
    CodexPatch,
    TokenArbitrageResult,
)
from codex_gravity.verifier import (
    VerificationEngine,
    TestExecutionResult,
    DiagnosticReport,
    ViewportCheckResult,
)
from codex_gravity.router import SmartRouter, RoutingMode, TaskRequest, ExecutionSummary
from codex_gravity.mcp_server import create_mcp_server

__all__ = [
    "SandboxManager",
    "LockState",
    "StateLock",
    "EventBus",
    "Event",
    "EventType",
    "SpeculativeDraftingPipeline",
    "DraftSkeleton",
    "AnchorTarget",
    "CodexPatch",
    "TokenArbitrageResult",
    "VerificationEngine",
    "TestExecutionResult",
    "DiagnosticReport",
    "ViewportCheckResult",
    "SmartRouter",
    "RoutingMode",
    "TaskRequest",
    "ExecutionSummary",
    "create_mcp_server",
]
