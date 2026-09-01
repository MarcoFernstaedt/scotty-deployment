from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import synthetic
from synthetic import (
    CLIENT_GUILD,
    EMPLOYEE_CHANNEL,
    EMPLOYEE_USER,
    OPERATOR_CHANNEL,
    OPERATOR_USER,
    ROUTE_CHANNEL,
    ROUTE_GUILD,
    ROUTE_USER,
    source,
)

from assistant.scotty_business.config import ConfigError, RuntimeConfig
from assistant.scotty_business.ingress import EMPLOYEE_SUMMARY_COMMAND, IngressGuard
from assistant.scotty_business.policy import (
    FIXED_WIZARD_COMMAND,
    SETUP_WIZARD,
    Role,
    employee_summary,
)
from assistant.scotty_business.routing import (
    ALL_TOOLSETS,
    CLIENT_PROFILES,
    CLIENT_TOOLSETS,
    MAINTAINER_PROFILE,
    RouteKind,
    resolve_route,
    source_fields,
)


class PrivateRouteConfigTests(unittest.TestCase):
    def test_the_route_must_not_share_the_client_guild(self) -> None:
        with self.assertRaises(ConfigError):
            RuntimeConfig.from_mapping(
                synthetic.private_mapping(
                    maintainer_route={
                        "guild_id": CLIENT_GUILD,
                        "channel_id": ROUTE_CHANNEL,
                        "user_id": ROUTE_USER,
                        "profile": MAINTAINER_PROFILE,
                    }
                )
            )

    def test_the_route_must_not_reuse_a_client_channel_or_user(self) -> None:
        for field, value in (("channel_id", OPERATOR_CHANNEL), ("user_id", EMPLOYEE_USER)):
            with self.subTest(field=field):
                route = {
                    "guild_id": ROUTE_GUILD,
                    "channel_id": ROUTE_CHANNEL,
                    "user_id": ROUTE_USER,
                    "profile": MAINTAINER_PROFILE,
                }
                route[field] = value
                with self.assertRaises(ConfigError):
                    RuntimeConfig.from_mapping(synthetic.private_mapping(maintainer_route=route))

    def test_the_route_profile_must_be_a_bounded_slug(self) -> None:
        with self.assertRaises(ConfigError):
            RuntimeConfig.from_mapping(
                synthetic.private_mapping(
                    maintainer_route={
                        "guild_id": ROUTE_GUILD,
                        "channel_id": ROUTE_CHANNEL,
                        "user_id": ROUTE_USER,
                        "profile": "operations full!",
                    }
                )
            )

    def test_the_route_channel_is_never_a_client_discord_destination(self) -> None:
        config = synthetic.config()
        self.assertNotIn(ROUTE_CHANNEL, config.client_discord_destinations())


class RouteResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = synthetic.config()

    def test_client_sources_resolve_to_bounded_per_role_profiles(self) -> None:
        cases = {
            (OPERATOR_CHANNEL, OPERATOR_USER): Role.MAIN_OPERATOR,
            (EMPLOYEE_CHANNEL, EMPLOYEE_USER): Role.EMPLOYEE,
        }
        seen: set[str] = set()
        for (channel, user), role in cases.items():
            route = resolve_route(self.config, source(channel=channel, user=user))
            assert route is not None
            self.assertEqual(route.kind, RouteKind.CLIENT)
            self.assertEqual(route.toolsets, CLIENT_TOOLSETS)
            assert route.principal is not None
            self.assertEqual(route.principal.role, role)
            self.assertEqual(route.profile, CLIENT_PROFILES[role])
            seen.add(route.profile)
        self.assertEqual(len(seen), 2, "each client role keeps a profile-local surface")

    def test_the_exact_private_tuple_resolves_to_the_full_profile(self) -> None:
        route = resolve_route(
            self.config, source(guild=ROUTE_GUILD, channel=ROUTE_CHANNEL, user=ROUTE_USER)
        )
        assert route is not None
        self.assertEqual(route.kind, RouteKind.MAINTAINER)
        self.assertEqual(route.profile, MAINTAINER_PROFILE)
        self.assertEqual(route.toolsets, ALL_TOOLSETS)
        self.assertIsNone(route.principal)

    def test_a_wrong_user_in_the_private_channel_is_rejected(self) -> None:
        for wrong_user in (OPERATOR_USER, EMPLOYEE_USER, "999999999999999999"):
            with self.subTest(user=wrong_user):
                self.assertIsNone(
                    resolve_route(
                        self.config,
                        source(guild=ROUTE_GUILD, channel=ROUTE_CHANNEL, user=wrong_user),
                    )
                )

    def test_every_wrong_cross_product_is_rejected(self) -> None:
        cases = [
            source(guild=CLIENT_GUILD, channel=ROUTE_CHANNEL, user=ROUTE_USER),
            source(guild=ROUTE_GUILD, channel=OPERATOR_CHANNEL, user=ROUTE_USER),
            source(guild=ROUTE_GUILD, channel=ROUTE_CHANNEL, user=OPERATOR_USER),
            source(guild=ROUTE_GUILD, channel=ROUTE_CHANNEL, user=ROUTE_USER, is_bot=True),
            source(guild=ROUTE_GUILD, channel=ROUTE_CHANNEL, user=ROUTE_USER, platform="telegram"),
            source(guild=ROUTE_GUILD, channel="900", user=ROUTE_USER, parent=OPERATOR_CHANNEL),
            source(guild=CLIENT_GUILD, channel="900", user=ROUTE_USER, parent=ROUTE_CHANNEL),
            source(channel=OPERATOR_CHANNEL, user=EMPLOYEE_USER),
            source(channel=EMPLOYEE_CHANNEL, user=OPERATOR_USER),
            source(guild="999000000000000001"),
            source(channel="900", parent=EMPLOYEE_CHANNEL),
        ]
        for candidate in cases:
            with self.subTest(source=candidate):
                self.assertIsNone(resolve_route(self.config, candidate))

    def test_a_private_thread_is_accepted_only_under_its_configured_parent(self) -> None:
        route = resolve_route(
            self.config,
            source(guild=ROUTE_GUILD, channel="900", user=ROUTE_USER, parent=ROUTE_CHANNEL),
        )
        assert route is not None
        self.assertEqual(route.kind, RouteKind.MAINTAINER)

    def test_mismatched_scope_and_guild_ids_fail_closed(self) -> None:
        broken = source(guild=ROUTE_GUILD, channel=ROUTE_CHANNEL, user=ROUTE_USER)
        broken.scope_id = CLIENT_GUILD
        self.assertIsNone(resolve_route(self.config, broken))

    def test_source_fields_reads_only_immutable_gateway_provenance(self) -> None:
        self.assertEqual(
            source_fields(source()),
            {
                "platform": "discord",
                "guild_id": CLIENT_GUILD,
                "channel_id": OPERATOR_CHANNEL,
                "user_id": OPERATOR_USER,
                "parent_channel_id": None,
                "is_bot": False,
            },
        )


class IngressRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.outbound: list[tuple[str, str]] = []
        self.guard = IngressGuard(
            synthetic.config(), lambda channel, text: self.outbound.append((channel, text))
        )

    def test_the_exact_private_tuple_reaches_the_full_profile(self) -> None:
        result = self.guard(
            synthetic.event(
                guild=ROUTE_GUILD,
                channel=ROUTE_CHANNEL,
                user=ROUTE_USER,
                text="Show the redacted approval receipts",
            )
        )
        self.assertEqual(result, {"action": "allow"})
        self.assertEqual(self.outbound, [])

    def test_a_wrong_user_in_the_private_channel_is_silently_rejected(self) -> None:
        for wrong_user in (OPERATOR_USER, EMPLOYEE_USER):
            with self.subTest(user=wrong_user):
                self.assertEqual(
                    self.guard(
                        synthetic.event(
                            guild=ROUTE_GUILD, channel=ROUTE_CHANNEL, user=wrong_user, text="hello"
                        )
                    ),
                    {"action": "skip", "reason": "unauthorized"},
                )
        self.assertEqual(self.outbound, [], "no reply ever discloses the private route")

    def test_client_fixed_paths_never_target_the_private_route(self) -> None:
        self.guard(synthetic.event(text=EMPLOYEE_SUMMARY_COMMAND))
        self.guard(synthetic.event(text="Please write code for an integration"))
        destinations = {channel for channel, _ in self.outbound}
        self.assertTrue(destinations)
        self.assertNotIn(ROUTE_CHANNEL, destinations)

    def test_the_private_route_is_not_narrowed_by_the_client_coding_refusal(self) -> None:
        result = self.guard(
            synthetic.event(
                guild=ROUTE_GUILD,
                channel=ROUTE_CHANNEL,
                user=ROUTE_USER,
                text="Please write code for an integration",
            )
        )
        self.assertEqual(result, {"action": "allow"})
        self.assertEqual(self.outbound, [])

    def test_credentials_are_still_withheld_from_the_model_on_every_route(self) -> None:
        result = self.guard(
            synthetic.event(
                guild=ROUTE_GUILD,
                channel=ROUTE_CHANNEL,
                user=ROUTE_USER,
                text="token is " + "ghp_" + ("a" * 28),
            )
        )
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.outbound, [])

    def test_only_the_exact_maintainer_tuple_triggers_the_fixed_wizard(self) -> None:
        result = self.guard(
            synthetic.event(
                guild=ROUTE_GUILD,
                channel=ROUTE_CHANNEL,
                user=ROUTE_USER,
                text=FIXED_WIZARD_COMMAND,
            )
        )
        self.assertEqual(result, {"action": "skip", "reason": "fixed-wizard"})
        self.assertEqual(self.outbound, [(OPERATOR_CHANNEL, SETUP_WIZARD)])

    def test_the_wizard_destination_is_chosen_by_code_not_by_the_model(self) -> None:
        self.guard(
            synthetic.event(
                guild=ROUTE_GUILD,
                channel=ROUTE_CHANNEL,
                user=ROUTE_USER,
                text=FIXED_WIZARD_COMMAND,
            )
        )
        destinations = {channel for channel, _ in self.outbound}
        self.assertEqual(destinations, {OPERATOR_CHANNEL})
        self.assertNotIn(ROUTE_CHANNEL, destinations)
        self.assertNotIn(EMPLOYEE_CHANNEL, destinations)

    def test_client_principals_get_no_wizard_and_no_disclosure(self) -> None:
        for channel, user in (
            (OPERATOR_CHANNEL, OPERATOR_USER),
            (EMPLOYEE_CHANNEL, EMPLOYEE_USER),
        ):
            with self.subTest(channel=channel):
                result = self.guard(
                    synthetic.event(channel=channel, user=user, text=FIXED_WIZARD_COMMAND)
                )
                self.assertEqual(result, {"action": "skip", "reason": "fixed-wizard"})
        self.assertEqual(self.outbound, [], "no wizard and no reply for a wrong principal")

    def test_mixed_tuples_get_no_wizard(self) -> None:
        for candidate in (
            synthetic.event(
                channel=OPERATOR_CHANNEL, user=EMPLOYEE_USER, text=FIXED_WIZARD_COMMAND
            ),
            synthetic.event(
                guild=ROUTE_GUILD,
                channel=ROUTE_CHANNEL,
                user=OPERATOR_USER,
                text=FIXED_WIZARD_COMMAND,
            ),
            synthetic.event(
                guild=CLIENT_GUILD,
                channel=ROUTE_CHANNEL,
                user=ROUTE_USER,
                text=FIXED_WIZARD_COMMAND,
            ),
        ):
            with self.subTest(source=candidate.source):
                self.assertEqual(self.guard(candidate)["action"], "skip")
        self.assertEqual(self.outbound, [])

    def test_the_wizard_is_never_sent_without_the_exact_trigger(self) -> None:
        for text in (
            "scotty, send trent the setup wizard.",
            "Scotty, send Trent the setup wizard",
            "send Trent the wizard please",
        ):
            with self.subTest(text=text):
                self.guard(
                    synthetic.event(
                        guild=ROUTE_GUILD, channel=ROUTE_CHANNEL, user=ROUTE_USER, text=text
                    )
                )
        self.assertEqual(self.outbound, [])

    def test_the_employee_summary_still_works_alongside_the_wizard(self) -> None:
        self.guard(synthetic.event(text=EMPLOYEE_SUMMARY_COMMAND))
        self.assertEqual(self.outbound, [(EMPLOYEE_CHANNEL, employee_summary("Assistant"))])


class WizardSingleDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="scotty-wizard-test-")
        self.outbound: list[tuple[str, str]] = []
        self.guard = IngressGuard(
            synthetic.config(),
            lambda channel, text: self.outbound.append((channel, text)),
            Path(self.tempdir.name),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def trigger(self, message_id: str) -> object:
        candidate = synthetic.event(
            guild=ROUTE_GUILD,
            channel=ROUTE_CHANNEL,
            user=ROUTE_USER,
            text=FIXED_WIZARD_COMMAND,
        )
        candidate.message_id = message_id
        return candidate

    def test_two_hooks_on_one_message_deliver_exactly_once(self) -> None:
        candidate = self.trigger("800000000000000001")
        self.guard(candidate)
        self.guard(candidate)
        self.assertEqual(self.outbound, [(OPERATOR_CHANNEL, SETUP_WIZARD)])

    def test_an_explicit_repeat_delivers_again(self) -> None:
        self.guard(self.trigger("800000000000000001"))
        self.guard(self.trigger("800000000000000002"))
        self.assertEqual(len(self.outbound), 2)

    def test_nothing_is_delivered_without_a_trigger(self) -> None:
        self.guard(synthetic.event(guild=ROUTE_GUILD, channel=ROUTE_CHANNEL, user=ROUTE_USER))
        self.assertEqual(self.outbound, [])


if __name__ == "__main__":
    unittest.main()
