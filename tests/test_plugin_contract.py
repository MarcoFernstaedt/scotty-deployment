from __future__ import annotations

import importlib
import json
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

import synthetic
from synthetic import (
    ANNOUNCEMENT_CHANNEL,
    CLIENT_GUILD,
    EMPLOYEE_CHANNEL,
    EMPLOYEE_USER,
    OPERATOR_CHANNEL,
    OPERATOR_USER,
    ROUTE_CHANNEL,
)

from assistant.scotty_business.identity import AuthorizedPrincipalResolver, IdentityError
from assistant.scotty_business.ingress import (
    CREDENTIAL_ROTATION_NOTICE,
    EMPLOYEE_SUMMARY_COMMAND,
    IngressGuard,
)
from assistant.scotty_business.policy import CODING_REFUSAL, EMPLOYEE_SUMMARY, Role

config = synthetic.config
event = synthetic.event


class IngressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.outbound: list[tuple[str, str]] = []
        self.guard = IngressGuard(
            config(), lambda channel, text: self.outbound.append((channel, text))
        )

    def test_all_wrong_and_mixed_tuples_are_silently_skipped_pre_model(self) -> None:
        cases = [
            event(guild="999000000000000001"),
            event(channel=EMPLOYEE_CHANNEL),
            event(user=EMPLOYEE_USER),
            event(channel="900", parent=EMPLOYEE_CHANNEL),
            event(channel="900", parent=OPERATOR_CHANNEL, user=EMPLOYEE_USER),
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

    def test_employee_summary_has_separate_fixed_request_and_destination(self) -> None:
        result = self.guard(event(text=EMPLOYEE_SUMMARY_COMMAND))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.outbound, [(EMPLOYEE_CHANNEL, EMPLOYEE_SUMMARY)])

    def test_fixed_paths_only_ever_reach_configured_client_destinations(self) -> None:
        allowed = set(synthetic.config().client_discord_destinations())
        self.assertEqual(allowed, {OPERATOR_CHANNEL, EMPLOYEE_CHANNEL, ANNOUNCEMENT_CHANNEL})
        self.guard(event(text=EMPLOYEE_SUMMARY_COMMAND))
        self.guard(event(text="Please write code for an integration"))
        for channel, _ in self.outbound:
            self.assertIn(channel, allowed)
            self.assertNotEqual(channel, ROUTE_CHANNEL)

    def test_credentials_and_coding_requests_never_reach_model(self) -> None:
        credential = self.guard(event(text="My token is " + "ghp_" + ("a" * 28)))
        self.assertEqual(credential["action"], "skip")
        self.assertEqual(self.outbound[-1], (OPERATOR_CHANNEL, CREDENTIAL_ROTATION_NOTICE))
        coding = self.guard(event(text="Please write code for an integration"))
        self.assertEqual(coding["action"], "skip")
        self.assertEqual(self.outbound[-1], (OPERATOR_CHANNEL, CODING_REFUSAL))

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
                        "guild_id": CLIENT_GUILD,
                        "scope_id": CLIENT_GUILD,
                        "chat_id": OPERATOR_CHANNEL,
                        "user_id": OPERATOR_USER,
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
                    {
                        "platform": "discord",
                        "guild_id": "999000000000000001",
                        "chat_id": OPERATOR_CHANNEL,
                        "user_id": OPERATOR_USER,
                    }
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
        self.assertIn("pre_gateway_dispatch", manifest)

    def test_no_unknown_plugin_hook_is_ever_registered(self) -> None:
        """Only hooks the pinned runtime actually invokes may be registered."""

        plugin = importlib.import_module("assistant.scotty_business")
        context = FakeContext()
        plugin.register(context)
        try:
            self.assertEqual(set(context.hooks), {"pre_gateway_dispatch"})
            manifest = Path("assistant/scotty_business/plugin.yaml").read_text(encoding="utf-8")
            declared = [
                line.strip().removeprefix("- ")
                for line in manifest.splitlines()
                if line.startswith("  - ")
            ]
            self.assertIn("pre_gateway_dispatch", declared)
            self.assertNotIn("resolve_enabled_toolsets_for_source", manifest)
            source = Path("assistant/scotty_business/__init__.py").read_text(encoding="utf-8")
            self.assertEqual(source.count("ctx.register_hook("), 1)
        finally:
            for unload in context.unloads:
                unload()


if __name__ == "__main__":
    unittest.main()
