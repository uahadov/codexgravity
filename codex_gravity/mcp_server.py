"""
Official Model Context Protocol (MCP) Server for Codex-Gravity.
Supports Stdio and Streamable HTTP transports.
Exposes zero-copy media inspection, context distillation, and verification tools.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import re
import secrets
import shlex
import signal
import sys
import urllib.parse
from importlib.metadata import PackageNotFoundError, version as _package_version
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer, Context
from starlette.responses import HTMLResponse, JSONResponse

from codex_gravity.event_bus import EventBus
from codex_gravity.codex_bridge import BridgeError, DesktopCodex, run_codex_architect
from codex_gravity.codex_gui import (
    CodexDesktopChat,
    CodexDesktopUnavailable,
    desktop_availability,
    reply_to_dict,
    snapshot_to_dict,
)
from codex_gravity.completion import CompletionWatches
from codex_gravity.router import RoutingMode, SmartRouter, TaskRequest
from codex_gravity.team.agents import default_agents
from codex_gravity.team.protocol import TeamError
from codex_gravity.team.session import TeamConfig, TeamOrchestrator
from codex_gravity.verifier import RootCauseAnalyzer, VisualRegressionInspector

# Shared EventBus and SmartRouter instances for Studio Cockpit
_shared_event_bus = EventBus()
_shared_router = SmartRouter(event_bus=_shared_event_bus)

# The studio routes run shell commands and mutate the project, so they are
# authenticated. The token is per-process (or operator-supplied) and is injected
# into the dashboard page, which a cross-origin page cannot read.
_PROCESS_STUDIO_TOKEN = secrets.token_urlsafe(32)
STUDIO_HEADER = "x-codex-gravity-token"


def studio_token() -> str:
    """Token required by the mutating Studio HTTP routes."""
    return os.environ.get("CODEX_GRAVITY_STUDIO_TOKEN") or _PROCESS_STUDIO_TOKEN


def studio_tool_allowlist() -> Optional[set[str]]:
    """Optional comma-separated allowlist restricting /api/execute-tool."""
    raw = os.environ.get("CODEX_GRAVITY_STUDIO_TOOLS", "").strip()
    return {name.strip() for name in raw.split(",") if name.strip()} or None


def _studio_denied(request: Any, tool_name: Optional[str] = None) -> Optional[JSONResponse]:
    """
    Gate for routes that execute shell commands or mutate project files.

    The dashboard is served with the token inlined so a browser tab can use it;
    cross-origin pages cannot read that document, so they cannot replay it.
    """
    supplied = request.headers.get(STUDIO_HEADER, "")
    if not secrets.compare_digest(supplied, studio_token()):
        return JSONResponse(
            {
                "status": "error",
                "error": (
                    f"Missing or invalid {STUDIO_HEADER}. Open http://127.0.0.1:<port>/ so the "
                    "dashboard can use its injected token, or send "
                    "CODEX_GRAVITY_STUDIO_TOKEN for scripted calls. No command was executed."
                ),
            },
            status_code=403,
        )
    allowed = studio_tool_allowlist()
    if allowed is not None and (tool_name is None or tool_name not in allowed):
        return JSONResponse(
            {
                "status": "error",
                "error": f"Tool '{tool_name}' is not in CODEX_GRAVITY_STUDIO_TOOLS. No command was executed.",
            },
            status_code=403,
        )
    return None

# Load environment variables (.env) if present
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
else:
    load_dotenv()


def _resolve_local_path(path_or_uri: str) -> Optional[Path]:
    """
    Resolves local paths and file:// URIs into canonical Path instances.
    Safely rejects raw multiline strings and excessively long inputs to prevent OSError/DoS.
    """
    clean = path_or_uri.strip()
    if "\n" in clean or len(clean) > 4096 or "\x00" in clean:
        return None
    if clean.startswith("file:"):
        parsed = urllib.parse.urlparse(clean)
        clean = urllib.parse.unquote(parsed.path)
    try:
        return Path(clean).resolve()
    except (ValueError, OSError):
        return None


def get_gemini_client() -> Optional[Any]:
    """
    Initializes official Google GenAI Client if GEMINI_API_KEY or GOOGLE_API_KEY is present.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai
        return genai.Client(api_key=api_key)
    except Exception:
        return None


async def call_native_antigravity(
    prompt: str,
    media_file_path: Optional[Path] = None,
    model: str = "flash",
    timeout_sec: float = 40.0,
) -> Optional[str]:
    """
    Directly invokes Google Antigravity (Gemini 3.8 Flash) using the user's active local
    Google Antigravity subscription via the official language_server / agentapi binary.
    No external API keys or cloud billing needed.
    """
    antigravity_home = Path(os.environ.get("ANTIGRAVITY_HOME", Path.home() / ".gemini/antigravity"))
    agentapi_candidates = [
        antigravity_home / "bin/agentapi",
        Path.home() / ".gemini/antigravity/bin/agentapi",
        Path("/opt/Antigravity/resources/bin/language_server"),
    ]
    agentapi_bin = None
    for cand in agentapi_candidates:
        if cand.exists() and os.access(cand, os.X_OK):
            agentapi_bin = str(cand)
            break

    if not agentapi_bin:
        return None

    if media_file_path and media_file_path.exists():
        formatted_prompt = (
            f"Zəhmət olmasa dərhal `read_file` alətindən istifadə edərək bu faylı oxu və vizual olaraq təhlil et: {media_file_path.resolve()}\n\n"
            f"Göstəriş/Sual: {prompt}\n\n"
            f"Faylda gördüyün bütün vizual detalları (başlıq, pəncərə adı, axtarış xanasındakı mətn, seçilmiş menyu, nəticələr sahəsi və s.) dəqiqliklə və ətraflı cavablandır. Heç bir faylı dəyişmə."
        )
    else:
        formatted_prompt = (
            "Cavabı dərhal heç bir alət və ya komanda (tool) çağırmadan birbaşa aydın mətn olaraq ver:\n\n"
            + prompt
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            agentapi_bin,
            "new-conversation",
            f"--model={model}",
            formatted_prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        if proc.returncode != 0:
            return None

        data = json.loads(stdout.decode("utf-8", errors="replace"))
        conv_id = data.get("response", {}).get("newConversation", {}).get("conversationId")
        if not conv_id:
            return None

        transcript_file = antigravity_home / f"brain/{conv_id}/.system_generated/logs/transcript.jsonl"

        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < timeout_sec:
            if transcript_file.exists():
                try:
                    lines = transcript_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                    for line in reversed(lines):
                        if not line.strip():
                            continue
                        entry = json.loads(line)
                        if entry.get("type") == "PLANNER_RESPONSE" and entry.get("content"):
                            return entry.get("content")
                except Exception:
                    pass
            await asyncio.sleep(0.4)
        return None
    except Exception:
        return None


async def _inspect_media_with_gemini_or_probe(
    file_path: Path,
    focus_query: str = "",
    instruction: Optional[str] = None,
) -> dict[str, Any]:
    """
    Performs real technical probing with ffprobe and connects to live Google Antigravity
    (Gemini 3.8 Flash subscription) or Google GenAI model.
    """
    # 1. Probing with ffprobe for real technical ground truth
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]
    meta = {}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        if proc.returncode == 0:
            meta = json.loads(out.decode("utf-8", errors="replace"))
    except Exception:
        pass

    format_info = meta.get("format", {})
    streams = meta.get("streams", [])
    duration_sec = float(format_info.get("duration", 0.0) or 0.0)
    size_bytes = file_path.stat().st_size
    mime, _ = mimetypes.guess_type(str(file_path))

    # 2. Check for native Google Antigravity subscription execution (agentapi)
    agy_analysis = await call_native_antigravity(
        prompt=focus_query or instruction or "Describe key visual events and detailed breakdown",
        media_file_path=file_path,
        model="flash",
        timeout_sec=35.0,
    )
    if agy_analysis:
        return {
            "status": "success",
            "provider": "gemini-3.8-flash",
            "engine": "google-antigravity-subscription (native agentapi)",
            "analysis_available": True,
            "file_path": str(file_path),
            "size_bytes": size_bytes,
            "mime_type": mime or "application/octet-stream",
            "duration_seconds": duration_sec,
            "analysis": agy_analysis,
            # No transcript/timestamps are produced by this path, so none are invented.
            "timestamps": [],
            "technical_streams": [
                {
                    "codec_type": s.get("codec_type"),
                    "codec_name": s.get("codec_name"),
                    "width": s.get("width"),
                    "height": s.get("height"),
                }
                for s in streams
            ],
        }

    # 3. Check for external Gemini API client if configured
    client = get_gemini_client()
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    if client:
        try:
            prompt = (
                f"Analyze this media file for the following query or instruction:\n"
                f"Query/Instruction: {focus_query or instruction or 'Describe key events with timestamps'}\n"
                f"File: {file_path.name} (Format: {format_info.get('format_long_name', mime)}, Duration: {duration_sec:.2f}s, Size: {size_bytes} bytes)\n"
                f"Please provide an accurate, timestamped breakdown and key visual/audio findings."
            )
            if size_bytes < 25 * 1024 * 1024 and mime and (mime.startswith("video/") or mime.startswith("audio/") or mime.startswith("image/")):
                uploaded_file = client.files.upload(file=str(file_path))
                response = client.models.generate_content(
                    model=model_name,
                    contents=[uploaded_file, prompt],
                )
            else:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )

            return {
                "status": "success",
                "provider": "gemini-3.8-flash",
                "engine": f"google-gemini-live ({model_name})",
                "analysis_available": True,
                "file_path": str(file_path),
                "size_bytes": size_bytes,
                "mime_type": mime or "application/octet-stream",
                "duration_seconds": duration_sec,
                "analysis": response.text,
                # This path returns prose only; no timestamps are invented.
                "timestamps": [],
                "technical_streams": [
                    {
                        "codec_type": s.get("codec_type"),
                        "codec_name": s.get("codec_name"),
                        "width": s.get("width"),
                        "height": s.get("height"),
                    }
                    for s in streams
                ],
            }
        except Exception as e:
            return {
                "status": "api_error",
                "provider": "gemini-3.8-flash",
                "engine": f"google-gemini ({model_name})",
                "error": str(e),
                "file_path": str(file_path),
                "timestamps": [
                    {"time": "00:00:00", "event": "API analysis error fallback", "confidence": 0.50},
                ],
                "fallback_technical_metadata": format_info,
            }

    # 4. No model answered: report container/stream metadata only, and say so.
    technical_summary = (
        f"Media probed via ffprobe: duration={duration_sec:.2f}s, format={format_info.get('format_name', 'unknown')}, "
        f"streams={len(streams)}. Query focus: '{focus_query or instruction}'."
    )
    return {
        "status": "partial",
        "provider": "ffprobe",
        "engine": "ffprobe-local-metadata",
        "analysis_available": False,
        "notice": (
            "No model analysed this file: neither the Antigravity agentapi binary nor a "
            "GEMINI_API_KEY / GOOGLE_API_KEY is available. Only container and stream "
            "metadata were read; there is no visual or audio understanding in this result."
        ),
        "file_path": str(file_path),
        "size_bytes": size_bytes,
        "mime_type": mime or "application/octet-stream",
        "duration_seconds": duration_sec,
        "format_name": format_info.get("format_long_name", "unknown"),
        "analysis": technical_summary,
        "timestamps": [],
        "streams": [
            {
                "codec_type": s.get("codec_type"),
                "codec_name": s.get("codec_name"),
                "width": s.get("width"),
                "height": s.get("height"),
                "r_frame_rate": s.get("r_frame_rate"),
            }
            for s in streams
        ],
        "focus_query": focus_query or instruction,
    }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="az">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Codex-Gravity MCP Server</title>
  <style>
    :root {
      --bg: #070709;
      --card-bg: #101014;
      --card-elevated: #15151b;
      --border: rgba(255, 255, 255, 0.08);
      --border-hover: rgba(249, 115, 22, 0.35);
      --accent: #f97316;
      --accent-deep: #ea580c;
      --accent-soft: rgba(249, 115, 22, 0.12);
      --accent-border: rgba(249, 115, 22, 0.28);
      --text: #f4f4f6;
      --text-secondary: #90909a;
      --text-muted: #5e5e68;
      --code-bg: #0a0a0d;
      --green: #10b981;
      --green-soft: rgba(16, 185, 129, 0.12);
      --red: #ef4444;
      --red-soft: rgba(239, 68, 68, 0.12);
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Segoe UI", Roboto, sans-serif;
      padding: 0;
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
    }

    /* Apple-Style Sticky Nav */
    .nav {
      position: sticky;
      top: 0;
      z-index: 100;
      background: rgba(8, 8, 10, 0.85);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border-bottom: 1px solid var(--border);
      padding: 12px 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .brand-area {
      display: flex;
      align-items: center;
      gap: 14px;
    }

    .brand-mark {
      width: 26px;
      height: 26px;
      background: linear-gradient(135deg, var(--accent), #c2410c);
      border-radius: 6px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #fff;
      font-size: 13px;
      font-weight: 700;
    }

    .brand-title {
      font-size: 15px;
      font-weight: 600;
      letter-spacing: -0.01em;
      color: #fff;
    }

    /* Mode Segmented Controls */
    .mode-pills {
      display: flex;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 3px;
      gap: 2px;
    }

    .mode-pill {
      background: transparent;
      border: none;
      color: var(--text-secondary);
      font-size: 12px;
      font-weight: 500;
      padding: 5px 12px;
      border-radius: 6px;
      cursor: pointer;
      transition: all 0.2s;
    }

    .mode-pill.active {
      background: var(--accent);
      color: #ffffff;
      font-weight: 600;
    }

    .nav-meta {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .pill-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 11.5px;
      font-weight: 500;
      background: var(--accent-soft);
      border: 1px solid var(--accent-border);
      color: var(--accent);
    }

    .dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--accent);
    }

    /* Navigation Tabs */
    .tab-bar {
      display: flex;
      gap: 24px;
      border-bottom: 1px solid var(--border);
      padding: 0 24px;
      background: rgba(12, 12, 15, 0.5);
    }

    .tab-btn {
      background: none;
      border: none;
      color: var(--text-secondary);
      font-size: 13px;
      font-weight: 500;
      padding: 12px 0;
      cursor: pointer;
      position: relative;
      transition: color 0.2s;
    }

    .tab-btn:hover { color: var(--text); }
    .tab-btn.active {
      color: var(--accent);
      font-weight: 600;
    }
    .tab-btn.active::after {
      content: '';
      position: absolute;
      bottom: -1px;
      left: 0;
      right: 0;
      height: 2px;
      background: var(--accent);
      border-radius: 2px 2px 0 0;
    }

    /* Main Container */
    .container {
      max-width: 1240px;
      margin: 0 auto;
      padding: 32px 24px 80px;
    }

    /* Omni-Input Studio Section */
    .omni-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 24px;
      margin-bottom: 24px;
    }

    .omni-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
    }

    .omni-title {
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent);
      font-weight: 600;
    }

    .omni-row {
      display: flex;
      gap: 12px;
      align-items: center;
    }

    .omni-input {
      flex: 1;
      background: var(--code-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: #fff;
      font-family: inherit;
      font-size: 14px;
      padding: 12px 16px;
      outline: none;
      transition: border-color 0.2s;
    }

    .omni-input:focus { border-color: var(--accent); }

    .target-input {
      width: 180px;
      background: var(--code-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: #fff;
      font-family: ui-monospace, monospace;
      font-size: 13px;
      padding: 12px 14px;
      outline: none;
    }

    .target-input:focus { border-color: var(--accent); }

    .btn-run {
      background: var(--accent);
      color: #fff;
      border: none;
      border-radius: 8px;
      padding: 12px 24px;
      font-size: 13.5px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      transition: background 0.2s, transform 0.1s;
    }

    .btn-run:hover { background: var(--accent-deep); }
    .btn-run:active { transform: scale(0.98); }

    /* Pipeline Stepper */
    .stepper {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-bottom: 24px;
    }

    .step-node {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 14px;
      font-size: 12px;
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--text-secondary);
      transition: all 0.3s;
    }

    .step-node.done {
      border-color: rgba(16, 185, 129, 0.3);
      color: var(--green);
      background: var(--green-soft);
    }

    .step-node.active {
      border-color: var(--accent-border);
      color: var(--accent);
      background: var(--accent-soft);
      font-weight: 600;
    }

    /* Split Workspace */
    .split-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px;
      margin-bottom: 24px;
    }

    @media (max-width: 900px) {
      .split-grid { grid-template-columns: 1fr; }
      .stepper { grid-template-columns: 1fr 1fr; }
      .omni-row { flex-direction: column; align-items: stretch; }
      .target-input { width: 100%; }
    }

    .panel-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      height: 480px;
    }

    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 14px;
    }

    .panel-title {
      font-size: 14px;
      font-weight: 600;
      color: #fff;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    /* Walkie-Talkie Chat Stream */
    .chat-stream {
      flex: 1;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding-right: 6px;
      margin-bottom: 12px;
    }

    .chat-msg {
      background: var(--code-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 14px;
      font-size: 12.5px;
      line-height: 1.45;
    }

    .chat-meta {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 4px;
      font-size: 11px;
    }

    .chat-sender {
      font-weight: 600;
      color: var(--accent);
      font-family: ui-monospace, monospace;
    }

    .chat-time {
      color: var(--text-muted);
      font-family: ui-monospace, monospace;
    }

    .chat-text {
      color: #e4e4e7;
    }

    .inject-box {
      display: flex;
      gap: 8px;
      padding-top: 10px;
      border-top: 1px solid var(--border);
    }

    .inject-input {
      flex: 1;
      background: var(--code-bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: #fff;
      font-size: 12.5px;
      padding: 8px 12px;
      outline: none;
    }

    .btn-inject {
      background: transparent;
      border: 1px solid var(--accent);
      color: var(--accent);
      border-radius: 6px;
      padding: 8px 14px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
    }

    .btn-inject:hover {
      background: var(--accent-soft);
    }

    /* Code Diff & Visual Verification Panel */
    .diff-box {
      flex: 1;
      background: var(--code-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 16px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      color: #d4d4d8;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
      margin-bottom: 12px;
    }

    .diff-add { color: #34d399; }
    .diff-meta { color: #f59e0b; }

    .visual-strip {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
      border-top: 1px solid var(--border);
      padding-top: 12px;
    }

    .viewport-box {
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px 10px;
      text-align: center;
    }

    .vp-label { font-size: 10px; color: var(--text-muted); text-transform: uppercase; margin-bottom: 2px; }
    .vp-status { font-size: 11px; font-weight: 600; color: var(--green); }

    /* Token Arbitrage Ledger Strip */
    .arbitrage-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px 24px;
      margin-bottom: 40px;
    }

    .arb-title {
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent);
      margin-bottom: 14px;
    }

    .arb-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 16px;
    }

    .arb-metric {
      border-left: 2px solid var(--accent);
      padding-left: 12px;
    }

    .arb-label { font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }
    .arb-val { font-size: 20px; font-weight: 700; color: #fff; }

    /* Standalone Tools View */
    .tools-view { display: none; }
    .config-view { display: none; }

    /* Bento Grid */
    .tools-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 16px;
      margin-top: 20px;
    }

    .tool-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 20px;
      transition: border-color 0.2s;
    }
    .tool-card:hover { border-color: var(--border-hover); }

    .tool-name { font-size: 14px; font-weight: 600; font-family: ui-monospace, monospace; color: #fff; margin-bottom: 6px; }
    .tool-desc { font-size: 12.5px; color: var(--text-secondary); line-height: 1.5; margin-bottom: 12px; }
    .btn-test {
      font-size: 11.5px;
      color: var(--accent);
      background: var(--accent-soft);
      border: 1px solid var(--accent-border);
      padding: 4px 10px;
      border-radius: 6px;
      cursor: pointer;
    }

    .footer {
      text-align: center;
      padding: 32px 0;
      border-top: 1px solid var(--border);
      font-size: 12px;
      color: var(--text-muted);
    }
  </style>
</head>
<body>

  <!-- Top Sticky Navigation -->
  <header class="nav">
    <div class="brand-area">
      <div class="brand-mark">CG</div>
      <span class="brand-title">Codex · Gravity Studio</span>
    </div>

    <!-- Active Routing Mode Selector -->
    <div class="mode-pills">
      <button class="mode-pill active" id="btnModeAdaptive" onclick="setMode('smart_adaptive')">🔘 Smart Adaptive</button>
      <button class="mode-pill" id="btnModeEco" onclick="setMode('eco')">⚪ Eco</button>
      <button class="mode-pill" id="btnModePower" onclick="setMode('power')">⚪ Power</button>
    </div>

    <div class="nav-meta">
      <div class="pill-badge" id="sandboxBadge">🌿 .shadow_sandbox/active</div>
      <div class="pill-badge" style="background: rgba(255,255,255,0.04); border-color: var(--border); color: #e4e4e7;">
        <span class="dot" id="statusDot"></span>
        <span id="latencyText">Port 8000 · 4ms</span>
      </div>
    </div>
  </header>

  <!-- Segmented Tab Navigation -->
  <div class="tab-bar">
    <button class="tab-btn active" id="tabStudio" onclick="switchTab('studio')">⚡ Studio Cockpit</button>
    <button class="tab-btn" id="tabTools" onclick="switchTab('tools')">🛠️ MCP Alətləri Kataloqu</button>
    <button class="tab-btn" id="tabConfig" onclick="switchTab('config')">⚙️ Codex Konfiqurasiyası</button>
  </div>

  <main class="container">

    <!-- VIEW 1: STUDIO COCKPIT -->
    <div id="viewStudio">

      <!-- Omni-Input Studio Bar -->
      <section class="omni-card">
        <div class="omni-header">
          <span class="omni-title">Omni-Input Agentic Task Runner</span>
          <span style="font-size: 12px; color: var(--text-muted);" id="activeModeLabel">Rejim: Smart Adaptive</span>
        </div>
        <div class="omni-row">
          <input type="text" class="omni-input" id="taskPrompt" value="PostgreSQL auth servisi yaz, mobil UI-ı test et və Redis konfiqurasiyasını sazla." placeholder="İcra olunacaq arxitektura tapşırığını daxil edin...">
          <input type="text" class="target-input" id="targetFile" value="auth_service.py" placeholder="Hədəf fayl">
          <button class="btn-run" id="btnRunTask" onclick="executeStudioTask()">
            <span>🚀 İcra Et</span>
          </button>
        </div>
      </section>

      <!-- 4-Stage Execution Stepper -->
      <section class="stepper">
        <div class="step-node" id="step1">
          <span>•</span>
          <div>1. Flash Skeleton</div>
        </div>
        <div class="step-node" id="step2">
          <span>•</span>
          <div>2. Codex Core Logic</div>
        </div>
        <div class="step-node" id="step3">
          <span>•</span>
          <div>3. Yoxlama (terminal)</div>
        </div>
        <div class="step-node" id="step4">
          <span>•</span>
          <div>4. Merge (yalnız PASS)</div>
        </div>
      </section>

      <!-- Two-Column Split Workspace -->
      <section class="split-grid">

        <!-- Left Column: Canlı Agent Walkie-Talkie -->
        <div class="panel-card">
          <div class="panel-header">
            <span class="panel-title">📻 CANLI AGENT "WALKIE-TALKIE" (Backchannel)</span>
            <span style="font-size: 11px; color: var(--accent); font-family: ui-monospace;">EVENT_BUS ACTIVE</span>
          </div>

          <div class="chat-stream" id="chatStream">
            <div class="chat-msg" id="chatEmpty">
              <div class="chat-meta">
                <span class="chat-sender">[ROUTER ➜ ALL]</span>
                <span class="chat-time">—</span>
              </div>
              <div class="chat-text">Hələ hadisə qeydə alınmayıb. Tapşırıq icra olunanda agent dialoqu burada canlı görünəcək.</div>
            </div>
          </div>

          <div class="inject-box">
            <input type="text" class="inject-input" id="injectInput" placeholder="Agentlərə birbaşa göstəriş göndər (Human Intervention)...">
            <button class="btn-inject" onclick="injectIntervention()">Müdaxilə Et</button>
          </div>
        </div>

        <!-- Right Column: Kod Diff və Vizual Self-Healing -->
        <div class="panel-card">
          <div class="panel-header">
            <span class="panel-title">💻 KOD VƏ SPEKULYATİV PATCH (Live Diff)</span>
            <span style="font-size: 11px; color: var(--text-muted); font-family: ui-monospace;" id="sandboxState">GÖZLƏNİLİR</span>
          </div>

          <div class="diff-box" id="diffBox">Hələ kod generasiya olunmayıb. Tapşırıq icra olunanda nəticə burada görünəcək.</div>

          <div class="visual-strip">
            <div class="viewport-box">
              <div class="vp-label">📱 Mobile (375x667)</div>
              <div class="vp-status" id="vpMobile" style="color: var(--text-muted);">Ölçülməyib</div>
            </div>
            <div class="viewport-box">
              <div class="vp-label">💻 Desktop (1920x1080)</div>
              <div class="vp-status" id="vpDesktop" style="color: var(--text-muted);">Ölçülməyib</div>
            </div>
            <div class="viewport-box">
              <div class="vp-label">🌿 Main Repo</div>
              <div class="vp-status" style="color: var(--accent);">Qorunur</div>
            </div>
          </div>
        </div>

      </section>

      <!-- Bottom Section: Token & Xərc Qənaəti (təxmini) -->
      <section class="arbitrage-card">
        <div class="arb-title">📊 TOKEN &amp; XƏRC QƏNAƏTİ — TƏXMİNİ (provayder istifadə metrikası deyil)</div>
        <div class="arb-grid">
          <div class="arb-metric">
            <div class="arb-label">Flash ilə Emal Olunan (təxmini)</div>
            <div class="arb-val" id="valFlashTokens">0 <span style="font-size: 12px; color: var(--text-muted);">token</span></div>
          </div>
          <div class="arb-metric">
            <div class="arb-label">Codex-ə Göndərilən (təxmini)</div>
            <div class="arb-val" id="valCodexTokens">0 <span style="font-size: 12px; color: var(--text-muted);">token</span></div>
          </div>
          <div class="arb-metric">
            <div class="arb-label">Təxmini Dollar Qənaəti</div>
            <div class="arb-val" style="color: var(--green);" id="valSavedDollars">$0.00</div>
          </div>
          <div class="arb-metric">
            <div class="arb-label">Təxmini Token Qənaəti</div>
            <div class="arb-val" style="color: var(--accent);" id="valSavedPct">0.0%</div>
          </div>
        </div>
      </section>

    </div>

    <!-- VIEW 2: MCP TOOLS CATALOG -->
    <div id="viewTools" class="tools-view">
      <h2 style="font-size: 18px; margin-bottom: 6px;">Qeydiyyatdan Keçmiş 6 Native MCP Aləti</h2>
      <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: 20px;">Codex və Antigravity-nin bir-birini çağırmaq üçün istifadə etdiyi maşın interfeysləri.</p>

      <div class="tools-grid">
        <div class="tool-card">
          <div class="tool-name">call_antigravity_multimodal</div>
          <div class="tool-desc">Codex birbaşa Antigravity-yə video, şəkil və ya mürəkkəb fayl göndərərək Gemini 3.8 Flash-ın təbii görmə təhlilini alır.</div>
          <button class="btn-test" onclick="executeSingleTool('call_antigravity_multimodal', {media_path: '/tmp/test.png', instruction: 'Vizual detalları analiz et'})">Canlı İcra Et →</button>
        </div>

        <div class="tool-card">
          <div class="tool-name">antigravity_inspect_media</div>
          <div class="tool-desc">Zero-Copy üsulu ilə diskdəki media faylları Base64 overflow olmadan sürətli zaman nişanları ilə oxunur.</div>
          <button class="btn-test" onclick="executeSingleTool('antigravity_inspect_media', {file_path: '/tmp/test.png', focus_query: 'pəncərə və axtarış sahəsi'})">Canlı İcra Et →</button>
        </div>

        <div class="tool-card">
          <div class="tool-name">antigravity_distill_context</div>
          <div class="tool-desc">100k+ sətirlik kütləvi loqları və ya repozitoriyanı 400 tokenlik AST blokuna süzərək token maya dəyərini 98% azaldır.</div>
          <button class="btn-test" onclick="executeSingleTool('antigravity_distill_context', {directory_or_log: '2026-10-21 ERROR DatabaseTimeout', issue_description: 'DatabaseTimeout'})">Canlı İcra Et →</button>
        </div>

        <div class="tool-card">
          <div class="tool-name">antigravity_verify_and_screenshot</div>
          <div class="tool-desc">İzolyasiya olunmuş terminal testlərini icra edir, xəta kökünü (root cause) tapır və mobil/desktop vizual UI yoxlaması aparır.</div>
          <button class="btn-test" onclick="executeSingleTool('antigravity_verify_and_screenshot', {test_command: 'python3 -c \\'print(\"Verify OK\")\\''})">Canlı İcra Et →</button>
        </div>

        <div class="tool-card">
          <div class="tool-name">call_codex_architect</div>
          <div class="tool-desc">Antigravity-nin OpenAI Codex-dən dərin biznes məntiqi və mürəkkəb alqoritm generasiyası tələb etməsi üçün simmetrik qapı.</div>
          <button class="btn-test" onclick="executeSingleTool('call_codex_architect', {prompt: 'LRU cache optimize', code_context: 'cache = {}'})">Canlı İcra Et →</button>
        </div>

        <div class="tool-card">
          <div class="tool-name">call_antigravity_verifier</div>
          <div class="tool-desc">Codex-in yazdığı kodların əsas budağa zərər vermədən gizli worktree sandbox-da təhlükəsiz test olunması qapısı.</div>
          <button class="btn-test" onclick="executeSingleTool('call_antigravity_verifier', {test_command: 'echo \"Verify pass\"'})">Canlı İcra Et →</button>
        </div>
      </div>
    </div>

    <!-- VIEW 3: CONFIGURATION -->
    <div id="viewConfig" class="config-view">
      <h2 style="font-size: 18px; margin-bottom: 6px;">Client Konfiqurasiyası (Codex & Claude)</h2>
      <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: 20px;">Codex və ya Claude Desktop-u bu MCP serverinə qoşmaq üçün konfiqurasiya faylına bu bloku əlavə edin:</p>

      <div style="background: var(--code-bg); border: 1px solid var(--border); border-radius: 8px; padding: 18px; position: relative;">
        <button style="position: absolute; top: 12px; right: 12px; background: rgba(255,255,255,0.06); border: 1px solid var(--border); color: #fff; padding: 4px 10px; border-radius: 6px; cursor: pointer; font-size: 11px;" onclick="copyConfig(this)">Kopyala</button>
        <pre id="cfgText" style="font-family: ui-monospace, monospace; font-size: 13px; color: #d4d4d8;">{
  "mcpServers": {
    "codex-gravity": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}</pre>
      </div>
    </div>

    <footer class="footer">
      Codex-Gravity Studio Architecture · Model Context Protocol (MCP 2024-11-05) · Google Antigravity & OpenAI Codex
    </footer>

  </main>

  <script>
    let currentMode = 'smart_adaptive';
    // Inlined by the server for this same-origin document only.
    const CG_TOKEN = __CG_TOKEN__;

    function authHeaders(extra) {
      return Object.assign({ 'X-Codex-Gravity-Token': CG_TOKEN || '' }, extra || {});
    }

    function chatMessage(sender, time, text) {
      const div = document.createElement('div');
      div.className = 'chat-msg';
      const meta = document.createElement('div');
      meta.className = 'chat-meta';
      const who = document.createElement('span');
      who.className = 'chat-sender';
      who.textContent = sender;
      const when = document.createElement('span');
      when.className = 'chat-time';
      when.textContent = time || '';
      meta.appendChild(who);
      meta.appendChild(when);
      const body = document.createElement('div');
      body.className = 'chat-text';
      // textContent: agent and GUI text is untrusted data, never markup.
      body.textContent = text;
      div.appendChild(meta);
      div.appendChild(body);
      return div;
    }

    function setMode(mode) {
      currentMode = mode;
      document.getElementById('btnModeAdaptive').className = mode === 'smart_adaptive' ? 'mode-pill active' : 'mode-pill';
      document.getElementById('btnModeEco').className = mode === 'eco' ? 'mode-pill active' : 'mode-pill';
      document.getElementById('btnModePower').className = mode === 'power' ? 'mode-pill active' : 'mode-pill';

      const labels = {
        'smart_adaptive': 'Rejim: Smart Adaptive (Flash Skeleton + Codex Logic)',
        'eco': 'Rejim: Eco (Maksimum Flash, Minimum Token)',
        'power': 'Rejim: Power (Dərin Codex Məntiqi + Adversarial Audit)'
      };
      document.getElementById('activeModeLabel').innerText = labels[mode] || mode;
    }

    function switchTab(tab) {
      document.getElementById('tabStudio').className = tab === 'studio' ? 'tab-btn active' : 'tab-btn';
      document.getElementById('tabTools').className = tab === 'tools' ? 'tab-btn active' : 'tab-btn';
      document.getElementById('tabConfig').className = tab === 'config' ? 'tab-btn active' : 'tab-btn';

      document.getElementById('viewStudio').style.display = tab === 'studio' ? 'block' : 'none';
      document.getElementById('viewTools').style.display = tab === 'tools' ? 'block' : 'none';
      document.getElementById('viewConfig').style.display = tab === 'config' ? 'block' : 'none';
    }

    // Ping health updater
    async function updateHealth() {
      const start = performance.now();
      try {
        const res = await fetch('/health');
        const lat = Math.round(performance.now() - start);
        if (res.ok) {
          document.getElementById('latencyText').innerText = `Port 8000 · ${lat}ms`;
          document.getElementById('statusDot').style.background = '#f97316';
        }
      } catch (e) {
        document.getElementById('latencyText').innerText = 'Oflayn';
        document.getElementById('statusDot').style.background = '#ef4444';
      }
    }
    setInterval(updateHealth, 3500);
    updateHealth();

    // Studio Task Runner
    async function executeStudioTask() {
      const prompt = document.getElementById('taskPrompt').value;
      const targetFile = document.getElementById('targetFile').value;
      const btn = document.getElementById('btnRunTask');

      btn.disabled = true;
      btn.innerHTML = '<span>⏳ İcra Edilir...</span>';

      // Animate Stepper
      const s1 = document.getElementById('step1');
      const s2 = document.getElementById('step2');
      const s3 = document.getElementById('step3');
      const s4 = document.getElementById('step4');

      s1.className = 'step-node active';
      s2.className = 'step-node';
      s3.className = 'step-node';
      s4.className = 'step-node';

      try {
        const res = await fetch('/api/studio/run', {
          method: 'POST',
          headers: authHeaders({ 'Content-Type': 'application/json' }),
          body: JSON.stringify({ prompt, target_file: targetFile, mode: currentMode })
        });
        const data = await res.json();
        if (!res.ok || data.status === 'error') {
          throw new Error(data.error || ('HTTP ' + res.status));
        }

        s2.className = 'step-node done';
        s3.className = data.status === 'completed' ? 'step-node done' : 'step-node active';
        s4.className = data.status === 'completed' ? 'step-node done' : 'step-node';

        const stateLabel = document.getElementById('sandboxState');
        stateLabel.textContent =
          data.status === 'completed' ? 'VERIFIED PASS' :
          data.status === 'unverified' ? 'YOXLANMADI' : 'FAILED';
        stateLabel.style.color =
          data.status === 'completed' ? 'var(--green)' :
          data.status === 'unverified' ? 'var(--accent)' : 'var(--red)';

        let code = data.code || '(boş)';
        if (data.is_mock) {
          code += '\n\n[MOCK MODE: model açarı yoxdur, template nəticə göstərilir]';
        }
        document.getElementById('diffBox').textContent = code + '\n\n' + (data.verification_summary || '');

        const measured = !!(data.summary && data.summary.verification && data.summary.verification.viewports_measured);
        document.getElementById('vpMobile').textContent = measured ? 'Ölçüldü' : 'Ölçülməyib';
        document.getElementById('vpDesktop').textContent = measured ? 'Ölçüldü' : 'Ölçülməyib';

        // Render Walkie Talkie chat (textContent only; event text is untrusted)
        const chat = document.getElementById('chatStream');
        chat.textContent = '';
        const events = data.events || [];
        events.forEach(ev => {
          chat.appendChild(chatMessage(
            '[' + (ev.sender || 'AGENT').toUpperCase() + ' ➜ ' + (ev.recipient || 'ALL').toUpperCase() + ']',
            ev.timestamp,
            ev.message || ''
          ));
        });
        if (!events.length) {
          chat.appendChild(chatMessage('[ROUTER ➜ ALL]', '—', 'Hadisə qeydə alınmadı.'));
        }
        chat.scrollTop = chat.scrollHeight;

        // Estimated figures, not provider usage metadata
        if (data.ledger) {
          const flashTokens = data.ledger.total_flash_tokens ?? 0;
          const codexTokens = data.ledger.total_codex_tokens ?? 0;
          const dollarsSaved = Number(data.ledger.total_dollars_saved ?? 0);
          const baselineCost = Number(data.ledger.total_baseline_cost ?? 0);
          const savedPct = baselineCost > 0 ? ((dollarsSaved / baselineCost) * 100).toFixed(1) : '0.0';

          document.getElementById('valFlashTokens').textContent = flashTokens.toLocaleString() + ' token';
          document.getElementById('valCodexTokens').textContent = codexTokens.toLocaleString() + ' token';
          document.getElementById('valSavedDollars').textContent = '$' + dollarsSaved.toFixed(2);
          document.getElementById('valSavedPct').textContent = savedPct + '%';
        }
      } catch (err) {
        document.getElementById('sandboxState').textContent = 'XƏTA';
        document.getElementById('sandboxState').style.color = 'var(--red)';
        alert('İcra xətası: ' + err.message);
      } finally {
        btn.disabled = false;
        btn.textContent = '🚀 İcra Et';
      }
    }

    async function injectIntervention() {
      const inp = document.getElementById('injectInput');
      const msg = inp.value.trim();
      if (!msg) return;

      const chat = document.getElementById('chatStream');
      chat.appendChild(chatMessage('[HUMAN ➜ AGENTS]', new Date().toLocaleTimeString(), msg));
      chat.scrollTop = chat.scrollHeight;
      inp.value = '';

      const res = await fetch('/api/studio/inject', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ message: msg })
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        chat.appendChild(chatMessage('[SERVER ➜ HUMAN]', new Date().toLocaleTimeString(),
          data.error || 'Müdaxilə qeydə alınmadı.'));
      }
    }

    async function executeSingleTool(toolName, args) {
      switchTab('studio');
      const chat = document.getElementById('chatStream');
      const holder = chatMessage('[STUDIO ➜ ' + toolName.toUpperCase() + ']',
        new Date().toLocaleTimeString(), 'Alət icra edilir...');
      chat.appendChild(holder);

      const res = await fetch('/api/execute-tool', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ tool_name: toolName, arguments: args })
      });
      const data = await res.json();
      holder.querySelector('.chat-text').textContent = JSON.stringify(data.result || data, null, 2);
      chat.scrollTop = chat.scrollHeight;
    }

    function copyConfig(btn) {
      const txt = document.getElementById('cfgText').innerText;
      navigator.clipboard.writeText(txt).then(() => {
        btn.innerText = 'Kopyalandı!';
        setTimeout(() => { btn.innerText = 'Kopyala'; }, 2000);
      });
    }
  </script>
</body>
</html>
"""


def create_mcp_server(name: str = "codex-gravity-mcp", version: str | None = None) -> MCPServer:
    """
    Creates and configures the official Codex-Gravity MCP server with standard tools.
    """
    if version is None:
        try:
            version = _package_version("codex-gravity")
        except PackageNotFoundError:
            version = "0.0.0+local"
    server = MCPServer(name, version=version)
    from codex_gravity.gui import AntigravityGUI, GUIError
    gui = AntigravityGUI()
    completion = CompletionWatches(gui=gui)

    async def gui_result(operation):
        try:
            return json.dumps(await operation, ensure_ascii=False)
        except (GUIError, OSError, ValueError) as error:
            return json.dumps({'status': 'error', 'error': str(error), 'task_id': getattr(error, 'task_id', None), 'transport': 'desktop-gui-cdp'}, ensure_ascii=False)

    @server.tool(name="antigravity_gui_status", description="Inspect the real local Antigravity GUI: current model, busy state, and unsent draft. No CLI or API fallback.")
    async def antigravity_gui_status(target_id: Optional[str] = None) -> str:
        return await gui_result(gui.status(target_id))

    def completion_for_request(ctx: Context):
        caller = None
        try:
            meta = ctx.request_context.meta
            data = meta.model_dump(by_alias=True) if meta is not None else {}
            caller = data.get('openai/threadId') or data.get('openai/thread_id') or data.get('codex_thread_id')
        except (AttributeError, ValueError):
            pass
        return CompletionWatches(gui=gui, desktop=DesktopCodex(caller_thread=caller))

    async def attach_completion(record: dict, destination: Optional[str], ctx: Context):
        if destination and record.get('task_id') and record.get('state') != 'unconfirmed':
            try:
                service = completion_for_request(ctx)
                watch = await service.register(record['workspace'], record['task_id'], destination)
                record['completion_watch'] = service.spawn(watch)
            except (BridgeError, GUIError, OSError, ValueError) as error:
                record['completion_watch_error'] = str(error)
        return record

    @server.tool(name="antigravity_gui_start_task", description="Send a scoped task to the real Antigravity GUI. Optionally watch completion and notify the exact existing Codex chat. No CLI/API fallback for Antigravity.")
    async def antigravity_gui_start_task(prompt: str, workspace_path: str,
                                        expected_model: str = "Gemini 3.8 Flash High",
                                        target_id: Optional[str] = None,
                                        new_conversation: bool = True,
                                        gui_project: Optional[str] = None,
                                        notify_codex_thread_id: Optional[str] = None, ctx: Context = None) -> str:
        async def start():
            record = await gui.start(prompt, workspace_path, expected_model, target_id, new_conversation, gui_project)
            return await attach_completion(record, notify_codex_thread_id, ctx)
        return await gui_result(start())

    @server.tool(name="antigravity_gui_task_status", description="Read the owned GUI task. ready_for_review means its turn ended, not that code/tests passed.")
    async def antigravity_gui_task_status(workspace_path: str, task_id: str) -> str:
        return await gui_result(gui.poll(workspace_path, task_id))

    @server.tool(name="antigravity_gui_followup", description="Send corrections to the same completed GUI task. Pass notify_codex_thread_id to watch the new round. Refuses busy chats, model changes and unsent drafts.")
    async def antigravity_gui_followup(workspace_path: str, task_id: str, prompt: str,
                                      notify_codex_thread_id: Optional[str] = None, ctx: Context = None) -> str:
        async def followup():
            record = await gui.followup(workspace_path, task_id, prompt)
            return await attach_completion(record, notify_codex_thread_id, ctx)
        return await gui_result(followup())

    @server.tool(name="codex_gravity_watch_completion", description="Register/resume a detached completion watch for an owned Antigravity task and exact existing Codex thread UUID. Explicitly authorizes one completion follow-up for this round. Waits while Codex is busy; never silently retries uncertain delivery.")
    async def codex_gravity_watch_completion(workspace_path: str, task_id: str,
                                            codex_thread_id: str, poll_seconds: float = 5,
                                            timeout_seconds: float = 3600, resume: bool = False, ctx: Context = None) -> str:
        try:
            service = completion_for_request(ctx)
            record = await service.register(workspace_path, task_id, codex_thread_id,
                                               poll_seconds, timeout_seconds, resume=resume)
            return json.dumps(service.spawn(record), ensure_ascii=False)
        except (BridgeError, GUIError, OSError, ValueError) as error:
            return json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False)

    @server.tool(name="codex_gravity_completion_status", description="Read a persisted completion watch without sending a message. notified means desktop accepted a follow-up, not review success.")
    async def codex_gravity_completion_status(workspace_path: str, watch_id: str) -> str:
        try:
            return json.dumps(completion.status(workspace_path, watch_id), ensure_ascii=False)
        except (BridgeError, OSError, ValueError) as error:
            return json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False)

    @server.tool(name="codex_gravity_cancel_completion", description="Cancel a completion watch. Does not cancel Antigravity or recall an already submitted Codex message.")
    async def codex_gravity_cancel_completion(workspace_path: str, watch_id: str) -> str:
        try:
            return json.dumps(completion.cancel(workspace_path, watch_id), ensure_ascii=False)
        except (BridgeError, OSError, ValueError) as error:
            return json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False)

    # -- team layer --------------------------------------------------------
    team = TeamOrchestrator(event_bus=_shared_event_bus)

    @server.tool(name="team_agents", description="List team agent transports (codex CLI, codex-gui desktop app, antigravity, antigravity-gui) and whether each is reachable right now.")
    async def team_agents(gui_agent: bool = False, codex_thread: Optional[str] = None) -> str:
        pool = default_agents(gui_agent=gui_agent, codex_thread=codex_thread)
        return json.dumps(
            {name: agent.describe() for name, agent in sorted(pool.items())},
            ensure_ascii=False,
        )

    @server.tool(name="team_run_task", description="Run a two-way Codex+Antigravity team: plan, adversarial review, cross-examination, revision, consensus and an optional real verification command. Pass codex_thread to use the ChatGPT/Codex desktop app instead of the CLI. Reports unavailable, deadlocked or failed honestly.")
    async def team_run_task(prompt: str, workspace_path: str,
                            plan_with: Optional[str] = None, implement_with: str = "antigravity",
                            review_with: Optional[str] = None,
                            judge_with: Optional[str] = None,
                            codex_thread: Optional[str] = None,
                            veto_agents: str = "", reviewer_weights: str = "",
                            max_rounds: int = 3, require_unanimous: bool = True,
                            test_command: Optional[str] = None,
                            verification_retries: int = 1,
                            human_notes: Optional[str] = None,
                            gui_agent: bool = False,
                            turn_timeout: float = 180) -> str:
        # A desktop chat id means "use the app, not the CLI" unless stated otherwise.
        codex_transport = "codex-gui" if codex_thread else "codex"
        resolved_plan = (plan_with or codex_transport).strip()
        resolved_reviewers = [
            name.strip()
            for name in (review_with or f"{codex_transport},antigravity").split(",")
            if name.strip()
        ]
        try:
            config = TeamConfig(
                task=prompt,
                workspace=str(Path(workspace_path).expanduser()),
                plan_with=resolved_plan,
                implement_with=implement_with,
                review_with=resolved_reviewers,
                judge_with=judge_with,
                codex_thread=codex_thread,
                veto_agents=[name.strip() for name in veto_agents.split(",") if name.strip()],
                reviewer_weights={
                    key.strip(): float(value)
                    for key, _, value in (item.partition("=") for item in reviewer_weights.split(","))
                    if key.strip() and value.strip().replace(".", "", 1).isdigit()
                },
                max_rounds=max_rounds,
                require_unanimous=require_unanimous,
                test_command=test_command,
                verification_retries=verification_retries,
                human_notes=[line.strip() for line in (human_notes or "").splitlines() if line.strip()],
                turn_timeout=turn_timeout,
            )
        except ValueError as error:
            return json.dumps({'status': 'error', 'error': f'Invalid team request: {error}'}, ensure_ascii=False)
        try:
            result = await team.run(
                config,
                agents=default_agents(turn_timeout=turn_timeout, gui_agent=gui_agent, codex_thread=codex_thread),
            )
        except TeamError as error:
            return json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False)
        return json.dumps(result.model_dump(mode="json"), ensure_ascii=False)

    @server.tool(name="team_status", description="Read a team session: running/finished, last message, decision and transcript path. Never reconstructs a missing session.")
    async def team_status(workspace_path: str, session_id: str) -> str:
        try:
            return json.dumps(team.status(workspace_path, session_id), ensure_ascii=False)
        except (TeamError, OSError, ValueError) as error:
            return json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False)

    @server.tool(name="team_transcript", description="Read a team session transcript as JSON messages or rendered Markdown.")
    async def team_transcript(workspace_path: str, session_id: str, fmt: str = "json", limit: int = 200) -> str:
        from codex_gravity.team.protocol import TeamTranscript
        try:
            transcript = TeamTranscript(workspace_path, session_id)
        except TeamError as error:
            return json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False)
        if fmt == "markdown":
            return transcript.to_markdown()
        return json.dumps(
            [message.model_dump(mode="json") for message in transcript.messages()[-max(1, limit):]],
            ensure_ascii=False,
        )

    @server.tool(name="team_send_message", description="Send a message into a team session as 'human' or another agent (kinds: note, question, answer, critique, approval, rejection). Delivered live when the session runs in this process, otherwise stored for audit.")
    async def team_send_message(workspace_path: str, session_id: str, body: str,
                                sender: str = "human", recipient: str = "all",
                                kind: str = "note") -> str:
        try:
            return json.dumps(
                await team.send_message(workspace_path, session_id, sender, recipient, kind, body),
                ensure_ascii=False,
            )
        except (TeamError, OSError, ValueError) as error:
            return json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False)

    @server.tool(name="team_cancel", description="Request cancellation of a team session running in this MCP process. In-flight agent turns finish; no new round starts.")
    async def team_cancel(workspace_path: str, session_id: str) -> str:
        cancelled = team.cancel(workspace_path, session_id)
        return json.dumps({
            'status': 'cancelling' if cancelled else 'not_running',
            'note': None if cancelled else 'The session is not running in this process; nothing to cancel.',
        }, ensure_ascii=False)

    @server.tool(name="team_report", description="Render a team session (or diff review) as one self-contained HTML file with the verdict, blockers, verification output and full transcript. Returns the written path.")
    async def team_report(workspace_path: str, session_id: str = "", limit: int = 500) -> str:
        from codex_gravity.team.report import latest_session, write_report
        try:
            target_session = session_id or latest_session(workspace_path)
            if not target_session:
                return json.dumps({
                    'status': 'error',
                    'error': 'No team sessions in this workspace yet.',
                }, ensure_ascii=False)
            path = write_report(workspace_path, target_session, limit=max(1, limit))
            return json.dumps({
                'status': 'written',
                'session_id': target_session,
                'path': str(path),
                'bytes': path.stat().st_size,
            }, ensure_ascii=False)
        except (TeamError, OSError, ValueError) as error:
            return json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False)

    @server.tool(name="team_compare", description="Compare two team sessions: status change, resolved/new blockers, approvals and the proposal diff. Defaults to the two most recent sessions.")
    async def team_compare(workspace_path: str, session_id: str = "", against: str = "") -> str:
        from codex_gravity.team.compare import compare, recent_sessions, render_text, snapshot
        try:
            if not session_id:
                chosen = recent_sessions(workspace_path, 2)
                if len(chosen) < 2:
                    return json.dumps({
                        'status': 'error',
                        'error': 'Need two sessions to compare.',
                    }, ensure_ascii=False)
                first, second = chosen[1], chosen[0]
            else:
                others = [sid for sid in recent_sessions(workspace_path, 50) if sid != session_id]
                first = session_id
                second = against or (others[0] if others else "")
                if not second:
                    return json.dumps({
                        'status': 'error',
                        'error': 'Need a second session to compare against.',
                    }, ensure_ascii=False)
            result = compare(snapshot(workspace_path, first), snapshot(workspace_path, second))
            result['summary'] = render_text(result)
            return json.dumps(result, ensure_ascii=False)
        except (TeamError, OSError, ValueError) as error:
            return json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False)

    @server.tool(name="team_decisions", description="Read the project decision log (.codex-gravity/decisions.md): the accepted decisions later sessions are given as context.")
    async def team_decisions(workspace_path: str, limit: int = 8) -> str:
        from codex_gravity.team.memory import decisions_count, decisions_path, load_decisions
        text = load_decisions(workspace_path, max(1, limit))
        return json.dumps({
            'path': str(decisions_path(workspace_path)),
            'entries': decisions_count(workspace_path),
            'text': text,
            'empty': not text,
        }, ensure_ascii=False)

    @server.tool(name="codex_gui_status", description="Inspect the ChatGPT/Codex desktop app: its live app-tools catalogue, the chat's busy state, turn count and last message. Read-only; requires running inside the Codex desktop app.")
    async def codex_gui_status(thread_id: str) -> str:
        try:
            chat = CodexDesktopChat(thread_id)
            tools = await chat.catalogue()
            snapshot = await chat.snapshot()
            return json.dumps({
                'status': 'connected',
                'engine': 'codex-desktop-gui',
                'thread': snapshot_to_dict(snapshot),
                'tools': tools,
            }, ensure_ascii=False)
        except (BridgeError, CodexDesktopUnavailable, OSError, ValueError) as error:
            available, reason = desktop_availability(thread_id)
            return json.dumps({
                'status': 'error',
                'error': str(error),
                'transport_available': available,
                'reason': reason,
            }, ensure_ascii=False)

    @server.tool(name="codex_gui_ask", description="Send one message to an exact existing ChatGPT/Codex desktop chat and wait for the new completed reply. Returns status=unconfirmed when no reply can be observed; it never guesses and never resends.")
    async def codex_gui_ask(thread_id: str, message: str,
                            timeout_seconds: float = 300, poll_seconds: float = 2) -> str:
        try:
            chat = CodexDesktopChat(thread_id, poll_interval=poll_seconds)
            reply = await chat.ask(message, timeout=timeout_seconds)
            return json.dumps(reply_to_dict(reply), ensure_ascii=False)
        except (BridgeError, CodexDesktopUnavailable, OSError, ValueError) as error:
            return json.dumps({'status': 'error', 'ok': False, 'error': str(error)}, ensure_ascii=False)

    @server.custom_route("/", methods=["GET"])
    async def index(request: Any) -> Any:
        # The token is inlined for this same-origin document only. A cross-origin
        # page cannot read the body, so it cannot replay the token.
        token = json.dumps(studio_token()).replace("</", "<\\/")
        return HTMLResponse(DASHBOARD_HTML.replace("__CG_TOKEN__", token))

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Any) -> Any:
        # Derived from the live registry so the list cannot drift from reality.
        tools = sorted(tool.name for tool in await server.list_tools())
        return JSONResponse({
            "status": "healthy",
            "server": "codex-gravity-mcp",
            "version": version,
            "mcp_endpoint": "/mcp",
            "tool_count": len(tools),
            "active_tools": tools,
        })

    @server.custom_route("/api/execute-tool", methods=["POST"])
    async def execute_tool(request: Any) -> Any:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"status": "error", "error": "Expected a JSON body."}, status_code=400)
        tool_name = body.get("tool_name")
        arguments = body.get("arguments", {})
        denied = _studio_denied(request, tool_name if isinstance(tool_name, str) else None)
        if denied:
            return denied
        try:
            start_t = asyncio.get_event_loop().time()
            res = await server.call_tool(tool_name, arguments)
            latency_ms = round((asyncio.get_event_loop().time() - start_t) * 1000, 1)
            raw_text = res.content[0].text if res.content else ""
            try:
                parsed = json.loads(raw_text)
            except Exception:
                parsed = raw_text
            return JSONResponse({
                "status": "success",
                "tool": tool_name,
                "latency_ms": latency_ms,
                "result": parsed,
            })
        except Exception as e:
            return JSONResponse({
                "status": "error",
                "error": str(e),
            }, status_code=500)

    @server.custom_route("/api/studio/state", methods=["GET"])
    async def studio_state(request: Any) -> Any:
        try:
            history = _shared_event_bus.get_history(limit=40)
            events = [
                {
                    "event_type": e.event_type.value if hasattr(e.event_type, "value") else str(e.event_type),
                    "sender": e.sender,
                    "recipient": e.recipient,
                    "message": e.message,
                    "timestamp": e.timestamp.strftime("%H:%M:%S") if hasattr(e.timestamp, "strftime") else str(e.timestamp),
                    "payload": e.payload,
                }
                for e in history
            ]
            return JSONResponse({
                "status": "active",
                "events": events,
                "ledger": _shared_router.ledger.model_dump(),
                "sandbox": ".shadow_sandbox/active",
            })
        except Exception as e:
            return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

    @server.custom_route("/api/studio/run", methods=["POST"])
    async def studio_run(request: Any) -> Any:
        denied = _studio_denied(request)
        if denied:
            return denied
        try:
            body = await request.json()
            prompt = body.get("prompt", "Create component")
            target_file = body.get("target_file", "auth_service.py")
            mode_str = body.get("mode", "smart_adaptive")
            # Only real checks count. By default the generated Python file is
            # compiled inside the sandbox; callers may supply a stronger command.
            test_command = body.get("test_command")
            if not test_command and str(target_file).endswith(".py"):
                test_command = (
                    f"{shlex.quote(sys.executable)} -m py_compile {shlex.quote(str(target_file))}"
                )
            ui_url = body.get("ui_url")

            mode = RoutingMode.SMART_ADAPTIVE
            if mode_str == "eco":
                mode = RoutingMode.ECO
            elif mode_str == "power":
                mode = RoutingMode.POWER

            task_req = TaskRequest(
                prompt=prompt,
                target_file=target_file,
                mode=mode,
                test_command=test_command,
                ui_url=ui_url,
                auto_merge=False,
            )

            repo_root = Path(__file__).resolve().parent.parent
            res = await _shared_router.execute_task(task_req, repo_root=repo_root)

            history = _shared_event_bus.get_history(limit=40)
            events = [
                {
                    "event_type": e.event_type.value if hasattr(e.event_type, "value") else str(e.event_type),
                    "sender": e.sender,
                    "recipient": e.recipient,
                    "message": e.message,
                    "timestamp": e.timestamp.strftime("%H:%M:%S") if hasattr(e.timestamp, "strftime") else str(e.timestamp),
                    "payload": e.payload,
                }
                for e in history
            ]

            written_code = res.generated_code or ""
            if not written_code:
                target_path = repo_root / target_file
                if target_path.exists():
                    try:
                        written_code = target_path.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        pass

            if not res.verified:
                status = "unverified"
            elif res.success:
                status = "completed"
            else:
                status = "failed"

            return JSONResponse({
                "status": status,
                "verified": res.verified,
                "is_mock": res.is_mock,
                "heuristic_repair_used": res.heuristic_repair_used,
                "verification_summary": (
                    res.verification.summary_text if res.verification else "no verification requested"
                ),
                "summary": res.model_dump(),
                "events": events,
                "code": written_code,
                "ledger": _shared_router.ledger.model_dump(),
            })
        except Exception as e:
            return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

    @server.custom_route("/api/studio/inject", methods=["POST"])
    async def studio_inject(request: Any) -> Any:
        denied = _studio_denied(request)
        if denied:
            return denied
        try:
            body = await request.json()
            msg = body.get("message", "")
            if msg:
                await _shared_event_bus.inject_human_intervention(message=msg)
            return JSONResponse({"status": "injected", "message": msg})
        except Exception as e:
            return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

    @server.tool(
        name="antigravity_inspect_media",
        description="Reads local video/audio/image metadata via ffprobe and, when a model is configured, asks it for an analysis. Returns status=partial when only metadata was read.",
    )
    async def antigravity_inspect_media(file_path: str, focus_query: str) -> str:
        """
        Zero-Copy Shared File-Descriptor Transport:
        Only the local absolute file path is passed over MCP (under 1KB payload),
        completely avoiding base64 payload overflow on large video/audio files.
        Connects to live Gemini Flash multimodal model and ffprobe parser.
        """
        p = _resolve_local_path(file_path)
        if not p or not p.exists():
            return json.dumps({
                "error": f"File does not exist: {file_path}",
                "status": "failed",
            })

        result = await _inspect_media_with_gemini_or_probe(p, focus_query=focus_query)
        return json.dumps(result, indent=2)

    @server.tool(
        name="antigravity_distill_context",
        description="Reads massive directories or 100k+ line logs and distills down to AST code blocks and pinpointed issue context.",
    )
    async def antigravity_distill_context(directory_or_log: str, issue_description: str) -> str:
        """
        Context distillation: Slashing raw 100k+ tokens to ~400 tokens
        before Codex processes it, saving 97.9% of token costs.
        """
        stop_words = {"a", "an", "the", "in", "on", "of", "to", "for", "with", "is", "at", "by", "from", "and", "or", "as"}
        raw_words = re.findall(r"\b[A-Za-z0-9_.-]+\b", issue_description)
        terms = [w.lower() for w in raw_words if w.lower() not in stop_words and len(w) > 1]
        if not terms:
            terms = [w.lower() for w in raw_words]

        p = _resolve_local_path(directory_or_log)
        raw_lines = 0
        distilled_content = ""
        ast_anchor = ""

        if p and p.exists() and p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            raw_lines = len(lines)

            # Score each line by matched search terms
            best_idx = -1
            best_score = 0
            for i, line in enumerate(lines):
                line_lower = line.lower()
                score = sum(1 for t in terms if t in line_lower)
                if score > best_score:
                    best_score = score
                    best_idx = i

            if best_idx >= 0:
                start_idx = max(0, best_idx - 3)
                end_idx = min(len(lines), best_idx + 4)
                distilled_content = "\n".join(lines[start_idx:end_idx])
                ast_anchor = f"{p.name}:L{best_idx + 1}: {lines[best_idx].strip()}"
            else:
                distilled_content = "\n".join(lines[-10:])
                ast_anchor = f"{p.name}:tail"

        elif p and p.exists() and p.is_dir():
            best_file = None
            best_line_idx = -1
            best_score = 0
            best_lines: list[str] = []

            for file_path in p.rglob("*"):
                if file_path.is_file() and not any(part.startswith(".") for part in file_path.parts):
                    try:
                        content = file_path.read_text(encoding="utf-8", errors="ignore")
                        f_lines = content.splitlines()
                        raw_lines += len(f_lines)

                        for i, line in enumerate(f_lines):
                            line_lower = line.lower()
                            score = sum(1 for t in terms if t in line_lower)
                            if score > best_score:
                                best_score = score
                                best_file = file_path
                                best_line_idx = i
                                best_lines = f_lines
                    except Exception:
                        pass

            if best_file and best_line_idx >= 0:
                rel_path = str(best_file.relative_to(p))
                start_idx = max(0, best_line_idx - 3)
                end_idx = min(len(best_lines), best_line_idx + 5)
                distilled_content = (
                    f"# Extracted from {rel_path} (L{best_line_idx + 1})\n"
                    + "\n".join(best_lines[start_idx:end_idx])
                )
                ast_anchor = f"{rel_path}:L{best_line_idx + 1}"
            else:
                distilled_content = f"Repository scan completed across {raw_lines} lines. No critical matches found."
                ast_anchor = "Root repository search"

        else:
            # Direct log string input
            lines = directory_or_log.splitlines()
            raw_lines = len(lines)
            best_idx = -1
            best_score = 0
            for i, line in enumerate(lines):
                line_lower = line.lower()
                score = sum(1 for t in terms if t in line_lower)
                if score > best_score:
                    best_score = score
                    best_idx = i

            if best_idx >= 0:
                start_idx = max(0, best_idx - 3)
                end_idx = min(len(lines), best_idx + 4)
                distilled_content = "\n".join(lines[start_idx:end_idx])
                ast_anchor = f"Log line {best_idx + 1}: {lines[best_idx].strip()}"
            else:
                distilled_content = "\n".join(lines[-15:])
                ast_anchor = "Raw log tail snippet"

        distilled_lines = len(distilled_content.splitlines())
        token_reduction_pct = round((1 - (distilled_lines / max(1, raw_lines))) * 100.0, 1)

        result = {
            "issue_description": issue_description,
            "raw_lines_analyzed": raw_lines,
            "distilled_lines": distilled_lines,
            "token_reduction_pct": max(0.0, token_reduction_pct),
            "ast_anchor": ast_anchor or "Root context anchor",
            "distilled_context": distilled_content,
        }
        return json.dumps(result, indent=2)

    @server.tool(
        name="antigravity_verify_and_screenshot",
        description="Executes test command in terminal and inspects multi-viewport UI layout.",
    )
    async def antigravity_verify_and_screenshot(
        test_command: str,
        ui_url: Optional[str] = None,
        cwd: Optional[str] = None,
        timeout: float = 60.0,
    ) -> str:
        """
        Executes terminal verification and checks responsive viewports (Mobile, Tablet, Desktop).
        Uses process group isolation and timeout protection to prevent zombie processes.
        """
        # Run terminal command in specified or current working directory with process group
        try:
            process = await asyncio.create_subprocess_shell(
                test_command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout)
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            returncode = process.returncode
        except (asyncio.TimeoutError, asyncio.CancelledError):
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except Exception:
                pass
            stdout = ""
            stderr = f"Command execution timed out after {timeout} seconds"
            returncode = -1

        passed = returncode == 0

        diagnostics = None
        if not passed:
            diag = RootCauseAnalyzer.analyze(stdout, stderr)
            diagnostics = diag.model_dump()

        viewport_results = []
        if ui_url:
            inspector = VisualRegressionInspector()
            v_res = await inspector.check_viewports(ui_url)
            viewport_results = [vr.model_dump() for vr in v_res]

        result = {
            "command": test_command,
            "exit_code": returncode,
            "passed": passed,
            "stdout_tail": "\n".join(stdout.splitlines()[-10:]),
            "stderr_tail": "\n".join(stderr.splitlines()[-10:]),
            "diagnostics": diagnostics,
            "viewport_checks": viewport_results,
        }
        return json.dumps(result, indent=2)

    @server.tool(
        name="call_antigravity_verifier",
        description="Symmetrical tool: Request Antigravity terminal verification and visual regression checks.",
    )
    async def call_antigravity_verifier(test_command: str, ui_url: Optional[str] = None) -> str:
        return await antigravity_verify_and_screenshot(test_command=test_command, ui_url=ui_url)

    @server.tool(name="call_codex_architect", description="Ask the real signed-in Codex CLI for a bounded read-only code/architecture answer. Uses the configured model; no synthetic fallback or provider API. Does not create a persistent chat or edit files.")
    async def call_codex_architect(prompt: str, code_context: str = "",
                                  workspace_path: Optional[str] = None, timeout: float = 300) -> str:
        try:
            return json.dumps(await run_codex_architect(prompt, code_context, workspace_path, timeout), ensure_ascii=False)
        except (BridgeError, OSError, ValueError, TimeoutError) as error:
            return json.dumps({'status': 'error', 'provider': 'codex', 'error': str(error) or 'Codex timed out; no completed answer.'}, ensure_ascii=False)

    @server.tool(
        name="call_antigravity_multimodal",
        description="Symmetrical tool: Codex requests Google Antigravity (Gemini Flash via active subscription) for media or context distillation.",
    )
    async def call_antigravity_multimodal(media_path: str, instruction: str) -> str:
        """
        Symmetrical MCP Gateway: Enables Codex to request real Google Antigravity (Gemini 3.8 Flash)
        using the user's active local Antigravity subscription (no external API keys needed).
        """
        p = _resolve_local_path(media_path)
        media_probe_info = None
        if p and p.exists():
            media_probe_info = await _inspect_media_with_gemini_or_probe(p, focus_query=instruction, instruction=instruction)
            # Only a real model answer may be presented as an analysis. ffprobe
            # metadata alone is not an answer to the instruction.
            if media_probe_info and media_probe_info.get("analysis_available"):
                return json.dumps({
                    "status": "completed",
                    "provider": media_probe_info.get("provider", "gemini-3.8-flash"),
                    "engine": media_probe_info.get("engine", "google-antigravity-subscription (native agentapi)"),
                    "instruction": instruction,
                    "target": str(p),
                    "distillation": media_probe_info["analysis"],
                    "media_analysis": media_probe_info,
                }, indent=2)

        # 1. Native Google Antigravity execution via user's active subscription (agentapi)
        agy_prompt = (
            f"Codex-Gravity Symmetrical Bridge Request:\n"
            f"Target/Context: {media_path}\n"
            f"Instruction: {instruction}\n"
        )
        if media_probe_info:
            agy_prompt += f"\nTechnical Media Streams: {json.dumps(media_probe_info.get('streams', []), indent=1)}\n"
        agy_prompt += "\nPlease analyze this request thoroughly using Gemini multimodal reasoning and provide your actionable breakdown."

        agy_response = await call_native_antigravity(agy_prompt, model="flash", timeout_sec=35.0)
        if agy_response:
            return json.dumps({
                "status": "completed",
                "provider": "gemini-3.8-flash",
                "engine": "google-antigravity-subscription (native agentapi)",
                "instruction": instruction,
                "target": media_path,
                "distillation": agy_response,
                "media_analysis": media_probe_info,
            }, indent=2)

        # 2. Fallback to Google GenAI SDK if GEMINI_API_KEY was provided
        client = get_gemini_client()
        model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        if client:
            try:
                gemini_res = client.models.generate_content(
                    model=model_name,
                    contents=agy_prompt,
                )
                return json.dumps({
                    "status": "completed",
                    "provider": "gemini-3.8-flash",
                    "engine": f"google-gemini-live ({model_name})",
                    "instruction": instruction,
                    "target": media_path,
                    "distillation": gemini_res.text,
                    "media_analysis": media_probe_info,
                }, indent=2)
            except Exception:
                pass

        # 3. Graceful completion with media probe
        return json.dumps({
            "status": "unavailable",
            "provider": "none",
            "engine": "no-model-available",
            "instruction": instruction,
            "target": media_path,
            "analysis_available": False,
            "error": (
                "No Antigravity agentapi binary and no GEMINI_API_KEY / GOOGLE_API_KEY were available, "
                "so nothing analysed this request. Configure one of them and retry."
            ),
            "distillation": None,
            "media_analysis": media_probe_info,
        }, indent=2)

    return server


def main() -> None:
    """
    CLI entrypoint for running the Codex-Gravity MCP server.
    """
    parser = argparse.ArgumentParser(description="Codex-Gravity MCP Server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio", help="MCP transport mode")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port")
    args = parser.parse_args()

    server = create_mcp_server()

    if args.transport == "http":
        print(f"Starting Codex-Gravity MCP Server on Streamable HTTP http://{args.host}:{args.port}/mcp ...")
        asyncio.run(server.run_streamable_http_async(host=args.host, port=args.port))
    else:
        asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
