from __future__ import annotations

import unittest
from types import SimpleNamespace

from test_setup import (
    CLIENT_GUILD,
    EMPLOYEE_CHANNEL,
    MAINT_CHANNEL,
    MAINT_GUILD,
    OPERATOR_CHANNEL,
    maintainer_sample,
)

from assistant.scotty_business.routing import (
    CLIENT_PROFILES,
    MAINTAINER_PROFILE,
    SERVED_PROFILES,
    ProfileRouteError,
    match_profile_route,
    parse_profile_routes,
)
from assistant.scotty_business.setup import (
    hermes_config_mapping,
    profile_config_mapping,
    render_hermes_config,
    render_profile_config,
)


def gateway() -> dict[str, object]:
    section = hermes_config_mapping(maintainer_sample())["gateway"]
    assert isinstance(section, dict)
    return section


def source(guild: str, chat: str, platform: str = "discord") -> SimpleNamespace:
    return SimpleNamespace(
        platform=SimpleNamespace(value=platform),
        guild_id=guild,
        scope_id=guild,
        chat_id=chat,
        user_id="999000000000000001",
        parent_chat_id=None,
        is_bot=False,
    )


class NativeGatewayConfigTests(unittest.TestCase):
    def test_multiplex_profiles_is_enabled(self) -> None:
        self.assertIs(gateway()["multiplex_profiles"], True)
        self.assertIn("multiplex_profiles: true", render_hermes_config(maintainer_sample()))

    def test_exactly_three_native_profile_routes_are_rendered(self) -> None:
        routes = parse_profile_routes(hermes_config_mapping(maintainer_sample()))
        self.assertEqual(len(routes), 3)
        self.assertEqual(
            [route.name for route in routes],
            [
                "maintainer-private-channel",
                "main-operator-private-channel",
                "employee-private-channel",
            ],
        )

    def test_every_route_uses_the_native_keys_only(self) -> None:
        entries = gateway()["profile_routes"]
        assert isinstance(entries, list)
        for entry in entries:
            assert isinstance(entry, dict)
            self.assertEqual(set(entry), {"name", "platform", "guild_id", "chat_id", "profile"})
            self.assertNotIn("channel_id", entry)
            self.assertEqual(entry["platform"], "discord")
            self.assertIsInstance(entry["guild_id"], str)
            self.assertIsInstance(entry["chat_id"], str)

    def test_every_routed_profile_is_in_the_served_allowlist(self) -> None:
        allowlist = gateway()["multiplex_profile_allowlist"]
        assert isinstance(allowlist, list)
        self.assertEqual(sorted(allowlist), sorted(SERVED_PROFILES))
        for route in parse_profile_routes(hermes_config_mapping(maintainer_sample())):
            self.assertIn(route.profile, allowlist)

    def test_the_rendered_text_carries_the_exact_native_route_block(self) -> None:
        rendered = render_hermes_config(maintainer_sample())
        self.assertIn("  profile_routes:\n", rendered)
        self.assertIn('    - name: "maintainer-private-channel"\n', rendered)
        self.assertIn('      platform: "discord"\n', rendered)
        self.assertIn(f'      guild_id: "{MAINT_GUILD}"\n', rendered)
        self.assertIn(f'      chat_id: "{MAINT_CHANNEL}"\n', rendered)
        self.assertIn(f'      profile: "{MAINTAINER_PROFILE}"\n', rendered)
        self.assertNotIn("channel_id:", rendered)

    def test_only_the_three_routed_channels_are_admitted_by_the_gateway(self) -> None:
        rendered = render_hermes_config(maintainer_sample())
        for channel in (MAINT_CHANNEL, OPERATOR_CHANNEL, EMPLOYEE_CHANNEL):
            self.assertIn(channel, rendered)
        self.assertEqual(rendered.count("allowed_channels:"), 1)


class NativeRouteMatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.routes = parse_profile_routes(hermes_config_mapping(maintainer_sample()))

    def test_the_exact_maintainer_tuple_resolves_to_the_full_profile(self) -> None:
        self.assertEqual(
            match_profile_route(self.routes, source(MAINT_GUILD, MAINT_CHANNEL)),
            MAINTAINER_PROFILE,
        )

    def test_the_client_channels_resolve_to_their_bounded_profiles(self) -> None:
        self.assertEqual(
            match_profile_route(self.routes, source(CLIENT_GUILD, OPERATOR_CHANNEL)),
            "scotty-main-operator",
        )
        self.assertEqual(
            match_profile_route(self.routes, source(CLIENT_GUILD, EMPLOYEE_CHANNEL)),
            "scotty-employee",
        )

    def test_wrong_and_mixed_tuples_match_no_route(self) -> None:
        cases = [
            ("wrong guild", source("999000000000000001", OPERATOR_CHANNEL)),
            ("private channel in the client guild", source(CLIENT_GUILD, MAINT_CHANNEL)),
            ("client channel in the private guild", source(MAINT_GUILD, OPERATOR_CHANNEL)),
            ("unknown channel", source(CLIENT_GUILD, "900000000000000001")),
            ("wrong platform", source(MAINT_GUILD, MAINT_CHANNEL, platform="telegram")),
        ]
        for label, candidate in cases:
            with self.subTest(case=label):
                self.assertIsNone(match_profile_route(self.routes, candidate))

    def test_a_route_missing_a_native_key_is_rejected(self) -> None:
        broken = {
            "gateway": {
                "multiplex_profile_allowlist": list(SERVED_PROFILES),
                "profile_routes": [
                    {"name": "x", "platform": "discord", "guild_id": "1", "profile": "p"}
                ],
            }
        }
        with self.assertRaises(ProfileRouteError):
            parse_profile_routes(broken)

    def test_a_route_naming_an_unserved_profile_is_rejected(self) -> None:
        broken = {
            "gateway": {
                "multiplex_profile_allowlist": list(SERVED_PROFILES),
                "profile_routes": [
                    {
                        "name": "x",
                        "platform": "discord",
                        "guild_id": "1",
                        "chat_id": "2",
                        "profile": "not-served",
                    }
                ],
            }
        }
        with self.assertRaises(ProfileRouteError):
            parse_profile_routes(broken)

    def test_multiplex_routing_that_is_switched_off_is_rejected(self) -> None:
        mapping = hermes_config_mapping(maintainer_sample())
        gateway_section = mapping["gateway"]
        assert isinstance(gateway_section, dict)
        gateway_section["multiplex_profiles"] = False
        with self.assertRaises(ProfileRouteError):
            parse_profile_routes(mapping)


class ProfileConfigTests(unittest.TestCase):
    def test_the_maintainer_profile_enables_no_plugin_and_keeps_the_full_inventory(self) -> None:
        profile = profile_config_mapping(MAINTAINER_PROFILE, maintainer_sample())
        self.assertEqual(profile["plugins"], {"enabled": ["scotty-guard"]})
        toolsets = profile["platform_toolsets"]
        assert isinstance(toolsets, dict)
        self.assertEqual(toolsets["discord"], ["*"])
        tools = profile["tools"]
        assert isinstance(tools, dict)
        self.assertEqual(tools["tool_search"], {"enabled": True})
        rendered = render_profile_config(MAINTAINER_PROFILE, maintainer_sample())
        self.assertNotIn("scotty-business", rendered)
        self.assertNotIn('discord: ["scotty"]', rendered)

    def test_the_base_configuration_is_bounded_so_a_failed_override_stays_bounded(self) -> None:
        """A profile widens its own surface. It never inherits a wider default."""

        base = hermes_config_mapping(maintainer_sample())
        toolsets = base["platform_toolsets"]
        assert isinstance(toolsets, dict)
        self.assertEqual(toolsets["discord"], ["scotty"])
        tools = base["tools"]
        assert isinstance(tools, dict)
        self.assertEqual(tools["tool_search"], {"enabled": False})

    def test_each_client_profile_enables_only_the_bounded_scotty_toolset(self) -> None:
        for profile_name in CLIENT_PROFILES.values():
            with self.subTest(profile=profile_name):
                profile = profile_config_mapping(profile_name, maintainer_sample())
                self.assertEqual(profile["plugins"], {"enabled": ["scotty-business"]})
                toolsets = profile["platform_toolsets"]
                assert isinstance(toolsets, dict)
                self.assertEqual(toolsets["discord"], ["scotty"])
                tools = profile["tools"]
                assert isinstance(tools, dict)
                self.assertEqual(tools["tool_search"], {"enabled": False})

    def test_an_unserved_profile_name_is_refused(self) -> None:
        with self.assertRaises(ProfileRouteError):
            profile_config_mapping("not-a-served-profile", maintainer_sample())

    def test_client_and_maintainer_profiles_are_distinct(self) -> None:
        self.assertNotIn(MAINTAINER_PROFILE, CLIENT_PROFILES.values())
        self.assertEqual(len(set(SERVED_PROFILES)), 3)


if __name__ == "__main__":
    unittest.main()
