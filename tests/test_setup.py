from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from assistant.scotty_business.setup import (
    SetupError,
    SetupInputs,
    collect_inputs,
    render_hermes_config,
    validate_discord_scope,
    write_private_state,
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
    def sample(self) -> SetupInputs:
        return SetupInputs(
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

    def test_all_credentials_are_collected_only_through_hidden_input(self) -> None:
        visible_answers = iter(
            [
                "openrouter",
                "synthetic/model",
                "100",
                "200",
                "300",
                "201",
                "301",
                "202",
                "302",
                "210",
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

        result = collect_inputs(input_fn=lambda prompt: next(visible_answers), hidden_fn=hidden)
        self.assertEqual(len(hidden_prompts), 6)
        self.assertEqual(len(result.secrets), 6)
        self.assertTrue(all(value.startswith("secret-") for value in result.secrets.values()))

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


if __name__ == "__main__":
    unittest.main()
