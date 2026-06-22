"""Typed views of inbound Google Chat payloads and their parsing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Subset of Google Chat event types the agent reacts to.
EVENT_ADDED_TO_SPACE: Final = "ADDED_TO_SPACE"
EVENT_MESSAGE: Final = "MESSAGE"
EVENT_CARD_CLICKED: Final = "CARD_CLICKED"

WELCOME_MESSAGE: Final = (
    "Hello! I am your Dual-MCP Workspace Assistant. "
    "Type `/help` to see available commands, or just ask me a question."
)


@dataclass(frozen=True, slots=True)
class Attachment:
    """A file attached to an inbound Chat message.

    ``resource_name`` identifies uploaded-content attachments (downloadable via
    the Chat media endpoint); ``drive_file_id`` is set instead when the user
    attached an existing Google Drive file.
    """

    content_name: str
    content_type: str
    resource_name: str
    drive_file_id: str


@dataclass(frozen=True, slots=True)
class ChatEvent:
    """A minimal, well-typed view of an inbound Google Chat payload."""

    event_type: str
    user_email: str | None
    # Chat user resource name (``users/USER_ID``) and display name.
    # ``user_resource`` is used to build a clickable ``<users/ID>``
    # mention in the public anchor message; ``user_display`` is a
    # human-readable fallback for logs and rendered text.
    user_resource: str
    user_display: str
    prompt: str
    slash_command_id: int | None
    thread_name: str
    # Raw Chat resource names needed to post async replies back into the
    # same conversation via the Chat REST API. ``conversation_key`` is
    # the session bucket (space-or-thread depending on space type);
    # ``space_resource`` / ``message_thread`` are the literal names
    # Chat expects in REST calls.
    space_resource: str
    message_thread: str
    # Files attached to the inbound message (e.g. an RFI .xlsx/.docx).
    attachments: tuple[Attachment, ...] = ()

    @classmethod
    def from_payload(cls, body: dict) -> "ChatEvent":
        """Extract the fields this agent cares about from a Chat event body."""
        message = body.get("message") or {}
        slash_command = message.get("slashCommand") or {}
        raw_command_id = slash_command.get("commandId")
        try:
            command_id = int(raw_command_id) if raw_command_id is not None else None
        except (TypeError, ValueError):
            command_id = None

        space = body.get("space") or {}
        space_resource = space.get("name") or ""
        message_thread = (message.get("thread") or {}).get("name") or ""

        thread_name = _conversation_key(body, message)

        user = body.get("user") or {}

        return cls(
            event_type=body.get("type", ""),
            user_email=user.get("email"),
            user_resource=user.get("name") or "",
            user_display=user.get("displayName") or "",
            prompt=_clean_prompt(message),
            slash_command_id=command_id,
            thread_name=thread_name,
            space_resource=space_resource,
            message_thread=message_thread,
            attachments=_parse_attachments(message),
        )


@dataclass(frozen=True, slots=True)
class CardClickedEvent:
    """A minimal, well-typed view of a CARD_CLICKED payload."""

    user_email: str
    space_resource: str
    message_name: str    # card message to update in place
    thread_name: str     # session lookup key
    invoker_email: str   # stored in button actionParameters
    decision: str        # "assign" | "skip"
    invoked_function: str  # onClick action function — routes the handler
    params: dict         # all button actionParameters (key -> value)
    form_inputs: dict    # raw common.formInputs dict

    @classmethod
    def from_payload(cls, body: dict) -> "CardClickedEvent":
        user = body.get("user") or {}
        space = body.get("space") or {}
        message = body.get("message") or {}
        thread = message.get("thread") or {}
        action = body.get("action") or {}
        common = body.get("common") or {}
        params = {p["key"]: p["value"] for p in (action.get("parameters") or [])}
        form_inputs = common.get("formInputs") or {}
        return cls(
            user_email=user.get("email") or "",
            space_resource=space.get("name") or "",
            message_name=message.get("name") or "",
            thread_name=thread.get("name") or space.get("name") or "",
            invoker_email=params.get("invoker_email") or user.get("email") or "",
            decision=params.get("decision") or "",
            invoked_function=action.get("function") or common.get("invokedFunction") or "",
            params=params,
            form_inputs=form_inputs,
        )


def _conversation_key(body: dict, message: dict) -> str:
    """Return the stable key identifying the conversation a message belongs to.

    We always key by ``message.thread.name`` so each Chat thread gets
    its own isolated session and memory. In DMs and group chats Chat
    creates a fresh thread for every top-level message — that's exactly
    the boundary we want: each new top-level prompt starts a fresh
    conversation, while replies inside a thread continue it. We fall
    back to ``space.name`` only when thread.name is unexpectedly empty.
    """
    space = body.get("space") or {}
    thread_name = (message.get("thread") or {}).get("name") or ""
    space_name = space.get("name") or ""
    return thread_name or space_name


def _clean_prompt(message: dict) -> str:
    """Return ``message.text`` with any leading slash command stripped.

    Chat tags the slash command's character range via a ``SLASH_COMMAND``
    annotation; using ``length`` from that annotation is more robust
    than splitting on whitespace.
    """
    text = message.get("text") or ""
    for annotation in message.get("annotations") or []:
        if annotation.get("type") == "SLASH_COMMAND":
            length = annotation.get("length")
            if isinstance(length, int) and length > 0:
                return text[length:].lstrip()
    if text.startswith("/"):
        _, _, rest = text.partition(" ")
        return rest.lstrip()
    return text


def _parse_attachments(message: dict) -> tuple[Attachment, ...]:
    """Extract attachment metadata from a Chat message payload.

    Chat exposes attachments under ``message.attachment`` (singular key, list
    value). Uploaded files carry an ``attachmentDataRef.resourceName``;
    Drive-sourced files carry a ``driveDataRef.driveFileId``.
    """
    out: list[Attachment] = []
    for att in message.get("attachment") or []:
        data_ref = att.get("attachmentDataRef") or {}
        drive_ref = att.get("driveDataRef") or {}
        out.append(Attachment(
            content_name=att.get("contentName") or "",
            content_type=att.get("contentType") or "",
            resource_name=data_ref.get("resourceName") or "",
            drive_file_id=drive_ref.get("driveFileId") or "",
        ))
    return tuple(out)
