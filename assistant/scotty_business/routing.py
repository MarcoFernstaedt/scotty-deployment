"""Native multiplexed profile routing for bounded client and full maintainer surfaces.

Routing is decided from immutable gateway provenance before any session or model
activity. The pinned runtime's own profile routing matches guild and channel; it
does not match the acting user, so the exact maintainer user ID is checked here.

Nothing in this module may be surfaced to a client profile. Route decisions carry
no human-readable maintainer identifiers, and an unresolved source is rejected
without explanation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .config import RuntimeConfig
from .policy import Principal, Role, authorize_source

CLIENT_TOOLSETS: tuple[str, ...] = ("scotty",)
ALL_TOOLSETS: tuple[str, ...] = ("*",)

_CLIENT_PROFILES: Mapping[Role, str] = {
    Role.MAINTAINER: "scotty-maintainer",
    Role.MAIN_OPERATOR: "scotty-main-operator",
    Role.EMPLOYEE: "scotty-employee",
}


class RouteKind(StrEnum):
    CLIENT = "client"
    MAINTAINER = "maintainer"


@dataclass(frozen=True, slots=True)
class Route:
    kind: RouteKind
    profile: str
    toolsets: tuple[str, ...]
    principal: Principal | None = None


def client_profile(role: Role) -> str:
    return _CLIENT_PROFILES[role]


def source_fields(source: object) -> dict[str, object]:
    """Extract the immutable provenance fields the gateway attaches to a source."""

    scope_id = getattr(source, "scope_id", None)
    guild_id = getattr(source, "guild_id", None)
    if scope_id is not None and guild_id is not None and scope_id != guild_id:
        # A disagreeing scope/guild pair is unusable provenance, not a guild.
        guild: object = None
    else:
        guild = scope_id if scope_id is not None else guild_id
    return {
        "platform": getattr(getattr(source, "platform", None), "value", None),
        "guild_id": guild,
        "channel_id": getattr(source, "chat_id", None),
        "user_id": getattr(source, "user_id", None),
        "parent_channel_id": getattr(source, "parent_chat_id", None),
        "is_bot": getattr(source, "is_bot", None),
    }


def resolve_route(config: RuntimeConfig, source: object) -> Route | None:
    """Return the exact route for a Discord source, or ``None`` to reject it."""

    fields = source_fields(source)
    if fields["platform"] != "discord" or fields["is_bot"] is not False:
        return None
    guild_id = fields["guild_id"]
    channel_id = fields["channel_id"]
    user_id = fields["user_id"]
    parent_channel_id = fields["parent_channel_id"]
    if not all(type(value) is str and value for value in (guild_id, channel_id, user_id)):
        return None
    if parent_channel_id is not None and (
        type(parent_channel_id) is not str or not parent_channel_id
    ):
        return None
    effective_channel = parent_channel_id or channel_id

    route = config.maintainer_route
    if (
        route is not None
        and route.guild_id == guild_id
        and route.channel_id == effective_channel
        and route.user_id == user_id
    ):
        return Route(kind=RouteKind.MAINTAINER, profile=route.profile, toolsets=ALL_TOOLSETS)

    principal = authorize_source(
        config.principals, guild_id, channel_id, user_id, parent_channel_id
    )
    if principal is None:
        return None
    return Route(
        kind=RouteKind.CLIENT,
        profile=client_profile(principal.role),
        toolsets=CLIENT_TOOLSETS,
        principal=principal,
    )


def toolsets_for_route(route: Route | None) -> tuple[str, ...]:
    """Fail closed: an unresolved source never receives a model toolset."""

    if route is None:
        return ()
    return route.toolsets
