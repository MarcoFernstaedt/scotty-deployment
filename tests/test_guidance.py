from __future__ import annotations

import re
import unittest

from assistant.scotty_business.guidance import (
    LOCAL_SETUP_DIRECTIVE,
    NOT_CONNECTED,
    PROVIDERS,
    all_provider_guidance_text,
    provider_guidance,
    provider_status,
)

_CREDENTIAL_REQUEST = re.compile(
    r"(?:send|paste|post|share|give|reply with|type)\b[^.]{0,60}"
    r"(?:token|api[_ -]?key|secret|password|credential)",
    re.I,
)


class ProviderGuidanceTests(unittest.TestCase):
    def test_every_release_provider_has_deterministic_guidance(self) -> None:
        self.assertEqual(PROVIDERS, ("discord", "trello", "ghl", "rentcast", "google_workspace"))
        for name in PROVIDERS:
            with self.subTest(provider=name):
                guidance = provider_guidance(name)
                self.assertEqual(guidance.provider, name)
                self.assertTrue(guidance.steps)
                self.assertTrue(guidance.as_text().strip())
                self.assertEqual(guidance.as_text(), provider_guidance(name).as_text())

    def test_an_unconfigured_provider_states_not_connected(self) -> None:
        for name in PROVIDERS:
            with self.subTest(provider=name):
                guidance = provider_guidance(name, connected=False)
                self.assertEqual(guidance.status, NOT_CONNECTED)
                self.assertIn(NOT_CONNECTED, guidance.as_text())

    def test_a_configured_provider_does_not_claim_to_be_unconnected(self) -> None:
        guidance = provider_guidance("trello", connected=True)
        self.assertNotEqual(guidance.status, NOT_CONNECTED)
        self.assertNotIn(NOT_CONNECTED, guidance.as_text())

    def test_guidance_names_the_required_ids_and_scopes(self) -> None:
        trello = provider_guidance("trello")
        self.assertIn("board ID", " ".join(trello.required_ids))
        ghl = provider_guidance("ghl")
        self.assertIn("location ID", " ".join(ghl.required_ids))
        self.assertTrue(any("Private Integration" in item for item in ghl.required_scopes))
        rentcast = provider_guidance("rentcast")
        self.assertTrue(any("read" in item.lower() for item in rentcast.required_scopes))
        discord = provider_guidance("discord")
        self.assertTrue(any("Manage Channels" in item for item in discord.required_scopes))
        self.assertFalse(
            any("Administrator" in item for item in discord.required_scopes),
            "Discord Administrator is never required",
        )

    def test_guidance_directs_the_operator_to_the_local_setup_command(self) -> None:
        for name in PROVIDERS:
            with self.subTest(provider=name):
                self.assertIn(LOCAL_SETUP_DIRECTIVE, provider_guidance(name).as_text())

    def test_guidance_never_asks_for_a_credential_in_discord(self) -> None:
        for name in PROVIDERS:
            with self.subTest(provider=name):
                text = provider_guidance(name).as_text()
                self.assertIsNone(_CREDENTIAL_REQUEST.search(text))
                self.assertIn("Never", text)

    def test_google_workspace_is_guidance_only_and_takes_no_add_on_slot(self) -> None:
        google = provider_guidance("google_workspace")
        self.assertEqual(google.status, NOT_CONNECTED)
        text = google.as_text().lower()
        self.assertIn("not installed", text)
        self.assertIn("add-on", text)

    def test_unknown_providers_are_refused_rather_than_invented(self) -> None:
        with self.assertRaises(KeyError):
            provider_guidance("zillow")

    def test_provider_status_reports_only_configured_release_providers(self) -> None:
        status = provider_status({"trello": True, "ghl": False})
        self.assertTrue(status["trello"])
        self.assertFalse(status["ghl"])
        self.assertFalse(status["google_workspace"])
        self.assertEqual(set(status), set(PROVIDERS))

    def test_the_combined_index_lists_every_provider_once(self) -> None:
        text = all_provider_guidance_text()
        for name in PROVIDERS:
            with self.subTest(provider=name):
                guidance = provider_guidance(name)
                header = f"{guidance.display_name}: {guidance.status}"
                self.assertEqual(text.count(header), 1)

    def test_the_combined_index_reflects_each_provider_connection_state(self) -> None:
        text = all_provider_guidance_text({"trello": True})
        self.assertIn("Trello: connected", text)
        self.assertIn("GoHighLevel: not connected", text)


if __name__ == "__main__":
    unittest.main()
