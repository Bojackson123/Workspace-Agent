"""Dev entry point for adk web.

Usage (from the agentic-backend/ directory):

    uv run adk web dev/

Required env vars (set in agentic-backend/.env):

    DEV_USER_EMAIL   Your email — used to impersonate you via DWD in context-mcp
    DEV_WORKFLOW     Slash command to test: /meeting (default) or /review

The circular import between agent.py and workflows._helpers is broken here
by importing workflows fully before importing anything from agent.py.
"""

import asyncio
import concurrent.futures
import os
import sys

# Ensure the parent directory (agentic-backend/) is on the path so all the
# app modules resolve correctly when adk web launches from dev/.
_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent not in sys.path:
    sys.path.insert(0, _parent)

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "1")
os.environ.setdefault("LOCATION", "us-central1")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")  # Vertex AI SDK reads this

from dotenv import load_dotenv

# load_dotenv() without a path searches upward from cwd, which is
# agentic-backend/ when you run `adk web dev/` from there.
load_dotenv()

# ── Break the circular import ──────────────────────────────────────────────
# agent.py → workflows._base → workflows.__init__ → _helpers → agent
# Loading workflows first (before agent) means agent.py's import of
# workflows._base finds a fully-initialised package in sys.modules.
import workflows  # noqa: E402 — must come before agent

import agent as _agent_mod  # noqa: E402 — safe now that workflows is loaded

# ── Build the workflow agent ───────────────────────────────────────────────
_user_email = os.environ.get("DEV_USER_EMAIL")
_workflow_name = os.environ.get("DEV_WORKFLOW", "/meeting")

if not _user_email:
    raise RuntimeError(
        "DEV_USER_EMAIL is not set. Add it to agentic-backend/.env:\n"
        "  DEV_USER_EMAIL=you@yourcompany.com\n"
        "  DEV_WORKFLOW=/meeting   # or /review"
    )

_wf = workflows.get_workflow_by_name(_workflow_name)
if _wf is None:
    raise RuntimeError(
        f"DEV_WORKFLOW={_workflow_name!r} is not a registered workflow. "
        f"Registered: {[w.command_name for w in workflows.WORKFLOWS.values()]}"
    )

# asyncio.run() can't be called from a running event loop (ADK web loads
# agent modules lazily, inside uvicorn's loop). Run in a new thread that
# has its own fresh event loop instead.
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    root_agent = pool.submit(asyncio.run, _wf.build_agent(_user_email)).result()
