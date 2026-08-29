from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from test_provisioning import BOT as PROVISION_BOT
from test_provisioning import EMPLOYEE_USER as PROVISION_EMPLOYEE
from test_provisioning import GUILD as PROVISION_GUILD
from test_provisioning import OPERATOR_USER as PROVISION_OPERATOR
from test_provisioning import FakeDiscord

from assistant.scotty_business.config import RuntimeConfig
from assistant.scotty_business.provisioning import ChannelPlan, intended_overwrites
from assistant.scotty_business.routing import MAINTAINER_PROFILE, SERVED_PROFILES
from assistant.scotty_business.setup import (
    CODEX_AUTH_COMMAND,
    CODEX_PROVIDER,
    SetupError,
    SetupInputs,
    channel_plans,
    collect_inputs,
    ensure_profile_homes,
    next_steps,
    private_mapping,
    profile_home,
    provision_private_channels,
    render_hermes_config,
    resolve_provisioned_channels,
    validate_discord_scope,
    validate_maintainer_route,
    write_private_state,
)

CLIENT_GUILD = "100000000000000001"
OPERATOR_CHANNEL = "201000000000000001"
OPERATOR_USER = "301000000000000001"
EMPLOYEE_CHANNEL = "202000000000000001"
EMPLOYEE_USER = "302000000000000001"
MAINT_GUILD = "110000000000000001"
MAINT_CHANNEL = "220000000000000001"
MAINT_USER = "320000000000000001"
BOT_ID = "600000000000000001"

VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
READ_HISTORY = 1 << 16
BOT_ROUTE_ALLOW = VIEW_CHANNEL | SEND_MESSAGES | READ_HISTORY


def sample(**overrides: object) -> SetupInputs:
    defaults: dict[str, object] = dict(
        model_provider="openrouter",
        model_name="synthetic/model",
        guild_id=CLIENT_GUILD,
        operator_channel_id=OPERATOR_CHANNEL,
        operator_user_id=OPERATOR_USER,
        employee_channel_id=EMPLOYEE_CHANNEL,
        employee_user_id=EMPLOYEE_USER,
        route_guild_id=MAINT_GUILD,
        route_channel_id=MAINT_CHANNEL,
        route_user_id=MAINT_USER,
        announcement_channel_ids=("210000000000000001",),
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
    return sample()


def discord_only_sample(**overrides: object) -> SetupInputs:
    defaults: dict[str, object] = dict(
        model_provider=CODEX_PROVIDER,
        model_name="synthetic/codex",
        trello_board_id="",
        trello_list_ids=(),
        trello_label_ids=(),
        trello_custom_field_ids=(),
        ghl_location_id="",
        announcement_channel_ids=(),
        secrets={"DISCORD_BOT_TOKEN": "discord-secret"},
    )
    defaults.update(overrides)
    return sample(**defaults)


def route_channel(
    *,
    private: bool = True,
    user_allow: int = VIEW_CHANNEL,
    bot_allow: int = BOT_ROUTE_ALLOW,
    guild_id: str = MAINT_GUILD,
    channel_type: int = 0,
    extra: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    overwrites: list[dict[str, object]] = [
        {
            "id": MAINT_GUILD,
            "type": 0,
            "allow": "0",
            "deny": str(VIEW_CHANNEL if private else 0),
        },
        {"id": MAINT_USER, "type": 1, "allow": str(user_allow), "deny": "0"},
        {"id": BOT_ID, "type": 1, "allow": str(bot_allow), "deny": "0"},
    ]
    overwrites.extend(extra or [])
    return {
        "id": MAINT_CHANNEL,
        "guild_id": guild_id,
        "type": channel_type,
        "permission_overwrites": overwrites,
    }


class FakeDiscordSetupClient:
    """Synthetic Discord REST double for setup validation."""

    def __init__(
        self,
        *,
        private: bool = True,
        member: bool = True,
        route_member: bool = True,
        route: dict[str, object] | None = None,
        route_channel_missing: bool = False,
        client_in_route_guild: bool = False,
    ) -> None:
        self.private = private
        self.member = member
        self.route_member = route_member
        self.route = route if route is not None else route_channel()
        self.route_channel_missing = route_channel_missing
        self.client_in_route_guild = client_in_route_guild
        self.calls: list[str] = []

    def status_get(self, path: str) -> tuple[int, object]:
        self.calls.append(path)
        if path == "/users/@me":
            return 200, {"id": BOT_ID}
        if path == f"/guilds/{CLIENT_GUILD}/members/@me":
            return (200, {"user": {"id": BOT_ID}}) if self.member else (403, {})
        if path == f"/guilds/{MAINT_GUILD}/members/@me":
            return (200, {"user": {"id": BOT_ID}}) if self.route_member else (403, {})
        if path.startswith(f"/guilds/{MAINT_GUILD}/members/"):
            return (200, {"user": {"id": "x"}}) if self.client_in_route_guild else (404, {})
        if path == f"/channels/{MAINT_CHANNEL}":
            if self.route_channel_missing:
                return 404, {}
            return 200, self.route
        if path.startswith("/channels/"):
            channel_id = path.rsplit("/", 1)[-1]
            overwrites = (
                [{"id": CLIENT_GUILD, "type": 0, "deny": str(VIEW_CHANNEL), "allow": "0"}]
                if self.private
                else []
            )
            return 200, {
                "id": channel_id,
                "guild_id": CLIENT_GUILD,
                "type": 0,
                "permission_overwrites": overwrites,
            }
        raise AssertionError(path)

    def get(self, path: str) -> object:
        status, body = self.status_get(path)
        if status != 200:
            raise SetupError("Discord setup validation failed")
        return body


class SetupTests(unittest.TestCase):
    def sample(self, **overrides: object) -> SetupInputs:
        return sample(**overrides)

    def test_all_credentials_are_collected_only_through_hidden_input(self) -> None:
        visible_answers = iter(
            [
                "openrouter",
                "synthetic/model",
                CLIENT_GUILD,
                "no",
                OPERATOR_CHANNEL,
                EMPLOYEE_CHANNEL,
                OPERATOR_USER,
                EMPLOYEE_USER,
                "210000000000000001",
                MAINT_GUILD,
                MAINT_CHANNEL,
                MAINT_USER,
                "board-1",
                "list-1,list-2",
                "label-1",
                "field-1",
                "location-1",
            ]
        )
        hidden_prompts: list[str] = []

        def hidden(prompt: str) -> str:
            hidden_prompts.append(prompt)
            return f"secret-{len(hidden_prompts)}"

        result = collect_inputs(
            input_fn=lambda prompt: next(visible_answers), hidden_fn=hidden, environ={}
        )
        self.assertEqual(len(hidden_prompts), 6)
        self.assertEqual(len(result.secrets), 6)
        self.assertTrue(all(value.startswith("secret-") for value in result.secrets.values()))
        self.assertIsNone(result.provision_channel_names)

    def test_provisioning_and_route_answers_never_travel_through_hidden_input(self) -> None:
        visible_answers = iter(
            [
                "openrouter",
                "synthetic/model",
                CLIENT_GUILD,
                "yes",
                "scotty-operator",
                "scotty-employee",
                OPERATOR_USER,
                EMPLOYEE_USER,
                "",
                MAINT_GUILD,
                MAINT_CHANNEL,
                MAINT_USER,
                "",
                "",
            ]
        )
        hidden_prompts: list[str] = []

        def hidden(prompt: str) -> str:
            hidden_prompts.append(prompt)
            return f"secret-{len(hidden_prompts)}"

        result = collect_inputs(
            input_fn=lambda prompt: next(visible_answers), hidden_fn=hidden, environ={}
        )
        self.assertEqual(
            result.provision_channel_names,
            {"main_operator": "scotty-operator", "employee": "scotty-employee"},
        )
        self.assertEqual(result.operator_channel_id, "")
        self.assertEqual(
            (result.route_guild_id, result.route_channel_id, result.route_user_id),
            (MAINT_GUILD, MAINT_CHANNEL, MAINT_USER),
        )

    def test_discord_validation_requires_bot_membership_exact_guild_and_private_channels(
        self,
    ) -> None:
        client = FakeDiscordSetupClient()
        validate_discord_scope(self.sample(), client)
        self.assertIn(f"/guilds/{CLIENT_GUILD}/members/@me", client.calls)
        self.assertIn(f"/channels/{OPERATOR_CHANNEL}", client.calls)
        self.assertIn(f"/channels/{EMPLOYEE_CHANNEL}", client.calls)
        with self.assertRaises(SetupError):
            validate_discord_scope(self.sample(), FakeDiscordSetupClient(private=False))
        with self.assertRaises(SetupError):
            validate_discord_scope(self.sample(), FakeDiscordSetupClient(member=False))

    def test_generated_config_has_only_scotty_model_toolset_and_disables_slash_commands(
        self,
    ) -> None:
        rendered = render_hermes_config(self.sample())
        self.assertIn('discord: ["scotty"]', rendered)
        self.assertIn("tool_search:\n    enabled: false", rendered)
        self.assertIn("slash_commands: false", rendered)
        self.assertIn("auto_thread: false", rendered)
        self.assertNotIn("terminal", rendered)
        self.assertNotIn("browser", rendered)
        self.assertNotIn("model-secret", rendered)

    def test_private_state_is_atomic_owner_only_and_secrets_stay_in_env(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-setup-test-") as directory:
            root = Path(directory)
            stage_client_plugins(root)
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
            stage_client_plugins(root)
            outside = root / "outside"
            outside.write_text("unchanged", encoding="utf-8")
            (root / "scotty").mkdir()
            (root / "scotty" / "private.json").symlink_to(outside)
            with self.assertRaises(SetupError):
                write_private_state(
                    self.sample(), root, owner_uid=os.getuid(), owner_gid=os.getgid()
                )
            self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged")


def stage_client_plugins(root: Path) -> None:
    """Mimic the installer staging the bounded plugin in each client home."""

    for profile in SERVED_PROFILES:
        if profile == MAINTAINER_PROFILE:
            continue
        staged = root / "profiles" / profile / "plugins" / "scotty_business"
        staged.mkdir(parents=True, exist_ok=True)
        (staged / "plugin.yaml").write_text("name: scotty-business\n", encoding="utf-8")


class ProfileHomeTests(unittest.TestCase):
    def test_each_served_profile_gets_its_own_home_and_configuration(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-profiles-") as directory:
            root = Path(directory)
            stage_client_plugins(root)
            homes = ensure_profile_homes(root, owner_uid=os.getuid(), owner_gid=os.getgid())
            self.assertEqual(set(homes), set(SERVED_PROFILES))
            self.assertEqual(len({str(path) for path in homes.values()}), 3)
            for profile, home in homes.items():
                with self.subTest(profile=profile):
                    self.assertTrue((home / "config.yaml").is_file())
                    self.assertEqual((home / "config.yaml").stat().st_mode & 0o777, 0o600)
                    self.assertEqual(home.stat().st_mode & 0o777, 0o700)

    def test_profile_homes_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-profiles-") as directory:
            root = Path(directory)
            stage_client_plugins(root)
            first = ensure_profile_homes(root, owner_uid=os.getuid(), owner_gid=os.getgid())
            second = ensure_profile_homes(root, owner_uid=os.getuid(), owner_gid=os.getgid())
            self.assertEqual(first, second)

    def test_a_client_profile_without_its_staged_plugin_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-profiles-") as directory:
            root = Path(directory)
            with self.assertRaises(SetupError):
                ensure_profile_homes(root, owner_uid=os.getuid(), owner_gid=os.getgid())

    def test_the_full_profile_must_not_carry_the_bounded_plugin(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-profiles-") as directory:
            root = Path(directory)
            stage_client_plugins(root)
            leaked = profile_home(root, MAINTAINER_PROFILE) / "plugins" / "scotty_business"
            leaked.mkdir(parents=True)
            (leaked / "plugin.yaml").write_text("name: scotty-business\n", encoding="utf-8")
            with self.assertRaises(SetupError):
                ensure_profile_homes(root, owner_uid=os.getuid(), owner_gid=os.getgid())


class MaintainerRouteSetupTests(unittest.TestCase):
    def test_private_mapping_records_the_route_and_config_accepts_it(self) -> None:
        mapping = private_mapping(maintainer_sample())
        self.assertEqual(
            mapping["maintainer_route"],
            {
                "guild_id": MAINT_GUILD,
                "channel_id": MAINT_CHANNEL,
                "user_id": MAINT_USER,
                "profile": MAINTAINER_PROFILE,
            },
        )
        config = RuntimeConfig.from_mapping(mapping)
        self.assertEqual(config.maintainer_route.profile, MAINTAINER_PROFILE)
        self.assertEqual({p.role.value for p in config.principals}, {"main_operator", "employee"})

    def test_a_valid_route_is_read_back_from_discord(self) -> None:
        client = FakeDiscordSetupClient()
        validate_maintainer_route(sample(), client)
        self.assertIn(f"/guilds/{MAINT_GUILD}/members/@me", client.calls)
        self.assertIn(f"/channels/{MAINT_CHANNEL}", client.calls)
        self.assertIn(f"/guilds/{MAINT_GUILD}/members/{OPERATOR_USER}", client.calls)
        self.assertIn(f"/guilds/{MAINT_GUILD}/members/{EMPLOYEE_USER}", client.calls)

    def test_a_nonexistent_route_channel_is_rejected(self) -> None:
        with self.assertRaises(SetupError):
            validate_maintainer_route(sample(), FakeDiscordSetupClient(route_channel_missing=True))

    def test_a_public_route_channel_is_rejected(self) -> None:
        client = FakeDiscordSetupClient(route=route_channel(private=False))
        with self.assertRaises(SetupError):
            validate_maintainer_route(sample(), client)

    def test_a_cross_guild_route_channel_is_rejected(self) -> None:
        client = FakeDiscordSetupClient(route=route_channel(guild_id=CLIENT_GUILD))
        with self.assertRaises(SetupError):
            validate_maintainer_route(sample(), client)

    def test_a_route_in_the_client_guild_is_rejected(self) -> None:
        with self.assertRaises(SetupError):
            validate_maintainer_route(sample(route_guild_id=CLIENT_GUILD), FakeDiscordSetupClient())

    def test_a_route_the_bot_cannot_reach_is_rejected(self) -> None:
        with self.assertRaises(SetupError):
            validate_maintainer_route(sample(), FakeDiscordSetupClient(route_member=False))

    def test_a_route_the_configured_user_cannot_view_is_rejected(self) -> None:
        client = FakeDiscordSetupClient(route=route_channel(user_allow=0))
        with self.assertRaises(SetupError):
            validate_maintainer_route(sample(), client)

    def test_a_route_where_the_bot_cannot_send_or_read_history_is_rejected(self) -> None:
        for allow in (VIEW_CHANNEL, VIEW_CHANNEL | SEND_MESSAGES):
            with self.subTest(allow=allow):
                client = FakeDiscordSetupClient(route=route_channel(bot_allow=allow))
                with self.assertRaises(SetupError):
                    validate_maintainer_route(sample(), client)

    def test_a_route_a_client_principal_can_view_is_rejected(self) -> None:
        leaked = route_channel(
            extra=[{"id": OPERATOR_USER, "type": 1, "allow": str(VIEW_CHANNEL), "deny": "0"}]
        )
        with self.assertRaises(SetupError):
            validate_maintainer_route(sample(), FakeDiscordSetupClient(route=leaked))

    def test_a_client_principal_inside_the_route_guild_is_rejected(self) -> None:
        with self.assertRaises(SetupError):
            validate_maintainer_route(sample(), FakeDiscordSetupClient(client_in_route_guild=True))

    def test_a_permission_drifted_route_is_rejected(self) -> None:
        drifted = route_channel()
        overwrites = list(drifted["permission_overwrites"])  # type: ignore[arg-type]
        overwrites[2] = {**overwrites[2], "deny": str(SEND_MESSAGES)}
        drifted["permission_overwrites"] = overwrites
        with self.assertRaises(SetupError):
            validate_maintainer_route(sample(), FakeDiscordSetupClient(route=drifted))

    def test_route_failures_never_name_the_private_identifiers(self) -> None:
        clients = [
            FakeDiscordSetupClient(route_channel_missing=True),
            FakeDiscordSetupClient(route=route_channel(private=False)),
            FakeDiscordSetupClient(client_in_route_guild=True),
        ]
        for client in clients:
            with self.subTest(client=client):
                try:
                    validate_maintainer_route(sample(), client)
                except SetupError as exc:
                    for identifier in (MAINT_GUILD, MAINT_CHANNEL, MAINT_USER):
                        self.assertNotIn(identifier, str(exc))


class CodexAndOptionalProviderTests(unittest.TestCase):
    def test_codex_setup_requires_no_model_api_key(self) -> None:
        visible_answers = iter(
            [
                CODEX_PROVIDER,
                "synthetic/codex",
                CLIENT_GUILD,
                "no",
                OPERATOR_CHANNEL,
                EMPLOYEE_CHANNEL,
                OPERATOR_USER,
                EMPLOYEE_USER,
                "",
                MAINT_GUILD,
                MAINT_CHANNEL,
                MAINT_USER,
                "",
                "",
            ]
        )
        hidden_prompts: list[str] = []

        def hidden(prompt: str) -> str:
            hidden_prompts.append(prompt)
            return "discord-secret" if "Discord" in prompt else ""

        result = collect_inputs(
            input_fn=lambda prompt: next(visible_answers), hidden_fn=hidden, environ={}
        )
        self.assertEqual(result.model_provider, CODEX_PROVIDER)
        self.assertEqual(set(result.secrets), {"DISCORD_BOT_TOKEN"})
        self.assertTrue(all("Model API credential" not in item for item in hidden_prompts))

    def test_codex_next_steps_name_the_native_auth_command_and_no_token(self) -> None:
        steps = "\n".join(next_steps(discord_only_sample()))
        self.assertIn(CODEX_AUTH_COMMAND, steps)
        self.assertIn("never handles, stores, or logs", steps)
        self.assertNotIn("OPENAI", steps)

    def test_no_oauth_material_is_ever_written_to_the_environment_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-codex-") as directory:
            root = Path(directory)
            stage_client_plugins(root)
            write_private_state(
                discord_only_sample(), root, owner_uid=os.getuid(), owner_gid=os.getgid()
            )
            env = (root / ".env").read_text(encoding="utf-8")
            self.assertEqual(env.strip(), "DISCORD_BOT_TOKEN=discord-secret")
            for forbidden in ("OPENAI", "CODEX", "oauth", "refresh_token"):
                self.assertNotIn(forbidden, env)

    def test_a_discord_only_deployment_omits_every_unconfigured_provider(self) -> None:
        mapping = private_mapping(discord_only_sample())
        for absent in ("trello", "ghl", "rentcast"):
            self.assertNotIn(absent, mapping)
        config = RuntimeConfig.from_mapping(mapping)
        self.assertIsNone(config.trello)
        self.assertIsNone(config.ghl_location_id)
        self.assertEqual(config.rentcast_endpoints, ())

    def test_no_placeholder_is_ever_recorded_as_a_connected_provider(self) -> None:
        mapping = private_mapping(discord_only_sample())
        rendered = json.dumps(mapping)
        for placeholder in ("changeme", "TODO", "placeholder", "example"):
            self.assertNotIn(placeholder, rendered)

    def test_next_steps_name_every_provider_left_unconnected(self) -> None:
        steps = "\n".join(next_steps(discord_only_sample()))
        for provider in ("Trello", "GoHighLevel", "RentCast"):
            self.assertIn(provider, steps)


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
        self.assertEqual(
            channel_plans(inputs),
            (
                ChannelPlan(
                    key="main_operator",
                    name="scotty-operator",
                    guild_id=CLIENT_GUILD,
                    user_id=OPERATOR_USER,
                ),
                ChannelPlan(
                    key="employee",
                    name="scotty-employee",
                    guild_id=CLIENT_GUILD,
                    user_id=EMPLOYEE_USER,
                ),
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
                CODEX_PROVIDER,
                "synthetic/codex",
                CLIENT_GUILD,
                "no",
                OPERATOR_CHANNEL,
                EMPLOYEE_CHANNEL,
                OPERATOR_USER,
                EMPLOYEE_USER,
                "",
                MAINT_GUILD,
                MAINT_CHANNEL,
                MAINT_USER,
                "",
                "",
            ]
        )
        hidden_prompts: list[str] = []

        def hidden(prompt: str) -> str:
            hidden_prompts.append(prompt)
            return ""

        result = collect_inputs(
            input_fn=lambda prompt: next(visible_answers),
            hidden_fn=hidden,
            environ={"DISCORD_BOT_TOKEN": "exported-token"},
        )
        self.assertEqual(result.secrets["DISCORD_BOT_TOKEN"], "exported-token")
        self.assertTrue(all("Discord bot token" not in item for item in hidden_prompts))

    def test_setup_never_reads_command_line_arguments(self) -> None:
        source = Path("assistant/scotty_business/setup.py").read_text(encoding="utf-8")
        self.assertNotIn("sys.argv", source)
        self.assertNotIn("argparse", source)


class ProvisioningHandoffTests(unittest.TestCase):
    def inputs(self) -> SetupInputs:
        return sample(
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

    def test_a_completed_provisioning_run_binds_both_channel_ids(self) -> None:
        client = FakeDiscord(bot_id=PROVISION_BOT)
        resolved = provision_private_channels(
            self.inputs(), token="unused", confirm=lambda _: True, client=client
        )
        self.assertTrue(resolved.operator_channel_id.isdigit())
        self.assertTrue(resolved.employee_channel_id.isdigit())
        self.assertNotEqual(resolved.operator_channel_id, resolved.employee_channel_id)

    def test_an_unknown_outcome_stops_setup_before_private_state(self) -> None:
        client = FakeDiscord(post_body={"guild_id": PROVISION_GUILD, "type": 0})
        with self.assertRaises(SetupError) as caught:
            provision_private_channels(
                self.inputs(), token="unused", confirm=lambda _: True, client=client
            )
        self.assertIn("unknown", str(caught.exception))

    def test_setup_without_provisioning_leaves_supplied_channel_ids_untouched(self) -> None:
        inputs = sample()
        self.assertIs(
            provision_private_channels(inputs, token="unused", confirm=lambda _: True), inputs
        )

    def test_provisioned_overwrites_never_grant_administrator(self) -> None:
        for overwrite in intended_overwrites(CLIENT_GUILD, OPERATOR_USER, BOT_ID):
            self.assertEqual(int(str(overwrite["allow"])) & (1 << 3), 0)


if __name__ == "__main__":
    unittest.main()
