"""Shared synthetic identifiers and configuration for the test suite.

Every identifier here is invented. Nothing in this file corresponds to a real
guild, channel, user, board, location, or credential.
"""

from __future__ import annotations

from types import SimpleNamespace

from assistant.scotty_business.config import RuntimeConfig

CLIENT_GUILD = "100000000000000001"
OPERATOR_CHANNEL = "201000000000000001"
OPERATOR_USER = "301000000000000001"
EMPLOYEE_CHANNEL = "202000000000000001"
EMPLOYEE_USER = "302000000000000001"
ANNOUNCEMENT_CHANNEL = "210000000000000001"

ROUTE_GUILD = "110000000000000001"
ROUTE_CHANNEL = "220000000000000001"
ROUTE_USER = "320000000000000001"
ROUTE_PROFILE = "scotty-maintainer"


def private_mapping(**overrides: object) -> dict[str, object]:
    mapping: dict[str, object] = {
        "version": 1,
        "addons": ["discord", "trello", "ghl", "rentcast", "google_workspace"],
        "principals": {
            "main_operator": {
                "guild_id": CLIENT_GUILD,
                "channel_id": OPERATOR_CHANNEL,
                "user_id": OPERATOR_USER,
            },
            "employee": {
                "guild_id": CLIENT_GUILD,
                "channel_id": EMPLOYEE_CHANNEL,
                "user_id": EMPLOYEE_USER,
            },
        },
        "discord": {"announcement_channel_ids": [ANNOUNCEMENT_CHANNEL]},
        # Trent's assistant is named; the employee names their own.
        "personas": {"main_operator": "Scotty"},
        "maintainer_route": {
            "guild_id": ROUTE_GUILD,
            "channel_id": ROUTE_CHANNEL,
            "user_id": ROUTE_USER,
            "profile": ROUTE_PROFILE,
        },
        "trello": {
            "board_id": "board-1",
            "list_ids": ["list-1", "list-2"],
            "label_ids": ["label-1"],
            "custom_field_ids": ["field-1"],
        },
        "ghl": {"location_id": "location-1"},
        "rentcast": {"endpoints": ["/v1/properties", "/v1/avm/value", "/v1/avm/rent/long-term"]},
    }
    for key, value in overrides.items():
        if value is None:
            mapping.pop(key, None)
        else:
            mapping[key] = value
    return mapping


def config(**overrides: object) -> RuntimeConfig:
    return RuntimeConfig.from_mapping(private_mapping(**overrides))


def source(
    guild: str = CLIENT_GUILD,
    channel: str = OPERATOR_CHANNEL,
    user: str = OPERATOR_USER,
    *,
    parent: str | None = None,
    is_bot: bool = False,
    platform: str = "discord",
) -> SimpleNamespace:
    return SimpleNamespace(
        platform=SimpleNamespace(value=platform),
        guild_id=guild,
        scope_id=guild,
        chat_id=channel,
        user_id=user,
        parent_chat_id=parent,
        is_bot=is_bot,
    )


def event(
    guild: str = CLIENT_GUILD,
    channel: str = OPERATOR_CHANNEL,
    user: str = OPERATOR_USER,
    text: str = "Show configured leads",
    *,
    parent: str | None = None,
    is_bot: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        source=source(guild, channel, user, parent=parent, is_bot=is_bot),
    )
