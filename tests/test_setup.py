from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from test_provisioning import EMPLOYEE_USER as FakeDiscordProvisioning_EMPLOYEE
from test_provisioning import GUILD as FakeDiscordProvisioning_GUILD
from test_provisioning import OPERATOR_USER as FakeDiscordProvisioning_OPERATOR
from test_provisioning import FakeDiscord

from assistant.scotty_business.config import RuntimeConfig
from assistant.scotty_business.provisioning import ChannelPlan
from assistant.scotty_business.setup import (
    SetupError,
    SetupInputs,
    channel_plans,
    collect_inputs,
    private_mapping,
    provision_private_channels,
    render_hermes_config,
    render_profile_routing_overlay,
    resolve_provisioned_channels,
    validate_discord_scope,
    write_private_state,
)

MAINT_GUILD = "110000000000000001"
MAINT_CHANNEL = "220000000000000001"
MAINT_USER = "320000000000000001"
PROVISION_GUILD = FakeDiscordProvisioning_GUILD
PROVISION_OPERATOR = FakeDiscordProvisioning_OPERATOR
PROVISION_EMPLOYEE = FakeDiscordProvisioning_EMPLOYEE


def sample(**overrides: object) -> SetupInputs:
    defaults: dict[str, object] = dict(
        model_provider="openrouter",
        model_name="synthetic/model",
        guild_id="100",
        maintainer_channel_id="200",
        maintainer_user_id="300",
        operator_channel_id="201",
        operator_user_id="301",
        employee_channel_id="202",
        employee_user_id="302",
        announcement_channel_ids=("210",),
        trello_board_id="board-1",
        trello_list_ids=("list-1", "list-2"),
        trello_label_ids=("label-1",),
        trello_custom_field_ids=("field-1",),
        ghl_location_id="location-1",
        secrets={
            "OPENROUTER_API_KEY": "model-secret",
            "DISCORD_BOT_TOKEN": "discord-secret",
            "SCOTTY_TRELLO_API_KEY": "trello-key-secret",
            "SCOTTY_TRELLO_TOKEN": "trello-token-secret",
            "SCOTTY_GHL_PRIVATE_TOKEN": "ghl-secret",
            "SCOTTY_RENTCAST_API_KEY": "rentcast-secret",
        },
    )
    defaults.update(overrides)
    return SetupInputs(**defaults)  # type: ignore[arg-type]


def maintainer_sample() -> SetupInputs:
    return sample(
        maintainer_route_guild_id=MAINT_GUILD,
        maintainer_route_channel_id=MAINT_CHANNEL,
        maintainer_route_user_id=MAINT_USER,
        maintainer_route_profile="operations-full",
    )


class FakeDiscordSetupClient:
    def __init__(self, *, private: bool = True, member: bool = True) -> None:
        self.private = private
        self.member = member
        self.calls: list[str] = []

    def get(self, path: str):
        self.calls.append(path)
        if path == "/users/@me":
            return {"id": "900"}
        if path == "/guilds/100/members/@me":
            if not self.member:
                raise SetupError("bot membership unavailable")
            return {"user": {"id": "900"}, "roles": ["bot-role"]}
        if path.startswith("/channels/"):
            channel_id = path.rsplit("/", 1)[-1]
            overwrites = (
                [{"id": "100", "type": 0, "deny": "1024", "allow": "0"}] if self.private else []
            )
            return {
                "id": channel_id,
                "guild_id": "100",
                "type": 0,
                "permission_overwrites": overwrites,
            }
        raise AssertionError(path)


class SetupTests(unittest.TestCase):
    def sample(self, **overrides: object) -> SetupInputs:
        return sample(**overrides)

    def test_all_credentials_are_collected_only_through_hidden_input(self) -> None:
        visible_answers = iter(
            [
                "openrouter",
                "synthetic/model",
                "100",
                "200",
                "300",
                "no",
                "201",
                "202",
                "301",
                "302",
                "210",
                "board-1",
                "list-1,list-2",
                "label-1",
                "field-1",
                "location-1",
                "no",
            ]
        )
        hidden_prompts: list[str] = []

        def hidden(prompt: str) -> str:
            hidden_prompts.append(prompt)
            return f"secret-{len(hidden_prompts)}"

        result = collect_inputs(input_fn=lambda prompt: next(visible_answers), hidden_fn=hidden)
        self.assertEqual(len(hidden_prompts), 6)
        self.assertEqual(len(result.secrets), 6)
        self.assertTrue(all(value.startswith("secret-") for value in result.secrets.values()))
        self.assertIsNone(result.provision_channel_names)
        self.assertEqual(result.route_fields(), ("", "", "", ""))

    def test_provisioning_and_route_answers_never_travel_through_hidden_input(self) -> None:
        visible_answers = iter(
            [
                "openrouter",
                "synthetic/model",
                "100",
                "200",
                "300",
                "yes",
                "scotty-operator",
                "scotty-employee",
                "301",
                "302",
                "210",
                "board-1",
                "list-1",
                "",
                "",
                "location-1",
                "yes",
                MAINT_GUILD,
                MAINT_CHANNEL,
                MAINT_USER,
                "operations-full",
            ]
        )
        hidden_prompts: list[str] = []

        def hidden(prompt: str) -> str:
            hidden_prompts.append(prompt)
            return f"secret-{len(hidden_prompts)}"

        result = collect_inputs(input_fn=lambda prompt: next(visible_answers), hidden_fn=hidden)
        self.assertEqual(len(hidden_prompts), 6)
        self.assertEqual(
            result.provision_channel_names,
            {"main_operator": "scotty-operator", "employee": "scotty-employee"},
        )
        self.assertEqual(result.operator_channel_id, "")
        self.assertEqual(result.employee_channel_id, "")
        self.assertEqual(
            result.route_fields(),
            (MAINT_GUILD, MAINT_CHANNEL, MAINT_USER, "operations-full"),
        )

    def test_discord_validation_requires_bot_membership_exact_guild_and_private_channels(
        self,
    ) -> None:
        client = FakeDiscordSetupClient()
        validate_discord_scope(self.sample(), client)
        self.assertIn("/guilds/100/members/@me", client.calls)
        self.assertIn("/channels/200", client.calls)
        self.assertIn("/channels/202", client.calls)
        with self.assertRaises(SetupError):
            validate_discord_scope(self.sample(), FakeDiscordSetupClient(private=False))
        with self.assertRaises(SetupError):
            validate_discord_scope(self.sample(), FakeDiscordSetupClient(member=False))

    def test_generated_config_has_only_scotty_model_toolset_and_disables_slash_commands(
        self,
    ) -> None:
        rendered = render_hermes_config(self.sample())
        self.assertIn("discord: [scotty]", rendered)
        self.assertIn("tool_search:\n    enabled: off", rendered)
        self.assertIn("slash_commands: false", rendered)
        self.assertIn("auto_thread: false", rendered)
        self.assertNotIn("terminal", rendered)
        self.assertNotIn("browser", rendered)
        self.assertNotIn("model-secret", rendered)

    def test_private_state_is_atomic_owner_only_and_secrets_stay_in_env(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-setup-test-") as directory:
            root = Path(directory)
            write_private_state(self.sample(), root, owner_uid=os.getuid(), owner_gid=os.getgid())
            private_path = root / "scotty" / "private.json"
            env_path = root / ".env"
            config_path = root / "config.yaml"
            self.assertEqual(private_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            private = json.loads(private_path.read_text(encoding="utf-8"))
            self.assertEqual(private["addons"], ["discord", "trello", "ghl", "rentcast"])
            self.assertNotIn("secret", private_path.read_text(encoding="utf-8"))
            self.assertIn(
                "SCOTTY_GHL_PRIVATE_TOKEN=ghl-secret", env_path.read_text(encoding="utf-8")
            )
            self.assertNotIn("ghl-secret", config_path.read_text(encoding="utf-8"))
            self.assertEqual(list(root.rglob("*.tmp")), [])

    def test_symlinked_private_target_is_rejected_without_following(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-setup-test-") as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.write_text("unchanged", encoding="utf-8")
            (root / "scotty").mkdir()
            (root / "scotty" / "private.json").symlink_to(outside)
            with self.assertRaises(SetupError):
                write_private_state(
                    self.sample(), root, owner_uid=os.getuid(), owner_gid=os.getgid()
                )
            self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged")


class MaintainerRouteSetupTests(unittest.TestCase):
    def test_private_mapping_records_the_route_and_config_accepts_it(self) -> None:
        mapping = private_mapping(maintainer_sample())
        self.assertEqual(
            mapping["maintainer_route"],
            {
                "guild_id": MAINT_GUILD,
                "channel_id": MAINT_CHANNEL,
                "user_id": MAINT_USER,
                "profile": "operations-full",
            },
        )
        config = RuntimeConfig.from_mapping(mapping)
        assert config.maintainer_route is not None
        self.assertEqual(config.maintainer_route.profile, "operations-full")

    def test_a_deployment_without_a_route_records_none(self) -> None:
        mapping = private_mapping(sample())
        self.assertNotIn("maintainer_route", mapping)
        self.assertIsNone(RuntimeConfig.from_mapping(mapping).maintainer_route)

    def test_partial_route_input_fails_closed(self) -> None:
        with self.assertRaises(SetupError):
            private_mapping(sample(maintainer_route_guild_id=MAINT_GUILD))

    def test_gateway_config_admits_the_route_channel_without_widening_client_toolsets(
        self,
    ) -> None:
        rendered = render_hermes_config(maintainer_sample())
        self.assertIn(MAINT_CHANNEL, rendered)
        self.assertIn("discord: [scotty]", rendered)
        self.assertNotIn("model-secret", rendered)

    def test_native_profile_routing_is_an_unmerged_reviewed_overlay(self) -> None:
        overlay = render_profile_routing_overlay(maintainer_sample())
        self.assertIn("operations-full", overlay)
        self.assertIn(MAINT_CHANNEL, overlay)
        self.assertIn("scotty-main-operator", overlay)
        self.assertIn("verify", overlay.lower())
        self.assertNotIn(overlay, render_hermes_config(maintainer_sample()))

    def test_no_overlay_is_rendered_without_a_configured_route(self) -> None:
        self.assertEqual(render_profile_routing_overlay(sample()), "")


class ProvisioningSetupTests(unittest.TestCase):
    def test_channel_plans_bind_each_name_to_the_exact_guild_and_user(self) -> None:
        inputs = sample(
            operator_channel_id="",
            employee_channel_id="",
            provision_channel_names={
                "main_operator": "scotty-operator",
                "employee": "scotty-employee",
            },
        )
        plans = channel_plans(inputs)
        self.assertEqual(
            plans,
            (
                ChannelPlan(
                    key="main_operator",
                    name="scotty-operator",
                    guild_id="100",
                    user_id="301",
                ),
                ChannelPlan(key="employee", name="scotty-employee", guild_id="100", user_id="302"),
            ),
        )

    def test_no_plan_is_produced_when_existing_channel_ids_were_supplied(self) -> None:
        self.assertEqual(channel_plans(sample()), ())

    def test_resolved_channel_ids_replace_the_placeholders_exactly_once(self) -> None:
        inputs = sample(
            operator_channel_id="",
            employee_channel_id="",
            provision_channel_names={
                "main_operator": "scotty-operator",
                "employee": "scotty-employee",
            },
        )
        resolved = resolve_provisioned_channels(
            inputs, {"main_operator": "500000000000000001", "employee": "500000000000000002"}
        )
        self.assertEqual(resolved.operator_channel_id, "500000000000000001")
        self.assertEqual(resolved.employee_channel_id, "500000000000000002")
        self.assertEqual(resolved.guild_id, inputs.guild_id)

    def test_an_incomplete_provisioning_result_never_reaches_private_state(self) -> None:
        inputs = sample(
            operator_channel_id="",
            employee_channel_id="",
            provision_channel_names={
                "main_operator": "scotty-operator",
                "employee": "scotty-employee",
            },
        )
        with self.assertRaises(SetupError):
            resolve_provisioned_channels(inputs, {"main_operator": "500000000000000001"})
        with self.assertRaises(SetupError):
            private_mapping(inputs)


class CredentialSourceTests(unittest.TestCase):
    def test_an_exported_bot_token_is_used_without_prompting_or_argv(self) -> None:
        visible_answers = iter(
            [
                "openrouter",
                "synthetic/model",
                "100",
                "200",
                "300",
                "no",
                "201",
                "202",
                "301",
                "302",
                "210",
                "board-1",
                "list-1",
                "",
                "",
                "location-1",
                "no",
            ]
        )
        hidden_prompts: list[str] = []

        def hidden(prompt: str) -> str:
            hidden_prompts.append(prompt)
            return f"secret-{len(hidden_prompts)}"

        result = collect_inputs(
            input_fn=lambda prompt: next(visible_answers),
            hidden_fn=hidden,
            environ={"DISCORD_BOT_TOKEN": "exported-token"},
        )
        self.assertEqual(result.secrets["DISCORD_BOT_TOKEN"], "exported-token")
        self.assertEqual(len(hidden_prompts), 5)
        self.assertTrue(all("Discord bot token" not in item for item in hidden_prompts))

    def test_setup_never_reads_command_line_arguments(self) -> None:
        source = Path("assistant/scotty_business/setup.py").read_text(encoding="utf-8")
        self.assertNotIn("sys.argv", source)
        self.assertNotIn("argparse", source)


class ProvisioningHandoffTests(unittest.TestCase):
    def test_a_completed_provisioning_run_binds_both_channel_ids(self) -> None:
        inputs = sample(
            guild_id=PROVISION_GUILD,
            operator_channel_id="",
            employee_channel_id="",
            operator_user_id=PROVISION_OPERATOR,
            employee_user_id=PROVISION_EMPLOYEE,
            provision_channel_names={
                "main_operator": "scotty-operator",
                "employee": "scotty-employee",
            },
        )
        client = FakeDiscord()
        resolved = provision_private_channels(
            inputs, token="unused", confirm=lambda _: True, client=client
        )
        self.assertTrue(resolved.operator_channel_id.isdigit())
        self.assertTrue(resolved.employee_channel_id.isdigit())
        self.assertNotEqual(resolved.operator_channel_id, resolved.employee_channel_id)

    def test_an_unknown_outcome_stops_setup_before_private_state(self) -> None:
        inputs = sample(
            guild_id=PROVISION_GUILD,
            operator_channel_id="",
            employee_channel_id="",
            operator_user_id=PROVISION_OPERATOR,
            employee_user_id=PROVISION_EMPLOYEE,
            provision_channel_names={
                "main_operator": "scotty-operator",
                "employee": "scotty-employee",
            },
        )
        client = FakeDiscord(post_body={"guild_id": PROVISION_GUILD, "type": 0})
        with self.assertRaises(SetupError) as caught:
            provision_private_channels(
                inputs, token="unused", confirm=lambda _: True, client=client
            )
        self.assertIn("unknown", str(caught.exception))

    def test_setup_without_provisioning_leaves_supplied_channel_ids_untouched(self) -> None:
        inputs = sample()
        self.assertIs(
            provision_private_channels(inputs, token="unused", confirm=lambda _: True), inputs
        )


if __name__ == "__main__":
    unittest.main()
