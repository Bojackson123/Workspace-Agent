"""FastAPI entry point for the Dual-MCP Agent Backend.

Receives Google Chat webhooks, verifies the inbound JWT, and forwards
the payload to the chat event handler.
"""

import logging
import os

# Logging and tracing must be configured before importing any application
# module so that module-level loggers and the OTel tracer provider are both
# ready by the time chat.py / sessions.py pull them in.
from logging_setup import configure_logging  # noqa: E402
from tracing_setup import configure_tracing  # noqa: E402

configure_logging()
configure_tracing()

# google-genai inspects these on import, so they must be set before any
# downstream module pulls google.genai into the import graph.
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "1"
os.environ.setdefault("LOCATION", "us-central1")

from dotenv import load_dotenv  # noqa: E402 — load env vars before our imports

load_dotenv()

from typing import Any  # noqa: E402

from fastapi import BackgroundTasks, Depends, FastAPI, Request  # noqa: E402

from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: E402

from chat import handle_event  # noqa: E402
from security import verify_chat_jwt  # noqa: E402

app = FastAPI(title="Dual-MCP Agent Backend")
FastAPIInstrumentor.instrument_app(app)


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
