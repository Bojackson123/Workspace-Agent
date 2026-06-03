"""FastAPI entry point for the Dual-MCP Agent Backend.

Receives Google Chat webhooks, verifies the inbound JWT, and forwards
the payload to the chat event handler.
"""

import logging
import os

# Configure root logger before any module-level log.* call so app logs
# (chat.py, sessions.py, chat_client.py) actually reach Cloud Logging.
# The Python default level is WARNING, which silently drops every
# log.info() in the app.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# google-genai inspects these on import, so they must be set before any
# downstream module pulls google.genai into the import graph.
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "1"
os.environ.setdefault("LOCATION", "us-central1")

from dotenv import load_dotenv  # noqa: E402 — load env vars before our imports

load_dotenv()

from typing import Any  # noqa: E402

from fastapi import BackgroundTasks, Depends, FastAPI, Request  # noqa: E402

from chat import handle_event  # noqa: E402
from security import verify_chat_jwt  # noqa: E402

app = FastAPI(title="Dual-MCP Agent Backend")


@app.post("/")
async def chat_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    _claims: dict = Depends(verify_chat_jwt),
) -> dict[str, Any]:
    """Entry point for Google Chat webhooks.

    ``verify_chat_jwt`` runs first via FastAPI's dependency injection;
    the request body is only parsed after the token has been verified.
    Long-running LLM workflows are dispatched into *background_tasks*
    so the webhook responds inside Chat's "not responding" window;
    their final reply is posted back via the Chat REST API.
    """
    return await handle_event(await request.json(), background_tasks)
