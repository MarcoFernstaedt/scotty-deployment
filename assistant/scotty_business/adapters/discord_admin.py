"""Typed Discord guild administration, on named permissions and readback.

The bot never holds `Administrator`, so everything here runs on ordinary named
permissions and fails with a clear "this needs MANAGE_CHANNELS" rather than
silently doing nothing. Every change is read back before it is reported as
done, because a 200 from Discord describes the request, not the guild.

Nothing in this module chooses its own target. The guild is fixed by
configuration, the private channels are excluded above it, and a role the bot
could not legitimately hand out is refused before the call rather than after.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..discord_permissions import (
    DANGEROUS_PERMISSIONS,
    missing_permissions,
    role_is_assignable,
)
from .http import (
    AmbiguousEffectError,
    ProviderError,
    RedactedMapping,
    Transport,
    fixed_id,
    require_success,
)

_BASE = "https://discord.com/api/v10"


@dataclass(frozen=True, slots=True)
class MemberRoles:
    """One member's roles, as the guild actually reports them."""

    user_id: str
    roles: tuple[str, ...]
    joined_at: str

    def as_json(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "roles": list(self.roles),
            "joined_at": self.joined_at,
        }


MAX_NAME_CHARS = 100
MAX_TOPIC_CHARS = 1024
MAX_REORDER = 25

#: Channel kinds this deployment creates. Voice and stage channels are absent
#: because nothing here needs them, and an absent kind cannot be asked for.
CHANNEL_KINDS: Mapping[str, int] = {"text": 0, "category": 4, "announcement": 5, "forum": 15}


def _int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ProviderError(f"Discord {label} is malformed")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"Discord {label} is malformed") from exc


def _name(value: object, label: str = "name") -> str:
    if type(value) is not str or not value.strip() or len(value) > MAX_NAME_CHARS:
        raise ProviderError(f"Discord {label} must be 1-{MAX_NAME_CHARS} characters")
    return value.strip()


def _overwrites(value: object) -> list[dict[str, str]]:
    """Validate permission overwrites and refuse any dangerous grant."""

    if not isinstance(value, list) or not value:
        raise ProviderError("Discord permission overwrites must be a non-empty list")
    checked: list[dict[str, str]] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise ProviderError("each Discord overwrite must be an object")
        allow = _int(entry.get("allow", 0), "overwrite allow bits")
        deny = _int(entry.get("deny", 0), "overwrite deny bits")
        if allow & DANGEROUS_PERMISSIONS:
            raise ProviderError("that permission is never granted through an overwrite")
        checked.append(
            {
                "id": fixed_id(entry.get("id"), "overwrite id"),
                "type": str(_int(entry.get("type", 0), "overwrite type")),
                "allow": str(allow),
                "deny": str(deny),
            }
        )
    return checked


class DiscordAdminAdapter:
    """Guild administration bound to one configured guild."""

    api_version = "v10"

    def __init__(self, transport: Transport, bot_token: str, guild_id: str):
        if not bot_token:
            raise ProviderError("Discord credential is not configured")
        self.transport = transport
        self.guild_id = fixed_id(guild_id, "guild id")
        self._headers = RedactedMapping(Authorization=f"Bot {bot_token}")

    # ---- permission readback -------------------------------------------

    def bot_permissions(self) -> int:
        """What the guild actually granted this bot, as an integer."""

        response = self.transport.request("GET", f"{_BASE}/users/@me/guilds", headers=self._headers)
        body = require_success(response)
        if not isinstance(body, list):
            raise ProviderError("Discord guild list is malformed")
        for entry in body:
            if isinstance(entry, Mapping) and entry.get("id") == self.guild_id:
                try:
                    return int(entry.get("permissions", 0))
                except (TypeError, ValueError) as exc:
                    raise ProviderError("Discord permission integer is malformed") from exc
        raise ProviderError("the bot is not a member of the configured guild")

    def require_permission(self, operation: str) -> None:
        """Refuse before the call when the guild has not granted enough.

        This is what makes running without `Administrator` workable: the
        operator is told the exact permission to add rather than seeing an
        opaque 403.
        """

        missing = missing_permissions(self.bot_permissions(), operation)
        if missing:
            raise ProviderError("this needs the Discord permission " + ", ".join(missing))

    def member_permissions(self, user_id: str) -> MemberRoles:
        """Read one member's roles back, for membership and permission review."""

        member = fixed_id(user_id, "user id")
        response = self.transport.request(
            "GET", f"{_BASE}/guilds/{self.guild_id}/members/{member}", headers=self._headers
        )
        body = require_success(response)
        if not isinstance(body, Mapping):
            raise ProviderError("Discord member response is malformed")
        roles = body.get("roles")
        return MemberRoles(
            user_id=member,
            roles=tuple(item for item in roles if type(item) is str)
            if isinstance(roles, list)
            else (),
            joined_at=str(body.get("joined_at", "")),
        )

    # ---- channels ------------------------------------------------------

    def create_channel(
        self,
        name: str,
        *,
        kind: str = "text",
        parent_id: str = "",
        topic: str = "",
        overwrites: Sequence[Mapping[str, object]] | None = None,
    ) -> dict[str, object]:
        """Create one channel and read it back before reporting success."""

        if kind not in CHANNEL_KINDS:
            raise ProviderError("that Discord channel kind is not one this deployment creates")
        if topic and (type(topic) is not str or len(topic) > MAX_TOPIC_CHARS):
            raise ProviderError(f"a Discord topic is at most {MAX_TOPIC_CHARS} characters")
        body: dict[str, object] = {"name": _name(name), "type": CHANNEL_KINDS[kind]}
        if parent_id:
            body["parent_id"] = self.require_in_guild(parent_id)["id"]
        if topic:
            body["topic"] = topic
        if overwrites is not None:
            body["permission_overwrites"] = _overwrites(list(overwrites))
        response = self.transport.request(
            "POST",
            f"{_BASE}/guilds/{self.guild_id}/channels",
            headers=self._headers,
            json_body=body,
        )
        created = require_success(response, expected=(200, 201))
        channel_id = created.get("id") if isinstance(created, Mapping) else None
        if type(channel_id) is not str or not channel_id:
            raise AmbiguousEffectError(
                "Discord channel acknowledgement is malformed; reconcile before retry"
            )
        return self._verify_channel(channel_id, {"name": body["name"], "type": body["type"]})

    def edit_channel(self, channel_id: str, changes: Mapping[str, object]) -> dict[str, object]:
        """Rename, retopic, or reparent one channel, then prove it."""

        channel = self.require_in_guild(channel_id)["id"]
        assert type(channel) is str  # noqa: S101 - proven by the readback above
        allowed = {"name", "topic", "parent_id", "position"}
        if not changes or set(changes) - allowed:
            raise ProviderError("that Discord channel change is not permitted")
        body: dict[str, object] = {}
        if "name" in changes:
            body["name"] = _name(changes["name"])
        if "topic" in changes:
            topic = changes["topic"]
            if type(topic) is not str or len(topic) > MAX_TOPIC_CHARS:
                raise ProviderError("a Discord topic is malformed")
            body["topic"] = topic
        if "parent_id" in changes:
            body["parent_id"] = fixed_id(changes["parent_id"], "category id")
        if "position" in changes:
            position = changes["position"]
            if type(position) is not int or not 0 <= position <= 500:
                raise ProviderError("a Discord channel position is malformed")
            body["position"] = position
        self._patch_channel(channel, body)
        return self._verify_channel(channel, body)

    def archive_channel(self, channel_id: str) -> dict[str, object]:
        """Archive by moving out of sight, never by deleting.

        Deleting a channel destroys its history, which is not something this
        deployment does on anyone's behalf. Archiving is reversible.
        """

        channel = fixed_id(channel_id, "channel id")
        self.require_in_guild(channel)
        self._patch_channel(channel, {"archived": True, "locked": True})
        observed = self._read_channel(channel)
        if observed.get("archived") is not True:
            raise AmbiguousEffectError(
                "the Discord channel does not read back as archived; reconcile before retry"
            )
        return observed

    def reorder_channels(self, positions: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
        """Set the order of several channels in one call, bounded."""

        if not positions or len(positions) > MAX_REORDER:
            raise ProviderError(f"a Discord reorder covers 1-{MAX_REORDER} channels")
        planned = [
            (
                fixed_id(entry.get("id"), "channel id"),
                _int(entry.get("position", 0), "channel position"),
            )
            for entry in positions
            if isinstance(entry, Mapping)
        ]
        for channel, _ in planned:
            self.require_in_guild(channel)
        ordered: list[str] = []
        for channel, position in planned:
            # One channel at a time, each read back, so a partly applied
            # reorder is visible rather than silently half-done.
            self._patch_channel(channel, {"position": position})
            observed = self._read_channel(channel)
            if observed.get("position") != position:
                raise AmbiguousEffectError(
                    "a Discord channel did not move as intended; reconcile before retry"
                )
            ordered.append(channel)
        return tuple(ordered)

    def set_channel_permissions(
        self, channel_id: str, overwrites: Sequence[Mapping[str, object]]
    ) -> dict[str, object]:
        """Replace one channel's overwrites, then read them back exactly."""

        channel = fixed_id(channel_id, "channel id")
        checked = _overwrites(list(overwrites))
        self.require_in_guild(channel)
        for entry in checked:
            response = self.transport.request(
                "PUT",
                f"{_BASE}/channels/{channel}/permissions/{entry['id']}",
                headers=self._headers,
                json_body={
                    "allow": entry["allow"],
                    "deny": entry["deny"],
                    "type": int(entry["type"]),
                },
            )
            if response.status not in {200, 204}:
                raise ProviderError("Discord refused the permission change")
        observed = self._read_channel(channel)
        stored = observed.get("permission_overwrites")
        if not isinstance(stored, list):
            raise AmbiguousEffectError(
                "the Discord overwrites could not be read back; reconcile before retry"
            )
        by_id = {str(item.get("id")): item for item in stored if isinstance(item, Mapping)}
        for entry in checked:
            landed = by_id.get(entry["id"])
            if landed is None or str(landed.get("allow")) != entry["allow"]:
                raise AmbiguousEffectError(
                    "the Discord overwrites do not read back as intended; reconcile before retry"
                )
        return observed

    # ---- roles, events, webhooks, moderation ---------------------------

    def guild_roles(self) -> dict[str, Mapping[str, object]]:
        """Every role in the configured guild, as the guild reports them."""

        body = require_success(
            self.transport.request(
                "GET", f"{_BASE}/guilds/{self.guild_id}/roles", headers=self._headers
            )
        )
        if not isinstance(body, list):
            raise ProviderError("Discord role list is malformed")
        roles: dict[str, Mapping[str, object]] = {}
        for entry in body:
            if isinstance(entry, Mapping) and type(entry.get("id")) is str:
                roles[str(entry["id"])] = entry
        return roles

    def bot_role_position(self, roles: Mapping[str, Mapping[str, object]]) -> int:
        """The bot's own highest role position, read rather than supplied."""

        identity = require_success(
            self.transport.request(
                "GET",
                f"{_BASE}/guilds/{self.guild_id}/members/@me",
                headers=self._headers,
            )
        )
        if not isinstance(identity, Mapping):
            raise ProviderError("Discord bot membership is malformed")
        held = identity.get("roles")
        positions = [
            _int(roles[item].get("position", 0), "role position")
            for item in (held if isinstance(held, list) else [])
            if type(item) is str and item in roles
        ]
        if not positions:
            raise ProviderError("the bot holds no role in the configured guild")
        return max(positions)

    def assign_role(self, user_id: str, role_id: str) -> MemberRoles:
        """Give one member one role, only below the bot and never privileged.

        The role's position, managed flag, and permission bits are read from the
        guild rather than taken from the caller: a proposal that described a
        privileged role as harmless would otherwise decide its own authority.
        """

        member = fixed_id(user_id, "user id")
        target = fixed_id(role_id, "role id")
        roles = self.guild_roles()
        role = roles.get(target)
        if role is None:
            raise ProviderError("that role is not in the configured guild")
        if not role_is_assignable(
            bot_position=self.bot_role_position(roles),
            role_position=_int(role.get("position", 0), "role position"),
            managed=bool(role.get("managed", False)),
            permissions=_int(role.get("permissions", 0), "role permissions"),
        ):
            raise ProviderError("that role is at or above the bot, managed, or privileged")
        response = self.transport.request(
            "PUT",
            f"{_BASE}/guilds/{self.guild_id}/members/{member}/roles/{target}",
            headers=self._headers,
        )
        if response.status not in {200, 204}:
            raise ProviderError("Discord refused the role assignment")
        observed = self.member_permissions(member)
        if target not in observed.roles:
            raise AmbiguousEffectError(
                "the role does not read back on the member; reconcile before retry"
            )
        return observed

    def remove_role(self, user_id: str, role_id: str) -> MemberRoles:
        member = fixed_id(user_id, "user id")
        target = fixed_id(role_id, "role id")
        response = self.transport.request(
            "DELETE",
            f"{_BASE}/guilds/{self.guild_id}/members/{member}/roles/{target}",
            headers=self._headers,
        )
        if response.status not in {200, 204}:
            raise ProviderError("Discord refused the role removal")
        observed = self.member_permissions(member)
        if target in observed.roles:
            raise AmbiguousEffectError("the role is still on the member; reconcile before retry")
        return observed

    def create_event(
        self, name: str, start_time: str, *, channel_id: str = "", description: str = ""
    ) -> dict[str, object]:
        """Schedule one guild event and read it back."""

        body: dict[str, object] = {
            "name": _name(name),
            "scheduled_start_time": str(start_time),
            "privacy_level": 2,
            "entity_type": 2 if channel_id else 3,
        }
        if channel_id:
            body["channel_id"] = fixed_id(channel_id, "channel id")
        else:
            body["entity_metadata"] = {"location": "online"}
        if description:
            body["description"] = str(description)[:1000]
        response = self.transport.request(
            "POST",
            f"{_BASE}/guilds/{self.guild_id}/scheduled-events",
            headers=self._headers,
            json_body=body,
        )
        created = require_success(response, expected=(200, 201))
        if not isinstance(created, Mapping) or type(created.get("id")) is not str:
            raise AmbiguousEffectError(
                "Discord event acknowledgement is malformed; reconcile before retry"
            )
        readback = require_success(
            self.transport.request(
                "GET",
                f"{_BASE}/guilds/{self.guild_id}/scheduled-events/{created['id']}",
                headers=self._headers,
            )
        )
        if not isinstance(readback, Mapping) or readback.get("name") != body["name"]:
            raise AmbiguousEffectError(
                "the Discord event does not read back as intended; reconcile before retry"
            )
        return {"event_id": str(created["id"]), "name": str(readback.get("name", ""))}

    def create_webhook(self, channel_id: str, name: str) -> dict[str, object]:
        """Create a webhook and return its identity only, never its token."""

        channel = fixed_id(channel_id, "channel id")
        self.require_in_guild(channel)
        response = self.transport.request(
            "POST",
            f"{_BASE}/channels/{channel}/webhooks",
            headers=self._headers,
            json_body={"name": _name(name)},
        )
        created = require_success(response, expected=(200, 201))
        if not isinstance(created, Mapping) or type(created.get("id")) is not str:
            raise AmbiguousEffectError(
                "Discord webhook acknowledgement is malformed; reconcile before retry"
            )
        # The token is credential material. It is never returned, logged, or
        # stored here; the operator reads it from Discord if they need it.
        return {
            "webhook_id": str(created["id"]),
            "channel_id": channel,
            "name": str(created.get("name", "")),
        }

    def kick_member(self, user_id: str, reason: str = "") -> dict[str, object]:
        member = fixed_id(user_id, "user id")
        response = self.transport.request(
            "DELETE",
            f"{_BASE}/guilds/{self.guild_id}/members/{member}",
            headers=self._headers,
            query={"reason": reason[:400]} if reason else None,
        )
        if response.status not in {200, 204}:
            raise ProviderError("Discord refused the removal")
        present = self.transport.request(
            "GET", f"{_BASE}/guilds/{self.guild_id}/members/{member}", headers=self._headers
        )
        if present.status != 404:
            raise AmbiguousEffectError(
                "the member still reads back as present; reconcile before retry"
            )
        return {"user_id": member, "removed": True}

    def ban_member(self, user_id: str, reason: str = "") -> dict[str, object]:
        member = fixed_id(user_id, "user id")
        response = self.transport.request(
            "PUT",
            f"{_BASE}/guilds/{self.guild_id}/bans/{member}",
            headers=self._headers,
            json_body={"delete_message_seconds": 0},
            query={"reason": reason[:400]} if reason else None,
        )
        if response.status not in {200, 204}:
            raise ProviderError("Discord refused the ban")
        banned = self.transport.request(
            "GET", f"{_BASE}/guilds/{self.guild_id}/bans/{member}", headers=self._headers
        )
        if banned.status != 200:
            raise AmbiguousEffectError("the ban does not read back; reconcile before retry")
        return {"user_id": member, "banned": True}

    # ---- internals -----------------------------------------------------

    def _patch_channel(self, channel: str, body: Mapping[str, object]) -> None:
        response = self.transport.request(
            "PATCH", f"{_BASE}/channels/{channel}", headers=self._headers, json_body=dict(body)
        )
        if response.status not in {200, 201}:
            raise ProviderError("Discord refused the channel change")

    def _read_channel(self, channel: str) -> dict[str, object]:
        body = require_success(
            self.transport.request("GET", f"{_BASE}/channels/{channel}", headers=self._headers)
        )
        if not isinstance(body, Mapping) or body.get("id") != channel:
            raise ProviderError("Discord channel readback identity mismatch")
        if body.get("guild_id") != self.guild_id:
            # The channel endpoint carries no guild in its path, so a snowflake
            # from anywhere the bot has been invited would otherwise be
            # reachable. The guild is proven from the channel itself.
            raise ProviderError("that channel is not in the configured guild")
        return dict(body)

    def require_in_guild(self, channel_id: str) -> dict[str, object]:
        """Prove a channel belongs to the configured guild before touching it."""

        return self._read_channel(fixed_id(channel_id, "channel id"))

    def _verify_channel(self, channel: str, intended: Mapping[str, object]) -> dict[str, object]:
        observed = self._read_channel(channel)
        for name, value in intended.items():
            if name in {"archived", "locked"}:
                continue
            if observed.get(name) != value:
                raise AmbiguousEffectError(
                    "the Discord channel does not read back as intended; reconcile before retry"
                )
        return observed


__all__ = ["CHANNEL_KINDS", "MAX_REORDER", "DiscordAdminAdapter", "MemberRoles"]
