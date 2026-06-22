"""Outbound Google Chat REST client.

The chat *webhook* path returns at most one message to the user
synchronously, but our agent runs frequently exceed Chat's ~6s "not
responding" UX threshold and its ~30s hard timeout. To stay inside both,
the dispatcher returns an ack immediately and runs the agent in a
background task; the agent's final reply is posted back into the same
space via this REST client.

Calling ``chat.spaces.messages.create`` with ``chat.bot`` scope posts as the
app — no extra credentials, no per-user OAuth.

Only one helper is exposed: :func:`post_message_to_space`. It runs the
authorized HTTP call in a worker thread because ``AuthorizedSession``
is sync; that's fine for a background task whose latency is not in the
hot path.
"""

from __future__ import annotations

import asyncio
import logging
from threading import Lock
from typing import Final

import google.auth
from google.auth.transport.requests import AuthorizedSession

log = logging.getLogger(__name__)

_CHAT_BOT_SCOPE: Final = "https://www.googleapis.com/auth/chat.bot"
_CHAT_API_BASE: Final = "https://chat.googleapis.com/v1"
_CHAT_MEDIA_BASE: Final = "https://chat.googleapis.com/v1/media"
_REQUEST_TIMEOUT_SECONDS: Final = 30
# Attachment downloads can be larger than a chat message; give them more room.
_DOWNLOAD_TIMEOUT_SECONDS: Final = 60

_session_lock: Final = Lock()
_session: AuthorizedSession | None = None


def _get_session() -> AuthorizedSession:
    """Return a process-wide ``AuthorizedSession`` minting tokens with chat.bot.

    Built lazily so importing this module doesn't try to resolve ADC at
    process start (which would break local dev when running tests or
    tools that don't talk to Chat).
    """
    global _session
    with _session_lock:
        if _session is None:
            credentials, _project = google.auth.default(scopes=[_CHAT_BOT_SCOPE])
            _session = AuthorizedSession(credentials)
        return _session


async def post_message_to_space(
    space_name: str,
    text: str,
    *,
    thread_name: str | None = None,
    thread_key: str | None = None,
) -> str | None:
    """Post *text* into *space_name* as the Chat app.

    *space_name* is the ``space.name`` field from the inbound event
    (``spaces/AAAA...``). Threading is resolved in priority order:

    * If *thread_name* is provided, post into that existing thread
      (falling back to a new thread if the named one is gone).
    * Otherwise, if *thread_key* is provided, Chat creates a thread
      tagged with that client-supplied key on the first post and routes
      subsequent posts with the same key into the same thread.
    * Otherwise, the post lands in a brand-new top-level thread Chat
      creates for it. The caller uses the returned ``thread.name`` to
      route subsequent posts (and to key the ADK session) into that
      same thread.

    Returns the ``thread.name`` Chat persisted the message to, or
    ``None`` on failure. Failures are logged and swallowed — this is
    called from a fire-and-forget background task and there is no
    synchronous response to attach an error to.
    """
    if not space_name:
        log.warning("post_message_to_space called without space_name; dropping")
        return None
    url = f"{_CHAT_API_BASE}/{space_name}/messages"
    body: dict[str, object] = {"text": text}
    params: dict[str, str] = {}
    if thread_name:
        body["thread"] = {"name": thread_name}
        # Without this, posting to an unknown thread is an error; we'd
        # rather start a new thread than drop the reply.
        params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    elif thread_key:
        body["thread"] = {"threadKey": thread_key}
        params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    log.info(
        "Chat REST POST space=%s thread_name=%r thread_key=%r reply_opt=%r",
        space_name,
        thread_name,
        thread_key,
        params.get("messageReplyOption"),
    )
    try:
        return await asyncio.to_thread(_post_sync, url, body, params)
    except Exception:
        log.exception("Failed to post async Chat message to %s", space_name)
        return None


async def post_card_to_space(
    space_name: str,
    card_body: dict,
    *,
    thread_name: str | None = None,
) -> str | None:
    """Post a Cards v2 message into *space_name* as the Chat app.

    *card_body* must be a full message body dict containing a ``cardsV2``
    key (no ``text`` key). Returns the ``message.name`` Chat assigned to
    the posted message so the caller can update the card later, or ``None``
    on failure.
    """
    if not space_name:
        log.warning("post_card_to_space called without space_name; dropping")
        return None
    url = f"{_CHAT_API_BASE}/{space_name}/messages"
    body: dict[str, object] = dict(card_body)
    params: dict[str, str] = {}
    if thread_name:
        body["thread"] = {"name": thread_name}
        params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    log.info(
        "Chat REST card POST space=%s thread_name=%r",
        space_name,
        thread_name,
    )
    try:
        return await asyncio.to_thread(_post_card_sync, url, body, params)
    except Exception:
        log.exception("Failed to post card to %s", space_name)
        return None


async def update_card_in_space(
    message_name: str,
    card_body: dict,
) -> None:
    """Replace a card message in place (PUT with updateMask=cardsV2).

    *message_name* is the ``message.name`` field returned when the card
    was first posted (``spaces/.../messages/...``).
    """
    if not message_name:
        log.warning("update_card_in_space called without message_name; dropping")
        return
    url = f"{_CHAT_API_BASE}/{message_name}"
    params = {"updateMask": "cardsV2"}
    log.info("Chat REST card PUT message=%s", message_name)
    try:
        await asyncio.to_thread(_put_sync, url, card_body, params)
    except Exception:
        log.exception("Failed to update card %s", message_name)


async def download_attachment(resource_name: str) -> bytes | None:
    """Download a Chat attachment's bytes by its ``attachmentDataRef.resourceName``.

    Uploaded-content attachments expose a ``resourceName`` under
    ``message.attachment[].attachmentDataRef``; the bytes are fetched from the
    Chat media endpoint with the same ``chat.bot`` credentials used to post
    messages. Returns ``None`` on failure (logged) since this runs inside a
    background task.
    """
    if not resource_name:
        log.warning("download_attachment called without resource_name; dropping")
        return None
    url = f"{_CHAT_MEDIA_BASE}/{resource_name}?alt=media"
    try:
        return await asyncio.to_thread(_download_sync, url)
    except Exception:
        log.exception("Failed to download Chat attachment %s", resource_name)
        return None


def _download_sync(url: str) -> bytes:
    """Blocking GET for attachment media. Run in a worker thread."""
    response = _get_session().get(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS)
    if not response.ok:
        log.error(
            "Chat media download failed: status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        response.raise_for_status()
    return response.content


def _post_card_sync(
    url: str,
    body: dict[str, object],
    params: dict[str, str],
) -> str | None:
    """Blocking POST for a card message. Returns the message.name."""
    response = _get_session().post(
        url,
        json=body,
        params=params,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    if not response.ok:
        log.error(
            "Chat REST card post failed: status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        response.raise_for_status()
    try:
        data = response.json()
    except Exception:
        return None
    msg_name = data.get("name")
    log.info("Chat REST card post OK: message_name=%r", msg_name)
    return msg_name


def _put_sync(
    url: str,
    body: dict[str, object],
    params: dict[str, str],
) -> None:
    """Blocking PUT to update a card message in place."""
    response = _get_session().put(
        url,
        json=body,
        params=params,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    if not response.ok:
        log.error(
            "Chat REST card PUT failed: status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        response.raise_for_status()
    log.info("Chat REST card PUT OK: url=%s", url)


def _post_sync(
    url: str,
    body: dict[str, object],
    params: dict[str, str],
) -> str | None:
    """Blocking POST to the Chat REST API. Run in a worker thread.

    Returns the ``thread.name`` Chat persisted the message to, so the
    caller can anchor follow-up posts (and the ADK session) on that
    thread when the request didn't specify one upfront.
    """
    response = _get_session().post(
        url,
        json=body,
        params=params,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    if not response.ok:
        log.error(
            "Chat REST post failed: status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        response.raise_for_status()
    try:
        data = response.json()
    except Exception:
        log.info("Chat REST post OK (no JSON body)")
        return None
    returned_thread = (data.get("thread") or {}).get("name")
    log.info(
        "Chat REST post OK: returned_thread=%r returned_message=%r",
        returned_thread,
        data.get("name"),
    )
    return returned_thread
