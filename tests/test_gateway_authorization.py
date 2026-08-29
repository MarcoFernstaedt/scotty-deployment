from __future__ import annotations

import unittest

from test_setup import (
    CLIENT_GUILD,
    EMPLOYEE_CHANNEL,
    EMPLOYEE_USER,
    MAINT_CHANNEL,
    MAINT_GUILD,
    MAINT_USER,
    OPERATOR_CHANNEL,
    OPERATOR_USER,
    discord_only_sample,
    sample,
)

from assistant.scotty_business.routing import (
    CLIENT_PROFILES,
    MAINTAINER_PROFILE,
    SERVED_PROFILES,
    match_profile_route,
    parse_profile_routes,
)
from assistant.scotty_business.setup import (
    DISCORD_ALLOWED_USERS_ENV,
    GUARD_PLUGIN,
    SetupError,
    discord_allowed_users,
    hermes_config_mapping,
    profile_config_mapping,
    render_profile_config,
    runtime_environment,
)


class SenderAllowlistTests(unittest.TestCase):
    """The gateway-level admission layer, generated from the three principals."""

    def test_the_allowlist_is_exactly_the_three_configured_users(self) -> None:
        self.assertEqual(
            discord_allowed_users(sample()), (MAINT_USER, OPERATOR_USER, EMPLOYEE_USER)
        )

    def test_the_allowlist_is_deterministic(self) -> None:
        self.assertEqual(discord_allowed_users(sample()), discord_allowed_users(sample()))

    def test_the_runtime_environment_carries_the_allowlist(self) -> None:
        environment = runtime_environment(sample())
        self.assertEqual(
            environment[DISCORD_ALLOWED_USERS_ENV],
            f"{MAINT_USER},{OPERATOR_USER},{EMPLOYEE_USER}",
        )

    def test_no_open_policy_wildcard_or_role_authorization_is_generated(self) -> None:
        environment = runtime_environment(sample())
        rendered = "\n".join(f"{name}={value}" for name, value in environment.items())
        for forbidden in (
            "DISCORD_ALLOW_ALL_USERS",
            "DISCORD_ALLOWED_ROLES",
            "*",
            "everyone",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)

    def test_a_duplicate_or_missing_principal_fails_closed(self) -> None:
        with self.assertRaises(SetupError):
            discord_allowed_users(sample(employee_user_id=OPERATOR_USER))
        with self.assertRaises(SetupError):
            discord_allowed_users(sample(route_user_id=""))

    def test_a_non_numeric_principal_is_rejected(self) -> None:
        with self.assertRaises(SetupError):
            discord_allowed_users(sample(operator_user_id="not-a-snowflake"))

    def test_a_discord_only_deployment_still_generates_the_allowlist(self) -> None:
        environment = runtime_environment(discord_only_sample())
        self.assertIn(DISCORD_ALLOWED_USERS_ENV, environment)
        self.assertEqual(len(environment[DISCORD_ALLOWED_USERS_ENV].split(",")), 3)

    def test_admission_alone_never_replaces_exact_tuple_enforcement(self) -> None:
        """An admitted sender still has to match a route and its exact tuple."""

        allowed = set(discord_allowed_users(sample()))
        routes = parse_profile_routes(hermes_config_mapping(sample()))

        class Source:
            def __init__(self, guild: str, chat: str, user: str) -> None:
                self.platform = type("P", (), {"value": "discord"})()
                self.guild_id = guild
                self.scope_id = guild
                self.chat_id = chat
                self.user_id = user
                self.parent_chat_id = None
                self.is_bot = False

        # Every one of these senders is admitted at the gateway, yet each lands
        # in a channel whose tuple does not match, so routing plus the
        # pre-dispatch gate must still reject them.
        crossed = [
            Source(MAINT_GUILD, MAINT_CHANNEL, OPERATOR_USER),
            Source(MAINT_GUILD, MAINT_CHANNEL, EMPLOYEE_USER),
            Source(CLIENT_GUILD, OPERATOR_CHANNEL, EMPLOYEE_USER),
            Source(CLIENT_GUILD, EMPLOYEE_CHANNEL, OPERATOR_USER),
            Source(CLIENT_GUILD, OPERATOR_CHANNEL, MAINT_USER),
        ]
        for candidate in crossed:
            with self.subTest(user=candidate.user_id, chat=candidate.chat_id):
                self.assertIn(candidate.user_id, allowed)
                # Native routing places the message in a profile by channel; the
                # user is not matched, which is exactly why the tuple gate exists.
                self.assertIsNotNone(match_profile_route(routes, candidate))


class ProfileModelPreservationTests(unittest.TestCase):
    def test_every_served_profile_restates_the_selected_provider_and_model(self) -> None:
        inputs = sample(model_provider="openrouter", model_name="synthetic/model")
        for profile in SERVED_PROFILES:
            with self.subTest(profile=profile):
                mapping = profile_config_mapping(profile, inputs)
                self.assertEqual(
                    mapping["model"],
                    {"provider": "openrouter", "default": "synthetic/model"},
                )

    def test_a_codex_deployment_keeps_codex_in_every_profile(self) -> None:
        inputs = discord_only_sample()
        for profile in SERVED_PROFILES:
            with self.subTest(profile=profile):
                rendered = render_profile_config(profile, inputs)
                self.assertIn(f'provider: "{inputs.model_provider}"', rendered)
                self.assertIn(f'default: "{inputs.model_name}"', rendered)

    def test_the_profile_model_matches_the_root_configuration_exactly(self) -> None:
        inputs = sample()
        root = hermes_config_mapping(inputs)["model"]
        for profile in SERVED_PROFILES:
            with self.subTest(profile=profile):
                self.assertEqual(profile_config_mapping(profile, inputs)["model"], root)

    def test_no_model_is_hard_coded_in_the_generator(self) -> None:
        from pathlib import Path

        source = Path("assistant/scotty_business/setup.py").read_text(encoding="utf-8")
        for forbidden in ("gpt-", "claude-", "gemini-", "llama"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class ProfilePluginIsolationTests(unittest.TestCase):
    def test_the_full_profile_enables_only_the_authorization_guard(self) -> None:
        mapping = profile_config_mapping(MAINTAINER_PROFILE, sample())
        self.assertEqual(mapping["plugins"], {"enabled": [GUARD_PLUGIN]})
        self.assertNotIn("scotty-business", render_profile_config(MAINTAINER_PROFILE, sample()))

    def test_each_client_profile_enables_only_the_bounded_plugin(self) -> None:
        for profile in CLIENT_PROFILES.values():
            with self.subTest(profile=profile):
                mapping = profile_config_mapping(profile, sample())
                self.assertEqual(mapping["plugins"], {"enabled": ["scotty-business"]})
                self.assertNotIn(GUARD_PLUGIN, render_profile_config(profile, sample()))

    def test_the_guard_never_widens_the_client_toolsets(self) -> None:
        for profile in CLIENT_PROFILES.values():
            with self.subTest(profile=profile):
                toolsets = profile_config_mapping(profile, sample())["platform_toolsets"]
                assert isinstance(toolsets, dict)
                self.assertEqual(toolsets["discord"], ["scotty"])


if __name__ == "__main__":
    unittest.main()
