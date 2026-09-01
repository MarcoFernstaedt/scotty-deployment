"""What this release actually lets an operator rename, and what it does not.

The documents said the product is white-labelable. The rename plan, in the
same repository, said the rename is not implemented and lists the packages,
tool names, profiles, paths, commands, container, network and environment
variables that still carry the old name. Both cannot be true, and a deferred
plan is not a shipped feature.

So this establishes the surface by exercising it rather than by reading about
it. One thing is configurable: what a client's assistant is called, per client,
chosen by that client. Everything else is a constant, and these try to change
each one through configuration and show that nothing moves.

What the documents may then claim is exactly this much.
"""

from __future__ import annotations

import unittest

import synthetic

from assistant.scotty_business.config import RuntimeConfig
from assistant.scotty_business.persona import (
    DEFAULT_ASSISTANT_NAME,
    PersonaError,
    resolve_persona,
    validate_assistant_name,
)
from assistant.scotty_business.policy import Role


def config(**overrides) -> RuntimeConfig:
    return RuntimeConfig.from_mapping(synthetic.private_mapping(**overrides))


class PersonaCustomizationTests(unittest.TestCase):
    """The one identity this release really does let somebody change."""

    def test_each_client_user_s_assistant_name_is_their_own(self) -> None:
        settings = config()
        personas = {"main_operator": "Scotty", "employee": "Nova"}
        operator = resolve_persona(settings, Role.MAIN_OPERATOR, personas)
        employee = resolve_persona(settings, Role.EMPLOYEE, personas)
        self.assertEqual(operator.assistant_name, "Scotty")
        self.assertEqual(employee.assistant_name, "Nova")

    def test_an_unchosen_name_is_neutral_rather_than_the_other_user_s(self) -> None:
        settings = config()
        self.assertEqual(
            resolve_persona(settings, Role.EMPLOYEE, {"main_operator": "Scotty"}).assistant_name,
            DEFAULT_ASSISTANT_NAME,
        )

    def test_a_name_that_advertises_what_is_underneath_is_refused(self) -> None:
        for name in ("Hermes", "Nous Research", "OpenRouter", "Claude", "GPT-4"):
            with self.subTest(name=name), self.assertRaises(PersonaError):
                validate_assistant_name(name)
        # And a name that says nothing about the machinery is accepted, so the
        # refusal is a filter rather than a wall.
        self.assertEqual(validate_assistant_name("Nova"), "Nova")


class ProductIdentityTests(unittest.TestCase):
    """Everything else is a constant, and configuration cannot move it.

    Each of these takes the identifier the rename plan lists, tries to override
    it through the configuration this release actually reads, and shows the
    identifier unchanged. That is the difference between "renameable" and "on
    a list of things to rename later".
    """

    def overridden(self):
        """A private mapping that asks, in every plausible spelling, to rebrand."""

        mapping = synthetic.private_mapping()
        mapping.update(
            {
                "product_name": "Closing Room",
                "product_slug": "closing_room",
                "brand": "Closing Room",
                "tool_prefix": "closing_room",
                "install_root": "/srv/ClosingRoom",
                "container_name": "closing-room",
                "network_name": "closing-room-egress",
            }
        )
        return mapping

    def test_configuration_cannot_rename_the_model_visible_tools(self) -> None:
        from assistant.scotty_business import client_tool_schemas

        before = [schema["name"] for schema in client_tool_schemas()]
        RuntimeConfig.from_mapping(self.overridden())
        after = [schema["name"] for schema in client_tool_schemas()]
        self.assertEqual(before, after)
        # Named rather than merely equal, so this fails loudly if the tool
        # surface is renamed without the claim being revisited.
        self.assertIn("scotty_read", after)

    def test_configuration_cannot_rename_the_served_profiles(self) -> None:
        from assistant.scotty_business.routing import (
            CLIENT_PROFILES,
            MAINTAINER_PROFILE,
            SERVED_PROFILES,
        )

        RuntimeConfig.from_mapping(self.overridden())
        self.assertEqual(MAINTAINER_PROFILE, "scotty-maintainer")
        self.assertEqual(
            set(SERVED_PROFILES),
            {"scotty-maintainer", "scotty-main-operator", "scotty-employee"},
        )
        self.assertTrue(all(name.startswith("scotty-") for name in CLIENT_PROFILES.values()))

    def test_configuration_cannot_move_the_installed_paths_or_sockets(self) -> None:
        from assistant.scotty_broker.broker import SOCKET_PATH, STORE_PATH
        from assistant.scotty_business.credential_intake import BROKER_SOCKET

        RuntimeConfig.from_mapping(self.overridden())
        self.assertEqual(STORE_PATH, "/var/lib/scotty/credentials.json")
        self.assertEqual(SOCKET_PATH, "/run/scotty/credential-broker.sock")
        self.assertEqual(BROKER_SOCKET, "/run/scotty/credential-broker.sock")

    def test_configuration_cannot_rename_the_credential_environment_variables(self) -> None:
        # The names setup accepts are fixed; an unrecognised one is refused
        # rather than quietly stored, which is also why a rebrand of these is
        # a migration and not a setting.
        from assistant.scotty_business.setup import _BROKER_ADDRESSES, broker_commitments

        RuntimeConfig.from_mapping(self.overridden())
        self.assertTrue(all(name.startswith("SCOTTY_") for name in _BROKER_ADDRESSES))
        self.assertIn("SCOTTY_TRELLO_API_KEY", _BROKER_ADDRESSES)
        self.assertTrue(callable(broker_commitments))

    def test_the_installer_names_one_container_and_one_network_by_constant(self) -> None:
        import re
        from pathlib import Path

        installer = Path("install.sh").read_text(encoding="utf-8")
        self.assertRegex(installer, r"readonly CONTAINER=scotty\b")
        self.assertRegex(installer, r"readonly NETWORK=scotty-egress\b")
        # And nothing in the installer reads a product name from anywhere.
        self.assertIsNone(re.search(r"\$\{?PRODUCT_NAME", installer))


class DocumentedClaimTests(unittest.TestCase):
    """The documents may claim exactly the surface above, and no more."""

    DOCUMENTS = (
        "README.md",
        "CLAUDE.md",
        "docs/white-label-rename-plan.md",
        "docs/scotty-basic-operations.md",
        ".claude/rules/identity-routing-branding.md",
        "assistant/scotty_business/persona.py",
    )

    def test_no_document_calls_the_product_itself_white_labelable(self) -> None:
        """The deferred plan may be described; it may not be described as done.

        "White-labelable" in the present tense says an operator can ship this
        under their own name today. They cannot: the packages, tools, profiles,
        paths, commands, container, network and environment variables are the
        constants the tests above pin down.
        """

        from pathlib import Path

        for name in self.DOCUMENTS:
            body = Path(name).read_text(encoding="utf-8")
            for line_number, line in enumerate(body.splitlines(), start=1):
                lowered = line.casefold()
                if "white-label" not in lowered and "white label" not in lowered:
                    continue
                with self.subTest(document=name, line=line_number):
                    # A claim about what the product *is*, rather than about a
                    # plan, a goal, or the persona naming that does ship.
                    self.assertNotIn("white-labelable", lowered, line.strip())
                    self.assertNotIn("white labelable", lowered, line.strip())
                    self.assertNotIn("white-labellable", lowered, line.strip())

    def test_the_rename_plan_says_plainly_that_it_has_not_run(self) -> None:
        from pathlib import Path

        plan = Path("docs/white-label-rename-plan.md").read_text(encoding="utf-8").casefold()
        self.assertIn("not implemented", plan)

    def test_the_readme_states_what_this_release_actually_supports(self) -> None:
        from pathlib import Path

        readme = Path("README.md").read_text(encoding="utf-8").casefold()
        self.assertIn("persona-name customization", readme)


if __name__ == "__main__":
    unittest.main()
