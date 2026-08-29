from __future__ import annotations

import importlib
import json
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from assistant.scotty_business.config import RuntimeConfig
from assistant.scotty_business.identity import AuthorizedPrincipalResolver, IdentityError
from assistant.scotty_business.ingress import (
    CREDENTIAL_ROTATION_NOTICE,
    EMPLOYEE_SUMMARY_COMMAND,
    IngressGuard,
)
from assistant.scotty_business.policy import (
    CODING_REFUSAL,
    EMPLOYEE_SUMMARY,
    FIXED_WIZARD_COMMAND,
    SETUP_WIZARD,
    Role,
)


def config() -> RuntimeConfig:
    return RuntimeConfig.from_mapping(
        {
            "version": 1,
            "addons": ["discord", "trello", "ghl", "rentcast"],
            "principals": {
                "maintainer": {"guild_id": "100", "channel_id": "200", "user_id": "300"},
                "main_operator": {"guild_id": "100", "channel_id": "201", "user_id": "301"},
                "employee": {"guild_id": "100", "channel_id": "202", "user_id": "302"},
            },
            "discord": {"announcement_channel_ids": ["210"]},
            "trello": {
                "board_id": "board-1",
                "list_ids": ["list-1", "list-2"],
                "label_ids": ["label-1"],
                "custom_field_ids": ["field-1"],
            },
            "ghl": {"location_id": "location-1"},
            "rentcast": {
                "endpoints": ["/v1/properties", "/v1/avm/value", "/v1/avm/rent/long-term"]
            },
        }
    )


def event(
    guild: str = "100",
    channel: str = "201",
    user: str = "301",
    text: str = "Show configured leads",
    *,
    parent: str | None = None,
    is_bot: bool = False,
):
    return SimpleNamespace(
        text=text,
        source=SimpleNamespace(
            platform=SimpleNamespace(value="discord"),
            guild_id=guild,
            scope_id=guild,
            chat_id=channel,
            user_id=user,
            parent_chat_id=parent,
            is_bot=is_bot,
        ),
    )


class IngressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.outbound: list[tuple[str, str]] = []
        self.guard = IngressGuard(
            config(), lambda channel, text: self.outbound.append((channel, text))
        )

    def test_all_wrong_and_mixed_tuples_are_silently_skipped_pre_model(self) -> None:
        cases = [
            event(guild="999"),
            event(channel="202"),
            event(user="302"),
            event(channel="900", parent="202"),
            event(channel="900", parent="201", user="302"),
            event(is_bot=True),
        ]
        for candidate in cases:
            with self.subTest(source=candidate.source):
                self.assertEqual(
                    self.guard(candidate), {"action": "skip", "reason": "unauthorized"}
                )
        self.assertEqual(self.outbound, [])

    def test_authorized_normal_message_continues(self) -> None:
        self.assertEqual(self.guard(event()), {"action": "allow"})
        self.assertEqual(self.outbound, [])

    def test_fixed_wizard_is_maintainer_only_and_destination_is_not_model_selected(self) -> None:
        result = self.guard(event(channel="200", user="300", text=FIXED_WIZARD_COMMAND))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.outbound, [("201", SETUP_WIZARD)])
        self.outbound.clear()
        self.assertEqual(self.guard(event(text=FIXED_WIZARD_COMMAND))["action"], "skip")
        self.assertEqual(self.outbound, [])

    def test_employee_summary_has_separate_fixed_request_and_destination(self) -> None:
        result = self.guard(event(text=EMPLOYEE_SUMMARY_COMMAND))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.outbound, [("202", EMPLOYEE_SUMMARY)])

    def test_credentials_and_coding_requests_never_reach_model(self) -> None:
        credential = self.guard(event(text="My token is " + "ghp_" + ("a" * 28)))
        self.assertEqual(credential["action"], "skip")
        self.assertEqual(self.outbound[-1], ("201", CREDENTIAL_ROTATION_NOTICE))
        coding = self.guard(event(text="Please write code for an integration"))
        self.assertEqual(coding["action"], "skip")
        self.assertEqual(self.outbound[-1], ("201", CODING_REFUSAL))

    def test_text_slash_commands_are_not_available(self) -> None:
        self.assertEqual(
            self.guard(event(text="/model other")),
            {"action": "skip", "reason": "commands-disabled"},
        )


class PrincipalResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="scotty-identity-test-")
        self.home = Path(self.tempdir.name)
        connection = sqlite3.connect(self.home / "state.db")
        connection.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, origin_json TEXT)")
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?)",
            (
                "session-1",
                json.dumps(
                    {
                        "platform": "discord",
                        "guild_id": "100",
                        "scope_id": "100",
                        "chat_id": "201",
                        "user_id": "301",
                    }
                ),
            ),
        )
        connection.commit()
        connection.close()
        self.resolver = AuthorizedPrincipalResolver(self.home, config())

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_session_origin_resolves_exact_principal(self) -> None:
        self.assertEqual(self.resolver.resolve("session-1").role, Role.MAIN_OPERATOR)

    def test_missing_or_malformed_origin_fails_closed(self) -> None:
        with self.assertRaises(IdentityError):
            self.resolver.resolve("missing")
        connection = sqlite3.connect(self.home / "state.db")
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?)",
            (
                "wrong",
                json.dumps(
                    {"platform": "discord", "guild_id": "999", "chat_id": "201", "user_id": "301"}
                ),
            ),
        )
        connection.commit()
        connection.close()
        with self.assertRaises(IdentityError):
            self.resolver.resolve("wrong")


class FakeContext:
    def __init__(self) -> None:
        self.tools: dict[str, dict[str, object]] = {}
        self.hooks: dict[str, object] = {}
        self.sections: dict[str, str] = {}
        self.unloads: list[Callable[[], object]] = []

    def register_tool(self, **kwargs: object) -> None:
        self.tools[str(kwargs["name"])] = kwargs

    def register_hook(self, name: str, callback: object) -> None:
        self.hooks[name] = callback

    def register_system_prompt_section(self, id: str, content: str, **kwargs: object) -> None:
        self.sections[id] = content

    def on_unload(self, callback: Callable[[], object]) -> None:
        self.unloads.append(callback)


class PluginRegistrationTests(unittest.TestCase):
    def test_registration_exposes_only_bounded_scotty_tools(self) -> None:
        plugin = importlib.import_module("assistant.scotty_business")
        context = FakeContext()
        plugin.register(context)
        self.assertEqual(
            set(context.tools),
            {
                "scotty_read",
                "scotty_propose",
                "scotty_approval",
                "scotty_reminder",
                "scotty_calculate",
            },
        )
        for registration in context.tools.values():
            self.assertEqual(registration["toolset"], "scotty")
            schema = registration["schema"]
            self.assertNotIn("principal", json.dumps(schema))
        self.assertEqual(set(context.hooks), {"pre_gateway_dispatch"})
        self.assertIn("Scotty by The Closing Room", context.sections["scotty.identity"])
        self.assertNotIn("Hermes", context.sections["scotty.identity"])
        for unload in context.unloads:
            unload()

    def test_manifest_and_package_shape_are_present(self) -> None:
        root = Path("assistant/scotty_business")
        self.assertTrue((root / "plugin.yaml").is_file())
        manifest = (root / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("name: scotty-business", manifest)
        self.assertIn("version: 1.0.0", manifest)


if __name__ == "__main__":
    unittest.main()
