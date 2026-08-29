"""Native multiplexed profile routing.

The pinned gateway resolves a Discord source to a served profile from
`gateway.profile_routes`, matching `platform`, `guild_id`, and `chat_id`. Three
profiles are served: one full profile for the private maintainer channel and one
bounded profile for each client channel.

This module owns two separate things:

* `parse_profile_routes` / `match_profile_route` model the native routing
  contract, so the generated configuration can be validated against it before a
  host ever loads it.
* `resolve_route` is the plugin's own pre-dispatch tuple gate. It runs inside the
  bounded client profiles, where the plugin is installed, and additionally binds
  the acting user, which native routing does not match.

Nothing here may be surfaced to a client profile. Route decisions carry no
human-readable private identifiers, and an unresolved source is rejected without
explanation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .config import RuntimeConfig
from .policy import Principal, Role, authorize_source

CLIENT_TOOLSETS: tuple[str, ...] = ("scotty",)
ALL_TOOLSETS: tuple[str, ...] = ("*",)

#: The separately served full profile reached only by the private route.
MAINTAINER_PROFILE = "scotty-maintainer"

#: The bounded profiles reached by the two client channels.
CLIENT_PROFILES: Mapping[Role, str] = {
    Role.MAIN_OPERATOR: "scotty-main-operator",
    Role.EMPLOYEE: "scotty-employee",
}

#: Every profile the gateway is allowed to serve.
SERVED_PROFILES: tuple[str, ...] = (
    MAINTAINER_PROFILE,
    CLIENT_PROFILES[Role.MAIN_OPERATOR],
    CLIENT_PROFILES[Role.EMPLOYEE],
)

_NATIVE_ROUTE_KEYS = frozenset({"name", "platform", "guild_id", "chat_id", "profile"})


class ProfileRouteError(ValueError):
    """Generated routing configuration does not satisfy the native contract."""


class RouteKind(StrEnum):
    CLIENT = "client"
    MAINTAINER = "maintainer"


@dataclass(frozen=True, slots=True)
class ProfileRoute:
    """One entry of the native `gateway.profile_routes` list."""

    name: str
    platform: str
    guild_id: str
    chat_id: str
    profile: str


@dataclass(frozen=True, slots=True)
class Route:
    kind: RouteKind
    profile: str
    toolsets: tuple[str, ...]
    principal: Principal | None = None


def client_profile(role: Role) -> str:
    try:
        return CLIENT_PROFILES[role]
    except KeyError as exc:
        raise ProfileRouteError(f"{role.value} has no bounded client profile") from exc


def parse_profile_routes(config: Mapping[str, object]) -> tuple[ProfileRoute, ...]:
    """Validate a rendered gateway configuration against the native contract."""

    gateway = config.get("gateway")
    if not isinstance(gateway, Mapping):
        raise ProfileRouteError("gateway configuration is missing")
    if gateway.get("multiplex_profiles") is not True:
        raise ProfileRouteError("gateway.multiplex_profiles must be true")
    allowlist = gateway.get("multiplex_profile_allowlist")
    if not isinstance(allowlist, list) or not allowlist:
        raise ProfileRouteError("gateway.multiplex_profile_allowlist must list served profiles")
    served = {item for item in allowlist if type(item) is str}
    if len(served) != len(allowlist):
        raise ProfileRouteError("gateway.multiplex_profile_allowlist is malformed")
    entries = gateway.get("profile_routes")
    if not isinstance(entries, list) or not entries:
        raise ProfileRouteError("gateway.profile_routes must list at least one route")

    routes: list[ProfileRoute] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != _NATIVE_ROUTE_KEYS:
            raise ProfileRouteError(
                "each gateway.profile_routes entry needs exactly "
                "name, platform, guild_id, chat_id, and profile"
            )
        values = {key: entry[key] for key in _NATIVE_ROUTE_KEYS}
        if any(type(value) is not str or not value for value in values.values()):
            raise ProfileRouteError("gateway.profile_routes values must be non-empty strings")
        route = ProfileRoute(
            name=str(values["name"]),
            platform=str(values["platform"]),
            guild_id=str(values["guild_id"]),
            chat_id=str(values["chat_id"]),
            profile=str(values["profile"]),
        )
        if route.profile not in served:
            raise ProfileRouteError(
                "a route names a profile that the gateway is not configured to serve"
            )
        routes.append(route)

    keys = {(route.platform, route.guild_id, route.chat_id) for route in routes}
    if len(keys) != len(routes):
        raise ProfileRouteError("gateway.profile_routes contains a duplicate source")
    if len({route.name for route in routes}) != len(routes):
        raise ProfileRouteError("gateway.profile_routes contains a duplicate route name")
    return tuple(routes)


def match_profile_route(routes: Sequence[ProfileRoute], source: object) -> str | None:
    """Resolve a source exactly the way native routing does, or return None."""

    fields = source_fields(source)
    platform = fields["platform"]
    guild_id = fields["guild_id"]
    chat_id = fields["channel_id"]
    if not all(type(value) is str and value for value in (platform, guild_id, chat_id)):
        return None
    for route in routes:
        if route.platform == platform and route.guild_id == guild_id and route.chat_id == chat_id:
            return route.profile
    return None


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
    """Return the exact route for a Discord source, or ``None`` to reject it.

    This is the plugin's own gate. It binds the acting user as well as the guild
    and channel, because native routing matches guild and channel only.
    """

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
        route.guild_id == guild_id
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
