from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import synthetic

from assistant.scotty_business.guidance import NOT_CONNECTED, PROVIDERS
from assistant.scotty_business.policy import Principal, Role
from assistant.scotty_business.runtime import ProviderNotConnected, Runtime

_ALL_SECRETS = {
    "DISCORD_BOT_TOKEN": "synthetic-discord",
    "SCOTTY_TRELLO_API_KEY": "synthetic-trello-key",
    "SCOTTY_TRELLO_TOKEN": "synthetic-trello-token",
    "SCOTTY_GHL_PRIVATE_TOKEN": "synthetic-ghl",
    "SCOTTY_RENTCAST_API_KEY": "synthetic-rentcast",
    # The per-actor variables are saved and cleared too, so one test's
    # per-user credential can never leak into another test's runtime.
    **{
        f"{name}_{role}": f"synthetic-{name.lower()}-{role.lower()}"
        for name in (
            "SCOTTY_TRELLO_API_KEY",
            "SCOTTY_TRELLO_TOKEN",
            "SCOTTY_GHL_PRIVATE_TOKEN",
            "SCOTTY_RENTCAST_API_KEY",
        )
        for role in ("MAIN_OPERATOR", "EMPLOYEE")
    },
}


@contextmanager
def runtime(private: dict[str, object] | None = None, **secrets: str) -> Iterator[Runtime]:
    private = private or {}
    with tempfile.TemporaryDirectory(prefix="scotty-connection-test-") as directory:
        home = Path(directory)
        (home / "scotty").mkdir()
        (home / "scotty" / "private.json").write_text(
            json.dumps(synthetic.private_mapping(**private)), encoding="utf-8"
        )
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
        guild_id=synthetic.CLIENT_GUILD,
        channel_id=synthetic.OPERATOR_CHANNEL,
        user_id=synthetic.OPERATOR_USER,
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

    def test_google_workspace_is_reported_as_a_release_add_on(self) -> None:
        with runtime(**_ALL_SECRETS) as instance:
            result = instance.handle_read(
                operator(), {"operation": "provider_setup", "provider": "google_workspace"}
            )
            assert isinstance(result, dict)
            self.assertEqual(result["status"], NOT_CONNECTED)
            status = instance.handle_read(operator(), {"operation": "status"})
            assert isinstance(status, dict)
            self.assertIn("google_workspace", status["addons"])
            self.assertEqual(status["addon_slots_remaining"], 1)

    def test_reading_an_unconnected_provider_is_denied_rather_than_attempted(self) -> None:
        with (
            runtime(DISCORD_BOT_TOKEN="synthetic-discord") as instance,
            self.assertRaises(ProviderNotConnected),
        ):
            instance.handle_read(operator(), {"operation": "trello_cards"})

    def test_a_provider_with_a_credential_but_no_scope_is_not_connected(self) -> None:
        """A credential without its configured resource scope is not a connection."""

        with runtime({"trello": None, "ghl": None, "rentcast": None}, **_ALL_SECRETS) as instance:
            status = instance.provider_connection_status()
            for name in ("trello", "ghl", "rentcast"):
                with self.subTest(provider=name):
                    self.assertFalse(status[name])

    def test_a_discord_only_deployment_starts_and_reports_every_provider(self) -> None:
        with runtime(
            {"trello": None, "ghl": None, "rentcast": None},
            DISCORD_BOT_TOKEN="synthetic-discord",
        ) as instance:
            result = instance.handle_read(operator(), {"operation": "provider_setup"})
            assert isinstance(result, dict)
            providers = result["providers"]
            assert isinstance(providers, dict)
            self.assertEqual(set(providers), set(PROVIDERS))
            for name in ("trello", "ghl", "rentcast", "google_workspace"):
                with self.subTest(provider=name):
                    entry = providers[name]
                    assert isinstance(entry, dict)
                    self.assertEqual(entry["status"], NOT_CONNECTED)

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
