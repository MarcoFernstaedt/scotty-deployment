"""Idempotent creation or reuse of the two private client Discord channels.

This runs only from the local, root-only setup command while the container is
stopped. The bot token is read from hidden local input or the process
environment; it never reaches argv, stdout, or a log line.

Every mutation is preceded by an exact identity, guild, and permission check and
by an explicit local confirmation. An outcome that may have reached Discord but
cannot be read back is recorded as unknown and is never resolved by creating a
second channel.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .adapters.http import (
    AmbiguousEffectError,
    HttpTransport,
    ProviderError,
    RedactedMapping,
    require_success,
)

_BASE = "https://discord.com/api/v10"
_CHANNEL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,98}[a-z0-9]")
_ROLE_OVERWRITE = 0
_MEMBER_OVERWRITE = 1

ADMINISTRATOR = 1 << 3
MANAGE_CHANNELS = 1 << 4
ADD_REACTIONS = 1 << 6
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
EMBED_LINKS = 1 << 14
ATTACH_FILES = 1 << 15
READ_MESSAGE_HISTORY = 1 << 16

#: The minimum in-channel permissions a human principal needs to hold a chat.
MEMBER_ALLOW = (
    VIEW_CHANNEL | SEND_MESSAGES | READ_MESSAGE_HISTORY | EMBED_LINKS | ATTACH_FILES | ADD_REACTIONS
)
#: The minimum in-channel permissions the assistant needs to reply.
BOT_ALLOW = VIEW_CHANNEL | SEND_MESSAGES | READ_MESSAGE_HISTORY | EMBED_LINKS

UNKNOWN_MARKER = "unknown"


class ProvisioningError(RuntimeError):
    """Channel provisioning stopped before or during a bounded Discord mutation."""


class ProvisionStatus(StrEnum):
    CREATED = "created"
    REUSED = "reused"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ChannelPlan:
    key: str
    name: str
    guild_id: str
    user_id: str


@dataclass(frozen=True, slots=True)
class ProvisionedChannel:
    key: str
    channel_id: str | None
    status: ProvisionStatus


@dataclass(frozen=True, slots=True)
class ProvisionOutcome:
    channels: Mapping[str, ProvisionedChannel]
    error: str | None

    def is_complete(self, plans: Sequence[ChannelPlan]) -> bool:
        return self.error is None and all(
            self.channels.get(plan.key) is not None
            and self.channels[plan.key].status is not ProvisionStatus.UNKNOWN
            for plan in plans
        )


class DiscordProvisioningClient(Protocol):
    def get(self, path: str) -> object: ...

    def post(self, path: str, json_body: Mapping[str, object]) -> object: ...


class DiscordProvisioningApi:
    """Bounded Discord REST client for local provisioning only."""

    def __init__(self, token: str):
        if not token:
            raise ProvisioningError("Discord credential is not configured")
        self.transport = HttpTransport(timeout=20.0, max_response_bytes=262_144)
        # The token lives only inside a mapping that refuses to render itself.
        self._headers = RedactedMapping(Authorization=f"Bot {token}")

    def _path(self, path: str) -> str:
        if not path.startswith("/") or ".." in path or "?" in path or "#" in path:
            raise ProvisioningError("Discord provisioning path is invalid")
        return f"{_BASE}{path}"

    def get(self, path: str) -> object:
        response = self.transport.request("GET", self._path(path), headers=self._headers)
        return require_success(response)

    def post(self, path: str, json_body: Mapping[str, object]) -> object:
        response = self.transport.request(
            "POST", self._path(path), headers=self._headers, json_body=json_body
        )
        return require_success(response, expected=(200, 201))


def _snowflake(value: object, field: str) -> str:
    if type(value) is not str or not value.isdigit() or not 1 <= len(value) <= 20:
        raise ProvisioningError(f"{field} must be a Discord numeric ID")
    return value


def intended_overwrites(guild_id: str, user_id: str, bot_id: str) -> tuple[dict[str, object], ...]:
    """The exact permission overwrites a provisioned private channel must carry."""

    return (
        {
            "id": _snowflake(guild_id, "guild_id"),
            "type": _ROLE_OVERWRITE,
            "allow": "0",
            "deny": str(VIEW_CHANNEL),
        },
        {
            "id": _snowflake(user_id, "user_id"),
            "type": _MEMBER_OVERWRITE,
            "allow": str(MEMBER_ALLOW),
            "deny": "0",
        },
        {
            "id": _snowflake(bot_id, "bot_id"),
            "type": _MEMBER_OVERWRITE,
            "allow": str(BOT_ALLOW),
            "deny": "0",
        },
    )


def preview_text(plan: ChannelPlan, bot_id: str) -> str:
    """Local, non-secret confirmation preview shown on the operator's console."""

    return "\n".join(
        (
            "Scotty will create one private Discord text channel:",
            f"  guild:      {plan.guild_id}",
            f"  name:       {plan.name}",
            f"  member:     {plan.user_id} (view, send, history, links, files, reactions)",
            f"  assistant:  {bot_id} (view, send, history, links)",
            "  @everyone:  View Channel denied",
            "No other member, role, or channel is changed. Nothing is sent to the channel.",
        )
    )


def _normalized(overwrite: object) -> tuple[str, int, int, int] | None:
    if not isinstance(overwrite, Mapping):
        return None
    identifier = overwrite.get("id")
    kind = overwrite.get("type")
    allow = overwrite.get("allow")
    deny = overwrite.get("deny")
    if type(identifier) is not str or type(kind) is not int:
        return None
    if type(allow) is not str or type(deny) is not str:
        return None
    if not allow.isdigit() or not deny.isdigit():
        return None
    return (identifier, kind, int(allow), int(deny))


def _overwrites_match(
    channel: Mapping[str, object], expected: Sequence[Mapping[str, object]]
) -> bool:
    observed = channel.get("permission_overwrites")
    if not isinstance(observed, list) or len(observed) != len(expected):
        return False
    normalized = [_normalized(item) for item in observed]
    if any(item is None for item in normalized):
        return False
    wanted = [_normalized(item) for item in expected]
    return sorted(item for item in normalized if item) == sorted(item for item in wanted if item)


def _channel_matches(
    channel: object, plan: ChannelPlan, bot_id: str, *, expect_name: bool = True
) -> bool:
    if not isinstance(channel, Mapping):
        return False
    if channel.get("guild_id") != plan.guild_id or channel.get("type") != 0:
        return False
    if expect_name and channel.get("name") != plan.name:
        return False
    return _overwrites_match(channel, intended_overwrites(plan.guild_id, plan.user_id, bot_id))


def _verify_bot(client: DiscordProvisioningClient, guild_id: str) -> str:
    identity = client.get("/users/@me")
    if not isinstance(identity, Mapping) or type(identity.get("id")) is not str:
        raise ProvisioningError("Discord bot identity is malformed")
    bot_id = _snowflake(identity["id"], "bot id")
    guild = client.get(f"/guilds/{guild_id}")
    if not isinstance(guild, Mapping) or guild.get("id") != guild_id:
        raise ProvisioningError("configured Discord guild identity mismatch")
    member = client.get(f"/guilds/{guild_id}/members/@me")
    if (
        not isinstance(member, Mapping)
        or not isinstance(member.get("user"), Mapping)
        or member["user"].get("id") != bot_id
    ):
        raise ProvisioningError("Discord bot is not a verified member of the configured guild")
    _require_manage_channels(client, guild_id, member)
    return bot_id


def _require_manage_channels(
    client: DiscordProvisioningClient, guild_id: str, member: Mapping[str, object]
) -> None:
    roles = client.get(f"/guilds/{guild_id}/roles")
    if not isinstance(roles, list):
        raise ProvisioningError("Discord guild roles are unavailable")
    held = {guild_id}
    member_roles = member.get("roles")
    if isinstance(member_roles, list):
        held.update(item for item in member_roles if type(item) is str)
    permissions = 0
    for role in roles:
        if not isinstance(role, Mapping) or role.get("id") not in held:
            continue
        raw = role.get("permissions")
        if type(raw) is not str or not raw.isdigit():
            raise ProvisioningError("Discord role permissions are malformed")
        permissions |= int(raw)
    if not permissions & MANAGE_CHANNELS and not permissions & ADMINISTRATOR:
        raise ProvisioningError(
            "the Discord application needs Manage Channels in the configured guild"
        )


def _find_existing(
    client: DiscordProvisioningClient, plan: ChannelPlan, bot_id: str
) -> Mapping[str, object] | None:
    listing = client.get(f"/guilds/{plan.guild_id}/channels")
    if not isinstance(listing, list):
        raise ProvisioningError("Discord channel listing is malformed")
    candidates = [
        item
        for item in listing
        if isinstance(item, Mapping) and item.get("name") == plan.name and item.get("type") == 0
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise ProvisioningError(
            f"more than one Discord channel is named {plan.name}; resolve it manually"
        )
    candidate = candidates[0]
    if not _channel_matches(candidate, plan, bot_id):
        raise ProvisioningError(
            f"an existing channel named {plan.name} does not match the intended private scope"
        )
    return candidate


def _verify_recorded(
    client: DiscordProvisioningClient, plan: ChannelPlan, bot_id: str, channel_id: str
) -> ProvisionedChannel:
    channel = client.get(f"/channels/{_snowflake(channel_id, 'channel_id')}")
    if not isinstance(channel, Mapping) or channel.get("id") != channel_id:
        raise ProvisioningError("recorded Discord channel identity mismatch")
    if not _channel_matches(channel, plan, bot_id):
        raise ProvisioningError("recorded Discord channel privacy or membership drifted")
    return ProvisionedChannel(key=plan.key, channel_id=channel_id, status=ProvisionStatus.REUSED)


def _create(
    client: DiscordProvisioningClient,
    plan: ChannelPlan,
    bot_id: str,
    confirm: Callable[[str], bool],
) -> ProvisionedChannel:
    if not _CHANNEL_NAME.fullmatch(plan.name):
        raise ProvisioningError("private channel name is not a valid Discord text channel name")
    if not confirm(preview_text(plan, bot_id)):
        raise ProvisioningError("local confirmation was declined; nothing was created")
    body = {
        "name": plan.name,
        "type": 0,
        "permission_overwrites": [
            dict(item) for item in intended_overwrites(plan.guild_id, plan.user_id, bot_id)
        ],
    }
    try:
        created = client.post(f"/guilds/{plan.guild_id}/channels", body)
    except AmbiguousEffectError:
        return ProvisionedChannel(key=plan.key, channel_id=None, status=ProvisionStatus.UNKNOWN)
    if not isinstance(created, Mapping) or type(created.get("id")) is not str:
        # The create may have reached Discord. Never create a second channel.
        return ProvisionedChannel(key=plan.key, channel_id=None, status=ProvisionStatus.UNKNOWN)
    channel_id = _snowflake(created["id"], "created channel id")
    try:
        readback = client.get(f"/channels/{channel_id}")
    except (ProviderError, ProvisioningError):
        return ProvisionedChannel(
            key=plan.key, channel_id=channel_id, status=ProvisionStatus.UNKNOWN
        )
    if not isinstance(readback, Mapping) or readback.get("id") != channel_id:
        return ProvisionedChannel(
            key=plan.key, channel_id=channel_id, status=ProvisionStatus.UNKNOWN
        )
    if not _channel_matches(readback, plan, bot_id):
        raise ProvisioningError("created Discord channel privacy or membership differs")
    return ProvisionedChannel(key=plan.key, channel_id=channel_id, status=ProvisionStatus.CREATED)


def ensure_private_channels(
    plans: Sequence[ChannelPlan],
    client: DiscordProvisioningClient,
    *,
    confirm: Callable[[str], bool],
    recorded: Mapping[str, str] | None = None,
) -> ProvisionOutcome:
    """Create or reuse exactly one private channel per plan, idempotently."""

    known = dict(recorded or {})
    channels: dict[str, ProvisionedChannel] = {}
    guilds = {plan.guild_id for plan in plans}
    if len(guilds) != 1:
        return ProvisionOutcome({}, "every provisioned channel must share one configured guild")
    if len({plan.name for plan in plans}) != len(plans):
        return ProvisionOutcome({}, "provisioned channel names must be distinct")
    guild_id = _snowflake(next(iter(guilds)), "guild_id")
    try:
        bot_id = _verify_bot(client, guild_id)
    except (ProviderError, ProvisioningError) as exc:
        return ProvisionOutcome({}, str(exc))

    for plan in plans:
        marker = known.get(plan.key)
        try:
            if marker == UNKNOWN_MARKER:
                channels[plan.key] = ProvisionedChannel(
                    key=plan.key, channel_id=None, status=ProvisionStatus.UNKNOWN
                )
                return ProvisionOutcome(
                    channels,
                    f"a previous run left {plan.key} unknown; reconcile it in Discord "
                    "before running setup again",
                )
            if marker:
                channels[plan.key] = _verify_recorded(client, plan, bot_id, marker)
                continue
            existing = _find_existing(client, plan, bot_id)
            if existing is not None:
                channels[plan.key] = ProvisionedChannel(
                    key=plan.key,
                    channel_id=_snowflake(existing.get("id"), "channel id"),
                    status=ProvisionStatus.REUSED,
                )
                continue
            result = _create(client, plan, bot_id, confirm)
        except (ProviderError, ProvisioningError) as exc:
            return ProvisionOutcome(channels, str(exc))
        channels[plan.key] = result
        if result.status is ProvisionStatus.UNKNOWN:
            return ProvisionOutcome(
                channels,
                f"the {plan.key} channel outcome is unknown; reconcile it in Discord "
                "before running setup again",
            )
    return ProvisionOutcome(channels, None)
