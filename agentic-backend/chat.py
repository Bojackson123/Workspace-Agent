"""Google Chat webhook event handling.

Translates an inbound Chat payload into an agent run and renders the
agent's response into the JSON envelope that Chat expects back.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Final

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent import build_agent_for_user
from config import settings

log = logging.getLogger(__name__)

# Subset of Google Chat event types the agent reacts to.
EVENT_ADDED_TO_SPACE: Final = "ADDED_TO_SPACE"
EVENT_MESSAGE: Final = "MESSAGE"

WELCOME_MESSAGE: Final = (
    "Hello! I am your Dual-MCP Workspace Assistant. "
    "How can I help you today?"
)

# A single in-memory session service is enough because each webhook
# invocation creates its own session — the webhook is stateless.
_session_service = InMemorySessionService()


@dataclass(frozen=True, slots=True)
class ChatEvent:
    """A minimal, well-typed view of an inbound Google Chat payload."""

    event_type: str
    user_email: str | None
    prompt: str

    @classmethod
    def from_payload(cls, body: dict) -> "ChatEvent":
        """Extract the fields this agent cares about from a Chat event body."""
        return cls(
            event_type=body.get("type", ""),
            user_email=body.get("user", {}).get("email"),
            prompt=body.get("message", {}).get("text", ""),
        )


def chat_text(text: str) -> dict[str, str]:
    """Wrap *text* in the JSON envelope Chat expects from a webhook response."""
    return {"text": text}


async def handle_event(body: dict) -> dict[str, str]:
    """Route an inbound Chat payload to the right handler."""
    event = ChatEvent.from_payload(body)

    if event.event_type == EVENT_ADDED_TO_SPACE:
        return chat_text(WELCOME_MESSAGE)

    if event.event_type == EVENT_MESSAGE:
        return await _handle_message(event)

    return chat_text(f"Event type {event.event_type!r} is not supported.")


async def _handle_message(event: ChatEvent) -> dict[str, str]:
    """Run the agent for a ``MESSAGE`` event and return the formatted reply."""
    if not event.user_email:
        return chat_text("Error: Could not extract user email from payload.")

    try:
        reply = await _run_agent(event.user_email, event.prompt)
    except Exception:
        # The full traceback is useful to operators but must not leak to chat.
        log.exception("Agent run failed for user %s", event.user_email)
        return chat_text(
            "I ran into an unexpected error while processing that request."
        )

    return chat_text(reply or "I wasn't able to generate a response.")


async def _run_agent(user_email: str, prompt: str) -> str:
    """Drive a single prompt through a per-request ADK agent.

    Returns the text of the agent's final response, or an empty string
    if the agent produced no terminal output.
    """
    cfg = settings()
    agent = await build_agent_for_user(user_email=user_email)

    runner = Runner(
        agent=agent,
        app_name=cfg.app_name,
        session_service=_session_service,
    )

    # Each webhook is a self-contained turn — give it its own session.
    session_id = str(uuid.uuid4())
    await _session_service.create_session(
        app_name=cfg.app_name,
        user_id=user_email,
        session_id=session_id,
    )

    message = types.Content(
        role="user",
        parts=[types.Part(text=prompt)],
    )

    final_text = ""
    async for event in runner.run_async(
        user_id=user_email,
        session_id=session_id,
        new_message=message,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = event.content.parts[0].text or ""

    return final_text
