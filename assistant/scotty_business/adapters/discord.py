from __future__ import annotations

from .http import ProviderError, RedactedMapping, Transport, fixed_id, require_success

_BASE = "https://discord.com/api/v10"


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
            json_body={"content": content, "allowed_mentions": {"parse": []}},
        )
        body = require_success(response, expected=(200, 201))
        if not isinstance(body, dict):
            raise ProviderError("Discord acknowledgement is malformed")
        message_id = body.get("id")
        returned_channel = body.get("channel_id")
        if type(message_id) is not str or not message_id or returned_channel != channel:
            raise ProviderError("Discord acknowledgement identity mismatch")
        return {"message_id": message_id, "channel_id": channel}

    def get_message(self, channel_id: str, message_id: str) -> dict[str, object]:
        channel = fixed_id(channel_id, "channel id")
        message = fixed_id(message_id, "message id")
        if channel not in self.channel_ids:
            raise ProviderError("Discord destination is not configured")
        response = self.transport.request(
            "GET", f"{_BASE}/channels/{channel}/messages/{message}", headers=self._headers
        )
        body = require_success(response)
        if not isinstance(body, dict) or body.get("id") != message or body.get("channel_id") != channel:
            raise ProviderError("Discord readback identity mismatch")
        return dict(body)
