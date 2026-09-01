"""One runtime, three people, and no upstream branding in front of any of them.

"Scotty" is Trent's assistant, Mikey names his own, and neither name is the
product's. Nothing a client ever sees may advertise the framework, the model
provider, or the infrastructure underneath.

This is the naming surface this release ships. Renaming the product itself is
not implemented; `tests/test_product_identity.py` holds that line.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import synthetic

from assistant.scotty_business.persona import (
    DEFAULT_ASSISTANT_NAME,
    PersonaError,
    PersonaStore,
    resolve_persona,
)
from assistant.scotty_business.policy import Role

#: Names that must never reach a client-visible surface.
UPSTREAM_BRANDS = (
    "hermes",
    "nous research",
    "nousresearch",
    "openclaw",
    "openrouter",
    "anthropic",
    "claude",
    "openai",
    "gpt-",
    "codex",
    "docker",
    "systemd",
)


class PersonaResolutionTests(unittest.TestCase):
    def test_each_client_user_has_their_own_assistant_name(self) -> None:
        config = synthetic.config(
            personas={"main_operator": "Scotty", "employee": "Nova"},
        )
        self.assertEqual(resolve_persona(config, Role.MAIN_OPERATOR).assistant_name, "Scotty")
        self.assertEqual(resolve_persona(config, Role.EMPLOYEE).assistant_name, "Nova")

    def test_an_unnamed_user_gets_a_neutral_default_not_the_other_users_name(self) -> None:
        config = synthetic.config(personas={"main_operator": "Scotty"})
        employee = resolve_persona(config, Role.EMPLOYEE)
        self.assertEqual(employee.assistant_name, DEFAULT_ASSISTANT_NAME)
        self.assertNotEqual(employee.assistant_name, "Scotty")

    def test_a_stored_choice_overrides_the_configured_default(self) -> None:
        config = synthetic.config(personas={"employee": "Nova"})
        with tempfile.TemporaryDirectory(prefix="scotty-persona-") as directory:
            store = PersonaStore(Path(directory) / "personas.json", owner_uid=None)
            store.set(Role.EMPLOYEE, "Juno")
            self.assertEqual(
                resolve_persona(config, Role.EMPLOYEE, store.read()).assistant_name, "Juno"
            )
            # One user's choice never renames the other user's assistant.
            self.assertEqual(
                resolve_persona(config, Role.MAIN_OPERATOR, store.read()).assistant_name,
                DEFAULT_ASSISTANT_NAME,
            )

    def test_a_name_is_bounded_and_never_impersonates_the_product_or_upstream(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-persona-") as directory:
            store = PersonaStore(Path(directory) / "personas.json", owner_uid=None)
            for bad in (
                "",
                " ",
                "x" * 41,
                "Hermes",
                "hermes agent",
                "Claude",
                "OpenAI helper",
                "Nous Research",
                "line\nbreak",
                "control\x00char",
                "@everyone",
            ):
                with self.subTest(name=bad), self.assertRaises(PersonaError):
                    store.set(Role.EMPLOYEE, bad)

    def test_a_persona_file_that_was_tampered_with_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-persona-") as directory:
            path = Path(directory) / "personas.json"
            path.write_text(json.dumps({"employee": "Hermes", "maintainer": "x"}), encoding="utf-8")
            store = PersonaStore(path, owner_uid=None)
            self.assertEqual(store.read(), {})
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(store.read(), {})


class ClientVisibleBrandingTests(unittest.TestCase):
    """No client-visible string advertises what Scotty is built on."""

    def client_strings(self) -> dict[str, str]:
        from assistant.scotty_business import _IDENTITY_PROMPT, client_tool_schemas
        from assistant.scotty_business.guidance import PROVIDERS, provider_guidance
        from assistant.scotty_business.policy import employee_summary, setup_wizard

        strings = {
            "identity_prompt": _IDENTITY_PROMPT,
            "setup_wizard": setup_wizard("Scotty"),
            "employee_summary": employee_summary("Nova"),
            "tool_schemas": json.dumps(client_tool_schemas()),
        }
        for provider in PROVIDERS:
            guidance = provider_guidance(provider)
            strings[f"guidance.{provider}"] = guidance.as_text()
        return strings

    def test_no_client_visible_string_names_the_framework_or_model_provider(self) -> None:
        for label, text in self.client_strings().items():
            lowered = text.casefold()
            for brand in UPSTREAM_BRANDS:
                with self.subTest(surface=label, brand=brand):
                    self.assertNotIn(brand, lowered)

    def test_the_identity_prompt_names_this_users_assistant_and_no_product_brand(self) -> None:
        from assistant.scotty_business import identity_prompt

        prompt = identity_prompt("Nova")
        self.assertIn("Nova", prompt)
        self.assertNotIn("Scotty by The Closing Room", prompt)

    def test_the_wizard_and_summary_carry_the_reader_s_own_assistant_name(self) -> None:
        from assistant.scotty_business.policy import employee_summary, setup_wizard

        self.assertIn("Scotty", setup_wizard("Scotty"))
        self.assertIn("Nova", employee_summary("Nova"))
        self.assertNotIn("Scotty", employee_summary("Nova"))


class PersonaSelectionTests(unittest.TestCase):
    """A user may name their own assistant, and only their own."""

    def runtime(self):
        from test_provider_connection import runtime

        return runtime(DISCORD_BOT_TOKEN="synthetic-discord")

    def principal(self, role: Role):
        from assistant.scotty_business.policy import Principal

        if role is Role.EMPLOYEE:
            return Principal(
                synthetic.CLIENT_GUILD,
                synthetic.EMPLOYEE_CHANNEL,
                synthetic.EMPLOYEE_USER,
                Role.EMPLOYEE,
            )
        return Principal(
            synthetic.CLIENT_GUILD,
            synthetic.OPERATOR_CHANNEL,
            synthetic.OPERATOR_USER,
            Role.MAIN_OPERATOR,
        )

    def test_a_user_renames_their_own_assistant_and_not_the_others(self) -> None:
        with self.runtime() as runtime:
            employee = self.principal(Role.EMPLOYEE)
            operator = self.principal(Role.MAIN_OPERATOR)
            runtime.handle_read(employee, {"operation": "persona", "action": "set", "name": "Nova"})
            self.assertEqual(
                runtime.handle_read(employee, {"operation": "persona"})["assistant_name"], "Nova"
            )
            self.assertNotEqual(
                runtime.handle_read(operator, {"operation": "persona"})["assistant_name"], "Nova"
            )

    def test_a_rejected_name_is_explained_rather_than_stored(self) -> None:
        with self.runtime() as runtime:
            employee = self.principal(Role.EMPLOYEE)
            before = runtime.handle_read(employee, {"operation": "persona"})["assistant_name"]
            answer = runtime.handle_read(
                employee, {"operation": "persona", "action": "set", "name": "Hermes"}
            )
            self.assertFalse(answer["accepted"])
            self.assertEqual(
                runtime.handle_read(employee, {"operation": "persona"})["assistant_name"], before
            )

    def test_the_status_reply_uses_the_callers_own_assistant_name(self) -> None:
        with self.runtime() as runtime:
            employee = self.principal(Role.EMPLOYEE)
            runtime.handle_read(employee, {"operation": "persona", "action": "set", "name": "Nova"})
            status = runtime.handle_read(employee, {"operation": "status"})
            self.assertEqual(status["identity"], "Nova")
            self.assertNotIn("Closing Room", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
