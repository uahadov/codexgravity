# Codex-Gravity 🚀

Hybrid Autonomous Coding & Verification Engine uniting **OpenAI Codex** and **Google Antigravity (Gemini 3.8 Flash)**.

Built on the official **Model Context Protocol (MCP)** standard and architectural pillars:

- **Sütun 1: Speculative Agent Drafting (`speculative.py`)** — Gemini Flash generates lightning-fast (<400ms) AST skeletons and interfaces, while Codex fills precise algorithmic business logic, slashing token costs by >70%.
- **Sütun 2: Inter-Agent Walkie-Talkie Bus (`event_bus.py`)** — Pub/sub backchannel communication stream between Codex and Antigravity with human-in-the-loop injection.
- **Sütun 3: Visual Self-Healing & Verification Engine (`verifier.py`)** — Automated multi-viewport (Mobile, Tablet, Desktop) visual regressions check and terminal test diagnostic runner with automated self-healing.
- **Sütun 4: Shadow Git Worktree Sandbox Manager (`sandbox.py`)** — Completely isolated execution in `.shadow_sandbox/<uuid>`, keeping the main branch 100% pristine until atomic merge on PASS.
- **Official MCP Server (`mcp_server.py`)** — High-performance Stdio and Streamable HTTP MCP server exposing zero-copy media inspection, context distillation, and verification tools.
- **Unified Smart Router (`router.py`)** — Orchestrates the full lifecycle with transactional state locks, token arbitrage tracking, and 3 routing modes: Smart Adaptive, Eco, and Power.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Running Tests

```bash
pytest
```
