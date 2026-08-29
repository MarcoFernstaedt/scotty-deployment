from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from assistant.scotty_business.guidance import NOT_CONNECTED, PROVIDERS
from assistant.scotty_business.policy import Principal, Role
from assistant.scotty_business.runtime import ProviderNotConnected, Runtime

PRIVATE = {
    "version": 1,
    "addons": ["discord", "trello", "ghl", "rentcast"],
    "principals": {
        "maintainer": {
            "guild_id": "100000000000000001",
            "channel_id": "200000000000000001",
            "user_id": "300000000000000001",
        },
        "main_operator": {
            "guild_id": "100000000000000001",
            "channel_id": "201000000000000001",
            "user_id": "301000000000000001",
        },
        "employee": {
            "guild_id": "100000000000000001",
            "channel_id": "202000000000000001",
            "user_id": "302000000000000001",
        },
    },
    "discord": {"announcement_channel_ids": ["210000000000000001"]},
    "trello": {
        "board_id": "board-1",
        "list_ids": ["list-1"],
        "label_ids": [],
        "custom_field_ids": [],
    },
    "ghl": {"location_id": "location-1"},
    "rentcast": {"endpoints": ["/v1/properties"]},
}

_ALL_SECRETS = {
    "DISCORD_BOT_TOKEN": "synthetic-discord",
    "SCOTTY_TRELLO_API_KEY": "synthetic-trello-key",
    "SCOTTY_TRELLO_TOKEN": "synthetic-trello-token",
    "SCOTTY_GHL_PRIVATE_TOKEN": "synthetic-ghl",
    "SCOTTY_RENTCAST_API_KEY": "synthetic-rentcast",
}


@contextmanager
def runtime(**secrets: str) -> Iterator[Runtime]:
    with tempfile.TemporaryDirectory(prefix="scotty-connection-test-") as directory:
        home = Path(directory)
        (home / "scotty").mkdir()
        (home / "scotty" / "private.json").write_text(json.dumps(PRIVATE), encoding="utf-8")
        saved = {name: os.environ.get(name) for name in _ALL_SECRETS}
        try:
            for name in _ALL_SECRETS:
                os.environ.pop(name, None)
            for name, value in secrets.items():
                os.environ[name] = value
            yield Runtime(home)
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def operator() -> Principal:
    return Principal(
        guild_id="100000000000000001",
        channel_id="201000000000000001",
        user_id="301000000000000001",
        role=Role.MAIN_OPERATOR,
    )


class ProviderConnectionTests(unittest.TestCase):
    def test_missing_provider_credentials_do_not_take_the_assistant_down(self) -> None:
        with runtime(DISCORD_BOT_TOKEN="synthetic-discord") as instance:
            status = instance.provider_connection_status()
            self.assertTrue(status["discord"])
            for name in ("trello", "ghl", "rentcast", "google_workspace"):
                with self.subTest(provider=name):
                    self.assertFalse(status[name])

    def test_provider_setup_read_reports_not_connected_with_deterministic_steps(self) -> None:
        with runtime(DISCORD_BOT_TOKEN="synthetic-discord") as instance:
            result = instance.handle_read(
                operator(), {"operation": "provider_setup", "provider": "trello"}
            )
            assert isinstance(result, dict)
            self.assertEqual(result["provider"], "trello")
            self.assertEqual(result["status"], NOT_CONNECTED)
            self.assertTrue(result["steps"])
            self.assertIn("local setup command", str(result["guidance"]))

    def test_provider_setup_read_lists_every_provider_when_none_is_named(self) -> None:
        with runtime(DISCORD_BOT_TOKEN="synthetic-discord") as instance:
            result = instance.handle_read(operator(), {"operation": "provider_setup"})
            assert isinstance(result, dict)
            self.assertEqual(set(result["providers"]), set(PROVIDERS))
            self.assertEqual(result["providers"]["ghl"]["status"], NOT_CONNECTED)

    def test_google_workspace_is_reported_without_consuming_an_add_on_slot(self) -> None:
        with runtime(**_ALL_SECRETS) as instance:
            result = instance.handle_read(
                operator(), {"operation": "provider_setup", "provider": "google_workspace"}
            )
            assert isinstance(result, dict)
            self.assertEqual(result["status"], NOT_CONNECTED)
            status = instance.handle_read(operator(), {"operation": "status"})
            assert isinstance(status, dict)
            self.assertNotIn("google_workspace", status["addons"])
            self.assertEqual(status["addon_slots_remaining"], 2)

    def test_reading_an_unconnected_provider_is_denied_rather_than_attempted(self) -> None:
        with (
            runtime(DISCORD_BOT_TOKEN="synthetic-discord") as instance,
            self.assertRaises(ProviderNotConnected),
        ):
            instance.handle_read(operator(), {"operation": "trello_cards"})

    def test_a_configured_provider_reports_connected(self) -> None:
        with runtime(**_ALL_SECRETS) as instance:
            status = instance.provider_connection_status()
            for name in ("discord", "trello", "ghl", "rentcast"):
                with self.subTest(provider=name):
                    self.assertTrue(status[name])
            self.assertFalse(status["google_workspace"])

    def test_provider_setup_output_never_asks_for_a_credential(self) -> None:
        with runtime(DISCORD_BOT_TOKEN="synthetic-discord") as instance:
            result = instance.handle_read(operator(), {"operation": "provider_setup"})
            rendered = json.dumps(result)
            self.assertNotIn("synthetic-discord", rendered)
            self.assertIn("Never put a key, token, or password in Discord", rendered)


if __name__ == "__main__":
    unittest.main()
