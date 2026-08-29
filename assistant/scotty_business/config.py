from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .policy import Principal, Role


class ConfigError(ValueError):
    """Private runtime configuration is absent or unsafe."""


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{field} must be an object")
    return value


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    return value.strip()


def _texts(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{field} must be a list")
    result = tuple(_text(item, f"{field}[]") for item in value)
    if not allow_empty and not result:
        raise ConfigError(f"{field} must not be empty")
    if len(set(result)) != len(result):
        raise ConfigError(f"{field} contains duplicates")
    return result


@dataclass(frozen=True, slots=True)
class TrelloScope:
    board_id: str
    list_ids: tuple[str, ...]
    label_ids: tuple[str, ...]
    custom_field_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    version: int
    addons: tuple[str, ...]
    principals: tuple[Principal, ...]
    announcement_channel_ids: tuple[str, ...]
    trello: TrelloScope
    ghl_location_id: str
    rentcast_endpoints: tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> RuntimeConfig:
        if type(raw.get("version")) is not int or raw["version"] != 1:
            raise ConfigError("version must be 1")
        addons = _texts(raw.get("addons"), "addons")
        if len(addons) > 6:
            raise ConfigError("addons exceeds the six-add-on deployment cap")

        principal_raw = _mapping(raw.get("principals"), "principals")
        principals: list[Principal] = []
        for role in Role:
            values = _mapping(principal_raw.get(role.value), f"principals.{role.value}")
            principals.append(
                Principal(
                    guild_id=_text(values.get("guild_id"), f"principals.{role.value}.guild_id"),
                    channel_id=_text(
                        values.get("channel_id"), f"principals.{role.value}.channel_id"
                    ),
                    user_id=_text(values.get("user_id"), f"principals.{role.value}.user_id"),
                    role=role,
                )
            )
        identity_tuples = {(p.guild_id, p.channel_id, p.user_id) for p in principals}
        if len(identity_tuples) != 3:
            raise ConfigError("principal tuples must be unique")

        discord = _mapping(raw.get("discord"), "discord")
        trello_raw = _mapping(raw.get("trello"), "trello")
        ghl = _mapping(raw.get("ghl"), "ghl")
        rentcast = _mapping(raw.get("rentcast"), "rentcast")
        endpoints = _texts(rentcast.get("endpoints"), "rentcast.endpoints")
        if any(not endpoint.startswith("/v1/") or ".." in endpoint for endpoint in endpoints):
            raise ConfigError("RentCast endpoints must be fixed /v1 paths")

        return cls(
            version=1,
            addons=addons,
            principals=tuple(principals),
            announcement_channel_ids=_texts(
                discord.get("announcement_channel_ids"),
                "discord.announcement_channel_ids",
            ),
            trello=TrelloScope(
                board_id=_text(trello_raw.get("board_id"), "trello.board_id"),
                list_ids=_texts(trello_raw.get("list_ids"), "trello.list_ids"),
                label_ids=_texts(trello_raw.get("label_ids"), "trello.label_ids", allow_empty=True),
                custom_field_ids=_texts(
                    trello_raw.get("custom_field_ids"),
                    "trello.custom_field_ids",
                    allow_empty=True,
                ),
            ),
            ghl_location_id=_text(ghl.get("location_id"), "ghl.location_id"),
            rentcast_endpoints=endpoints,
        )
