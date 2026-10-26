"""
Codex-Gravity: a two-way Codex + Antigravity engineering team.

Plan → adversarial review → cross-examination → revision → consensus → real
verification, with every message persisted in an auditable transcript.
"""

__version__ = "0.3.0"

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
from codex_gravity.team import (
    AgentReply,
    AgentTurn,
    AntigravityAgent,
    AntigravityGUIAgent,
    CodexAgent,
    Kind,
    ReviewFinding,
    Role,
    TeamAgent,
    TeamChannel,
    TeamConfig,
    TeamError,
    TeamMessage,
    TeamOrchestrator,
    TeamResult,
    TeamTranscript,
    Verdict,
    parse_review,
    tally,
)
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
    "AgentReply",
    "AgentTurn",
    "AntigravityAgent",
    "AntigravityGUIAgent",
    "CodexAgent",
    "Kind",
    "ReviewFinding",
    "Role",
    "TeamAgent",
    "TeamChannel",
    "TeamConfig",
    "TeamError",
    "TeamMessage",
    "TeamOrchestrator",
    "TeamResult",
    "TeamTranscript",
    "Verdict",
    "parse_review",
    "tally",
    "create_mcp_server",
]
