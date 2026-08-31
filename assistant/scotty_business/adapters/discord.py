from __future__ import annotations

import urllib.parse
from collections.abc import Mapping

from .http import (
    AmbiguousEffectError,
    Attachment,
    ProviderError,
    RedactedMapping,
    Transport,
    fixed_id,
    require_success,
)

_BASE = "https://discord.com/api/v10"

#: Mentions are never parsed, so Scotty can neither ping a person nor a server.
_NO_MENTIONS: dict[str, object] = {"parse": []}

#: An ordinary channel read stays small and bounded.
MAX_READ_LIMIT = 50


def _content(value: object) -> str:
    if type(value) is not str or not value.strip() or len(value) > 2000:
        raise ProviderError("Discord message must contain 1-2000 characters")
    return value


def _thread_name(value: object) -> str:
    if type(value) is not str or not value.strip() or len(value) > 100:
        raise ProviderError("Discord thread name must contain 1-100 characters")
    if any(ord(char) < 32 for char in value):
        raise ProviderError("Discord thread name contains forbidden characters")
    return value


class DiscordAdapter:
    api_version = "v10"

    def __init__(self, transport: Transport, bot_token: str, channel_ids: tuple[str, ...]):
        if not bot_token:
            raise ProviderError("Discord credential is not configured")
        if not channel_ids:
            raise ProviderError("Discord destination allowlist is empty")
        self.transport = transport
        self.channel_ids = frozenset(fixed_id(item, "channel id") for item in channel_ids)
        self._headers = RedactedMapping(Authorization=f"Bot {bot_token}")
        self._identity: str | None = None

    def identity(self) -> str:
        """The authenticated bot's own user id, read once and cached."""

        if self._identity is None:
            body = require_success(
                self.transport.request("GET", f"{_BASE}/users/@me", headers=self._headers)
            )
            if not isinstance(body, dict):
                raise ProviderError("Discord identity response is malformed")
            self._identity = fixed_id(body.get("id"), "bot id")
        return self._identity

    def _destination(self, channel_id: object) -> str:
        channel = fixed_id(channel_id, "channel id")
        if channel not in self.channel_ids:
            raise ProviderError("Discord destination is not configured")
        return channel

    def _own_message(self, channel: str, message_id: str) -> Mapping[str, object]:
        """Read one message back and prove Scotty is its author."""

        body = self.get_message(channel, message_id)
        author = body.get("author")
        author_id = author.get("id") if isinstance(author, Mapping) else None
        if type(author_id) is not str or author_id != self.identity():
            raise ProviderError("Scotty may only change its own messages")
        return body

    def send_message(self, channel_id: str, content: str) -> dict[str, str]:
        channel = fixed_id(channel_id, "channel id")
        if channel not in self.channel_ids:
            raise ProviderError("Discord destination is not configured")
        if type(content) is not str or not content.strip() or len(content) > 2000:
            raise ProviderError("Discord message must contain 1-2000 characters")
        response = self.transport.request(
            "POST",
            f"{_BASE}/channels/{channel}/messages",
            headers=self._headers,
            json_body={"content": content, "allowed_mentions": _NO_MENTIONS},
        )
        body = require_success(response, expected=(200, 201))
        if not isinstance(body, dict):
            raise ProviderError("Discord acknowledgement is malformed")
        message_id = body.get("id")
        returned_channel = body.get("channel_id")
        if type(message_id) is not str or not message_id or returned_channel != channel:
            raise AmbiguousEffectError(
                "Discord acknowledgement is malformed; reconcile before retry"
            )
        return {"message_id": message_id, "channel_id": channel}

    def read_channel(self, channel_id: str, *, limit: int = 20) -> tuple[Mapping[str, object], ...]:
        """Read recent messages from one configured channel."""

        channel = self._destination(channel_id)
        if type(limit) is not int or not 1 <= limit <= MAX_READ_LIMIT:
            raise ProviderError(f"Discord read limit must be an integer from 1 to {MAX_READ_LIMIT}")
        body = require_success(
            self.transport.request(
                "GET",
                f"{_BASE}/channels/{channel}/messages",
                headers=self._headers,
                query={"limit": limit},
            )
        )
        if not isinstance(body, list):
            raise ProviderError("Discord channel history is malformed")
        messages: list[Mapping[str, object]] = []
        for item in body:
            if not isinstance(item, Mapping) or item.get("channel_id") not in (channel, None):
                raise ProviderError("Discord channel history is malformed")
            messages.append(dict(item))
        return tuple(messages)

    def reply_message(self, channel_id: str, message_id: str, content: str) -> dict[str, str]:
        """Reply to one exact message in a configured channel."""

        channel = self._destination(channel_id)
        message = fixed_id(message_id, "message id")
        body = require_success(
            self.transport.request(
                "POST",
                f"{_BASE}/channels/{channel}/messages",
                headers=self._headers,
                json_body={
                    "content": _content(content),
                    "allowed_mentions": _NO_MENTIONS,
                    "message_reference": {"message_id": message, "fail_if_not_exists": True},
                },
            ),
            expected=(200, 201),
        )
        return self._acknowledged(body, channel)

    def edit_own_message(self, channel_id: str, message_id: str, content: str) -> dict[str, str]:
        """Edit a message Scotty itself wrote, proven by readback first."""

        channel = self._destination(channel_id)
        message = fixed_id(message_id, "message id")
        text = _content(content)
        self._own_message(channel, message)
        body = require_success(
            self.transport.request(
                "PATCH",
                f"{_BASE}/channels/{channel}/messages/{message}",
                headers=self._headers,
                json_body={"content": text, "allowed_mentions": _NO_MENTIONS},
            )
        )
        if not isinstance(body, Mapping) or body.get("content") != text:
            raise AmbiguousEffectError("Discord edit readback mismatch; reconcile before retry")
        return {"message_id": message, "channel_id": channel}

    def delete_own_message(self, channel_id: str, message_id: str) -> bool:
        """Delete a message Scotty itself wrote. Never anyone else's."""

        channel = self._destination(channel_id)
        message = fixed_id(message_id, "message id")
        self._own_message(channel, message)
        return self.delete_message(channel, message)

    def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> bool:
        return self._reaction("PUT", channel_id, message_id, emoji)

    def remove_own_reaction(self, channel_id: str, message_id: str, emoji: str) -> bool:
        return self._reaction("DELETE", channel_id, message_id, emoji)

    def _reaction(self, method: str, channel_id: str, message_id: str, emoji: str) -> bool:
        channel = self._destination(channel_id)
        message = fixed_id(message_id, "message id")
        if type(emoji) is not str or not 1 <= len(emoji) <= 32 or "/" in emoji:
            raise ProviderError("Discord reaction is malformed")
        response = self.transport.request(
            method,
            f"{_BASE}/channels/{channel}/messages/{message}/reactions/"
            f"{urllib.parse.quote(emoji, safe='')}/@me",
            headers=self._headers,
        )
        return response.status == 204

    def attach_file(self, channel_id: str, content: str, attachment: Attachment) -> dict[str, str]:
        """Post one approved file with a short message."""

        channel = self._destination(channel_id)
        body = require_success(
            self.transport.request(
                "POST",
                f"{_BASE}/channels/{channel}/messages",
                headers=self._headers,
                json_body={"content": _content(content), "allowed_mentions": _NO_MENTIONS},
                attachment=attachment,
            ),
            expected=(200, 201),
        )
        return self._acknowledged(body, channel)

    def create_thread(self, channel_id: str, name: str, message_id: str | None = None) -> str:
        """Open a task thread on a configured channel, optionally from a message."""

        channel = self._destination(channel_id)
        title = _thread_name(name)
        if message_id is None:
            url = f"{_BASE}/channels/{channel}/threads"
            payload: dict[str, object] = {"name": title, "type": 11, "auto_archive_duration": 1440}
        else:
            message = fixed_id(message_id, "message id")
            url = f"{_BASE}/channels/{channel}/messages/{message}/threads"
            payload = {"name": title, "auto_archive_duration": 1440}
        body = require_success(
            self.transport.request("POST", url, headers=self._headers, json_body=payload),
            expected=(200, 201),
        )
        if not isinstance(body, Mapping):
            raise ProviderError("Discord thread acknowledgement is malformed")
        thread_id = body.get("id")
        if type(thread_id) is not str or not thread_id or body.get("parent_id") != channel:
            raise AmbiguousEffectError(
                "Discord thread acknowledgement is malformed; reconcile before retry"
            )
        return thread_id

    def _thread_parent(self, thread_id: str) -> tuple[str, Mapping[str, object]]:
        """A thread is reachable only through its configured parent channel."""

        thread = fixed_id(thread_id, "thread id")
        body = require_success(
            self.transport.request("GET", f"{_BASE}/channels/{thread}", headers=self._headers)
        )
        if not isinstance(body, Mapping) or body.get("id") != thread:
            raise ProviderError("Discord thread readback identity mismatch")
        parent = body.get("parent_id")
        if type(parent) is not str:
            raise ProviderError("Discord thread has no configured parent")
        self._destination(parent)
        return thread, body

    def send_thread_message(self, thread_id: str, content: str) -> dict[str, str]:
        thread, _ = self._thread_parent(thread_id)
        body = require_success(
            self.transport.request(
                "POST",
                f"{_BASE}/channels/{thread}/messages",
                headers=self._headers,
                json_body={"content": _content(content), "allowed_mentions": _NO_MENTIONS},
            ),
            expected=(200, 201),
        )
        return self._acknowledged(body, thread)

    def archive_own_thread(self, thread_id: str) -> bool:
        """Archive a task thread Scotty opened. Never one it does not own."""

        thread, body = self._thread_parent(thread_id)
        if body.get("owner_id") != self.identity():
            raise ProviderError("Scotty may only archive its own threads")
        response = self.transport.request(
            "PATCH",
            f"{_BASE}/channels/{thread}",
            headers=self._headers,
            json_body={"archived": True},
        )
        readback = require_success(response)
        return isinstance(readback, Mapping) and readback.get("archived") is True

    def _acknowledged(self, body: object, channel: str) -> dict[str, str]:
        if not isinstance(body, Mapping):
            raise ProviderError("Discord acknowledgement is malformed")
        message_id = body.get("id")
        if type(message_id) is not str or not message_id or body.get("channel_id") != channel:
            raise AmbiguousEffectError(
                "Discord acknowledgement is malformed; reconcile before retry"
            )
        return {"message_id": message_id, "channel_id": channel}

    def delete_message(self, channel_id: str, message_id: str) -> bool:
        """Delete one exact message and confirm the platform really removed it.

        Deletion is confirmed by reading the message back and requiring the
        platform to report it absent. Any other outcome returns False so a
        caller that depends on confirmed deletion fails closed.
        """

        channel = fixed_id(channel_id, "channel id")
        message = fixed_id(message_id, "message id")
        if channel not in self.channel_ids:
            raise ProviderError("Discord destination is not configured")
        deleted = self.transport.request(
            "DELETE", f"{_BASE}/channels/{channel}/messages/{message}", headers=self._headers
        )
        # 404 means the platform never had that message under this id, which is
        # not evidence that the operator's message was removed.
        if deleted.status != 204:
            return False
        readback = self.transport.request(
            "GET", f"{_BASE}/channels/{channel}/messages/{message}", headers=self._headers
        )
        return readback.status == 404

    def get_message(self, channel_id: str, message_id: str) -> dict[str, object]:
        channel = fixed_id(channel_id, "channel id")
        message = fixed_id(message_id, "message id")
        if channel not in self.channel_ids:
            raise ProviderError("Discord destination is not configured")
        response = self.transport.request(
            "GET", f"{_BASE}/channels/{channel}/messages/{message}", headers=self._headers
        )
        body = require_success(response)
        if (
            not isinstance(body, dict)
            or body.get("id") != message
            or body.get("channel_id") != channel
        ):
            raise ProviderError("Discord readback identity mismatch")
        return dict(body)
