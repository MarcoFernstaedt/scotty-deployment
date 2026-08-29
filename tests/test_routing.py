from __future__ import annotations

import unittest
from types import SimpleNamespace

from assistant.scotty_business.config import ConfigError, RuntimeConfig
from assistant.scotty_business.ingress import EMPLOYEE_SUMMARY_COMMAND, IngressGuard
from assistant.scotty_business.policy import FIXED_WIZARD_COMMAND, Role
from assistant.scotty_business.routing import (
    ALL_TOOLSETS,
    CLIENT_TOOLSETS,
    RouteKind,
    resolve_route,
    source_fields,
    toolsets_for_route,
)

CLIENT_GUILD = "100000000000000001"
MAINT_GUILD = "110000000000000001"
MAINT_CHANNEL = "220000000000000001"
MAINT_USER = "320000000000000001"


def mapping(*, maintainer_route: object | None = None) -> dict[str, object]:
    raw: dict[str, object] = {
        "version": 1,
        "addons": ["discord", "trello", "ghl", "rentcast"],
        "principals": {
            "maintainer": {"guild_id": CLIENT_GUILD, "channel_id": "200", "user_id": "300"},
            "main_operator": {"guild_id": CLIENT_GUILD, "channel_id": "201", "user_id": "301"},
            "employee": {"guild_id": CLIENT_GUILD, "channel_id": "202", "user_id": "302"},
        },
        "discord": {"announcement_channel_ids": ["210"]},
        "trello": {
            "board_id": "board-1",
            "list_ids": ["list-1"],
            "label_ids": [],
            "custom_field_ids": [],
        },
        "ghl": {"location_id": "location-1"},
        "rentcast": {"endpoints": ["/v1/properties"]},
    }
    if maintainer_route is not None:
        raw["maintainer_route"] = maintainer_route
    return raw


def routed_config() -> RuntimeConfig:
    return RuntimeConfig.from_mapping(
        mapping(
            maintainer_route={
                "guild_id": MAINT_GUILD,
                "channel_id": MAINT_CHANNEL,
                "user_id": MAINT_USER,
                "profile": "operations-full",
            }
        )
    )


def source(
    guild: str = CLIENT_GUILD,
    channel: str = "201",
    user: str = "301",
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


class MaintainerRouteConfigTests(unittest.TestCase):
    def test_maintainer_route_is_optional_and_absent_means_no_hidden_route(self) -> None:
        config = RuntimeConfig.from_mapping(mapping())
        self.assertIsNone(config.maintainer_route)
        self.assertIsNone(
            resolve_route(config, source(guild=MAINT_GUILD, channel=MAINT_CHANNEL, user=MAINT_USER))
        )

    def test_maintainer_route_must_not_share_the_client_guild(self) -> None:
        with self.assertRaises(ConfigError):
            RuntimeConfig.from_mapping(
                mapping(
                    maintainer_route={
                        "guild_id": CLIENT_GUILD,
                        "channel_id": MAINT_CHANNEL,
                        "user_id": MAINT_USER,
                        "profile": "operations-full",
                    }
                )
            )

    def test_maintainer_route_must_not_reuse_a_client_channel_or_tuple(self) -> None:
        with self.assertRaises(ConfigError):
            RuntimeConfig.from_mapping(
                mapping(
                    maintainer_route={
                        "guild_id": MAINT_GUILD,
                        "channel_id": "201",
                        "user_id": MAINT_USER,
                        "profile": "operations-full",
                    }
                )
            )

    def test_maintainer_profile_name_must_be_a_bounded_slug(self) -> None:
        with self.assertRaises(ConfigError):
            RuntimeConfig.from_mapping(
                mapping(
                    maintainer_route={
                        "guild_id": MAINT_GUILD,
                        "channel_id": MAINT_CHANNEL,
                        "user_id": MAINT_USER,
                        "profile": "operations full!",
                    }
                )
            )

    def test_maintainer_channel_is_never_a_client_discord_destination(self) -> None:
        config = routed_config()
        destinations = {principal.channel_id for principal in config.principals}
        destinations.update(config.announcement_channel_ids)
        self.assertNotIn(MAINT_CHANNEL, destinations)
        self.assertNotIn(MAINT_CHANNEL, config.client_discord_destinations())


class RouteResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = routed_config()

    def test_client_sources_resolve_to_bounded_per_role_profiles(self) -> None:
        cases = {
            ("200", "300"): Role.MAINTAINER,
            ("201", "301"): Role.MAIN_OPERATOR,
            ("202", "302"): Role.EMPLOYEE,
        }
        seen: set[str] = set()
        for (channel, user), role in cases.items():
            route = resolve_route(self.config, source(channel=channel, user=user))
            assert route is not None
            self.assertEqual(route.kind, RouteKind.CLIENT)
            self.assertEqual(route.toolsets, CLIENT_TOOLSETS)
            assert route.principal is not None
            self.assertEqual(route.principal.role, role)
            seen.add(route.profile)
        self.assertEqual(len(seen), 3, "each client role keeps a profile-local surface")

    def test_exact_maintainer_tuple_resolves_to_the_full_profile(self) -> None:
        route = resolve_route(
            self.config, source(guild=MAINT_GUILD, channel=MAINT_CHANNEL, user=MAINT_USER)
        )
        assert route is not None
        self.assertEqual(route.kind, RouteKind.MAINTAINER)
        self.assertEqual(route.profile, "operations-full")
        self.assertEqual(route.toolsets, ALL_TOOLSETS)
        self.assertIsNone(route.principal)

    def test_wrong_user_in_the_maintainer_channel_is_rejected(self) -> None:
        for wrong_user in ("300", "301", "302", "999999999999999999"):
            with self.subTest(user=wrong_user):
                self.assertIsNone(
                    resolve_route(
                        self.config,
                        source(guild=MAINT_GUILD, channel=MAINT_CHANNEL, user=wrong_user),
                    )
                )

    def test_every_wrong_maintainer_cross_product_is_rejected(self) -> None:
        cases = [
            source(guild=CLIENT_GUILD, channel=MAINT_CHANNEL, user=MAINT_USER),
            source(guild=MAINT_GUILD, channel="201", user=MAINT_USER),
            source(guild=MAINT_GUILD, channel=MAINT_CHANNEL, user="301"),
            source(guild=MAINT_GUILD, channel=MAINT_CHANNEL, user=MAINT_USER, is_bot=True),
            source(guild=MAINT_GUILD, channel=MAINT_CHANNEL, user=MAINT_USER, platform="telegram"),
            source(guild=MAINT_GUILD, channel="900", user=MAINT_USER, parent="201"),
            source(guild=CLIENT_GUILD, channel="900", user=MAINT_USER, parent=MAINT_CHANNEL),
        ]
        for candidate in cases:
            with self.subTest(source=candidate):
                self.assertIsNone(resolve_route(self.config, candidate))

    def test_maintainer_thread_is_accepted_only_under_its_configured_parent(self) -> None:
        route = resolve_route(
            self.config,
            source(guild=MAINT_GUILD, channel="900", user=MAINT_USER, parent=MAINT_CHANNEL),
        )
        assert route is not None
        self.assertEqual(route.kind, RouteKind.MAINTAINER)

    def test_maintainer_user_gets_no_client_surface_outside_its_exact_tuple(self) -> None:
        self.assertIsNone(resolve_route(self.config, source(channel="201", user=MAINT_USER)))

    def test_mismatched_scope_and_guild_ids_fail_closed(self) -> None:
        broken = source(guild=MAINT_GUILD, channel=MAINT_CHANNEL, user=MAINT_USER)
        broken.scope_id = CLIENT_GUILD
        self.assertIsNone(resolve_route(self.config, broken))

    def test_source_fields_reads_only_immutable_gateway_provenance(self) -> None:
        fields = source_fields(source())
        self.assertEqual(
            fields,
            {
                "platform": "discord",
                "guild_id": CLIENT_GUILD,
                "channel_id": "201",
                "user_id": "301",
                "parent_channel_id": None,
                "is_bot": False,
            },
        )


class IngressRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.outbound: list[tuple[str, str]] = []
        self.guard = IngressGuard(
            routed_config(), lambda channel, text: self.outbound.append((channel, text))
        )

    def event(self, text: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(text=text, source=source(**kwargs))  # type: ignore[arg-type]

    def test_exact_maintainer_tuple_reaches_the_full_profile(self) -> None:
        result = self.guard(
            self.event(
                "Show the redacted approval receipts",
                guild=MAINT_GUILD,
                channel=MAINT_CHANNEL,
                user=MAINT_USER,
            )
        )
        self.assertEqual(result, {"action": "allow"})
        self.assertEqual(self.outbound, [])

    def test_wrong_user_in_the_maintainer_channel_is_silently_rejected_pre_model(self) -> None:
        for wrong_user in ("300", "301", "302"):
            with self.subTest(user=wrong_user):
                self.assertEqual(
                    self.guard(
                        self.event(
                            "hello", guild=MAINT_GUILD, channel=MAINT_CHANNEL, user=wrong_user
                        )
                    ),
                    {"action": "skip", "reason": "unauthorized"},
                )
        self.assertEqual(self.outbound, [], "no reply ever discloses the hidden route")

    def test_client_fixed_paths_never_target_the_maintainer_route(self) -> None:
        self.guard(self.event(FIXED_WIZARD_COMMAND, channel="200", user="300"))
        self.guard(self.event(EMPLOYEE_SUMMARY_COMMAND))
        self.guard(self.event("Please write code for an integration"))
        destinations = {channel for channel, _ in self.outbound}
        self.assertTrue(destinations)
        self.assertNotIn(MAINT_CHANNEL, destinations)

    def test_maintainer_route_is_not_narrowed_by_the_client_coding_refusal(self) -> None:
        result = self.guard(
            self.event(
                "Please write code for an integration",
                guild=MAINT_GUILD,
                channel=MAINT_CHANNEL,
                user=MAINT_USER,
            )
        )
        self.assertEqual(result, {"action": "allow"})
        self.assertEqual(self.outbound, [])

    def test_credentials_are_still_withheld_from_the_model_on_the_maintainer_route(self) -> None:
        result = self.guard(
            self.event(
                "token is " + "ghp_" + ("a" * 28),
                guild=MAINT_GUILD,
                channel=MAINT_CHANNEL,
                user=MAINT_USER,
            )
        )
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.outbound, [])


class ToolsetResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = routed_config()

    def test_unresolved_sources_receive_no_toolset(self) -> None:
        self.assertEqual(toolsets_for_route(None), ())
        self.assertEqual(
            toolsets_for_route(resolve_route(self.config, source(user="999"))),
            (),
        )

    def test_client_sources_receive_only_the_bounded_scotty_toolset(self) -> None:
        for channel, user in (("200", "300"), ("201", "301"), ("202", "302")):
            with self.subTest(channel=channel):
                self.assertEqual(
                    toolsets_for_route(
                        resolve_route(self.config, source(channel=channel, user=user))
                    ),
                    ("scotty",),
                )

    def test_only_the_exact_maintainer_tuple_receives_the_full_inventory(self) -> None:
        self.assertEqual(
            toolsets_for_route(
                resolve_route(
                    self.config,
                    source(guild=MAINT_GUILD, channel=MAINT_CHANNEL, user=MAINT_USER),
                )
            ),
            ALL_TOOLSETS,
        )


if __name__ == "__main__":
    unittest.main()
