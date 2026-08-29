from __future__ import annotations

import importlib
import json
import re
import unittest
from collections.abc import Callable
from pathlib import Path

import synthetic
from synthetic import ROUTE_CHANNEL as MAINT_CHANNEL
from synthetic import ROUTE_GUILD as MAINT_GUILD
from synthetic import ROUTE_USER as MAINT_USER
from test_setup import maintainer_sample

from assistant.scotty_business import guidance
from assistant.scotty_business.ingress import CREDENTIAL_ROTATION_NOTICE
from assistant.scotty_business.policy import (
    ADDON_CAP_RESPONSE,
    CODING_REFUSAL,
    EMPLOYEE_SUMMARY,
)
from assistant.scotty_business.routing import CLIENT_PROFILES, MAINTAINER_PROFILE
from assistant.scotty_business.setup import render_hermes_config, render_profile_config

_ROUTE_IDENTIFIERS = (MAINT_GUILD, MAINT_CHANNEL, MAINT_USER)
_DISCLOSURE_PATTERNS = (
    re.compile(r"maintainer (?:guild|server|channel|route|profile)", re.I),
    re.compile(r"hidden (?:route|admin|channel|profile|server)", re.I),
    re.compile(r"admin (?:route|server|channel)", re.I),
    re.compile(r"second(?:ary)? (?:guild|server)", re.I),
)


def client_facing_strings() -> dict[str, str]:
    plugin = importlib.import_module("assistant.scotty_business")
    strings = {
        "employee_summary": EMPLOYEE_SUMMARY,
        "coding_refusal": CODING_REFUSAL,
        "addon_cap": ADDON_CAP_RESPONSE,
        "credential_notice": CREDENTIAL_ROTATION_NOTICE,
        "identity_prompt": plugin._IDENTITY_PROMPT,
    }
    for profile_name in CLIENT_PROFILES.values():
        strings[f"profile_config:{profile_name}"] = render_profile_config(profile_name)
    for name in guidance.PROVIDERS:
        strings[f"guidance:{name}"] = guidance.provider_guidance(name).as_text()
    strings["guidance:index"] = guidance.all_provider_guidance_text()
    return strings


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


class ClientFacingSecrecyTests(unittest.TestCase):
    def test_no_fixed_client_string_carries_a_maintainer_identifier(self) -> None:
        for name, value in client_facing_strings().items():
            for identifier in _ROUTE_IDENTIFIERS:
                with self.subTest(string=name, identifier=identifier):
                    self.assertNotIn(identifier, value)

    def test_no_fixed_client_string_discloses_that_a_hidden_route_exists(self) -> None:
        for name, value in client_facing_strings().items():
            for pattern in _DISCLOSURE_PATTERNS:
                with self.subTest(string=name, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(value))

    def test_client_profile_names_carry_no_route_identifier(self) -> None:
        for name in CLIENT_PROFILES.values():
            for identifier in _ROUTE_IDENTIFIERS:
                self.assertNotIn(identifier, name)
            self.assertNotEqual(name, MAINTAINER_PROFILE)

    def test_registered_tool_schemas_and_prompts_stay_free_of_route_identifiers(self) -> None:
        plugin = importlib.import_module("assistant.scotty_business")
        context = FakeContext()
        plugin.register(context)
        try:
            rendered = json.dumps(
                {
                    "tools": {
                        name: {
                            key: value for key, value in registration.items() if key != "handler"
                        }
                        for name, registration in context.tools.items()
                    },
                    "sections": context.sections,
                }
            )
            for identifier in _ROUTE_IDENTIFIERS:
                self.assertNotIn(identifier, rendered)
            for pattern in _DISCLOSURE_PATTERNS:
                self.assertIsNone(pattern.search(rendered))
        finally:
            for unload in context.unloads:
                unload()

    def test_public_repository_sources_contain_no_route_disclosure_language(self) -> None:
        root = Path("assistant/scotty_business")
        for path in sorted(root.rglob("*.py")):
            content = path.read_text(encoding="utf-8")
            for identifier in _ROUTE_IDENTIFIERS:
                with self.subTest(path=str(path), identifier=identifier):
                    self.assertNotIn(identifier, content)


class OwnerOnlyRuntimeConfigTests(unittest.TestCase):
    """The gateway config is owner-only runtime state, never client-facing text.

    It must carry the route channel so the gateway delivers those messages at
    all, while the plugin still decides the profile and toolset before dispatch.
    """

    def test_gateway_config_admits_the_route_without_widening_client_toolsets(self) -> None:
        rendered = render_hermes_config(maintainer_sample())
        self.assertIn(MAINT_CHANNEL, rendered)
        self.assertIn('discord: ["scotty"]', rendered)

    def test_the_full_profile_config_carries_no_bounded_client_identity(self) -> None:
        rendered = render_profile_config(MAINTAINER_PROFILE)
        self.assertNotIn("scotty-business", rendered)
        self.assertNotIn("Scotty by The Closing Room", rendered)

    def test_the_gateway_config_is_never_a_tracked_repository_artifact(self) -> None:
        self.assertFalse(Path("config.yaml").exists())
        self.assertFalse(Path("scotty/private.json").exists())


class ClientDestinationIsolationTests(unittest.TestCase):
    def test_route_channel_is_never_a_client_discord_destination(self) -> None:
        config = synthetic.config()
        self.assertNotIn(MAINT_CHANNEL, config.client_discord_destinations())

    def test_client_announcement_scope_cannot_be_widened_to_the_route(self) -> None:
        config = synthetic.config()
        self.assertNotIn(config.maintainer_route.channel_id, config.announcement_channel_ids)


if __name__ == "__main__":
    unittest.main()
