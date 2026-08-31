from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

import synthetic

from assistant.scotty_business.config import GOOGLE_OAUTH_SCOPES
from assistant.scotty_business.guidance import PROVIDERS, provider_guidance
from assistant.scotty_business.setup_flow import (
    FAILURE_KINDS,
    REQUIRED_IDENTIFIERS,
    SetupFlowError,
    SetupStagingStore,
    diagnose,
    first_unfinished,
    setup_progress,
    validate_identifier,
)

_CREDENTIAL_REQUEST = re.compile(
    r"(?:send|paste|post|share|give|reply with|type)\b[^.]{0,60}"
    r"(?:token|api[_ -]?key|secret|password|credential)",
    re.I,
)

NOTHING_CONNECTED = dict.fromkeys(PROVIDERS, False)
GOOGLE_SCOPE = {
    "account_email": "scotty.synthetic@example.invalid",
    "oauth_scopes": list(GOOGLE_OAUTH_SCOPES),
}


class GuidedExplanationTests(unittest.TestCase):
    def test_every_provider_explains_apis_scopes_ids_and_callback_behaviour(self) -> None:
        for name in PROVIDERS:
            with self.subTest(provider=name):
                guidance = provider_guidance(name)
                text = guidance.as_text()
                self.assertTrue(guidance.apis)
                self.assertTrue(guidance.callback)
                self.assertIn("APIs or products to enable:", text)
                self.assertIn("Callback or redirect:", text)
                self.assertTrue(guidance.required_ids)

    def test_google_guidance_matches_the_corrected_account_wide_model(self) -> None:
        text = provider_guidance("google_workspace").as_text()
        lowered = text.lower()
        self.assertIn("gmail api", lowered)
        self.assertIn("people api", lowered)
        self.assertIn("127.0.0.1", text)
        self.assertIn("oauth2/callback", text)
        # The stale resource-allowlist promise must be gone, and trash and
        # sharing must be described as gated rather than absent.
        self.assertNotIn("exact Gmail label and Calendar IDs", text)
        self.assertNotIn("drive file, docs document, sheets spreadsheet", lowered)
        self.assertIn("permanent deletion", lowered)
        self.assertIn("approval-bound", lowered)
        self.assertIn("no admin sdk", lowered)

    def test_no_guidance_or_next_action_ever_asks_for_a_secret_in_discord(self) -> None:
        progress = setup_progress(synthetic.config(), NOTHING_CONNECTED)
        texts = [provider_guidance(name).as_text() for name in PROVIDERS]
        texts.extend(item.next_action for item in progress)
        texts.extend(diagnose(name, kind) for name in PROVIDERS for kind in FAILURE_KINDS)
        for text in texts:
            with self.subTest(text=text[:60]):
                self.assertIsNone(_CREDENTIAL_REQUEST.search(text))


class IdentifierValidationTests(unittest.TestCase):
    def test_a_valid_identifier_is_accepted_and_returned_normalized(self) -> None:
        cases = (
            ("discord", "guild_id", f"  {synthetic.CLIENT_GUILD} ", synthetic.CLIENT_GUILD),
            ("trello", "board_id", "abc123def456ghi789jkl012", "abc123def456ghi789jkl012"),
            ("ghl", "location_id", "synthetic-location-1x", "synthetic-location-1x"),
            ("rentcast", "endpoint", "/v1/properties", "/v1/properties"),
            (
                "google_workspace",
                "account_email",
                "scotty.synthetic@example.invalid",
                "scotty.synthetic@example.invalid",
            ),
        )
        for provider, field, value, expected in cases:
            with self.subTest(provider=provider):
                self.assertEqual(validate_identifier(provider, field, value), expected)

    def test_a_malformed_identifier_is_refused_with_a_specific_correction(self) -> None:
        cases = (
            ("discord", "guild_id", "my server"),
            ("discord", "guild_id", "123"),
            ("trello", "board_id", "short"),
            ("ghl", "location_id", "has spaces here"),
            ("rentcast", "endpoint", "/v2/properties"),
            ("rentcast", "endpoint", "/v1/../secret"),
            ("google_workspace", "account_email", "not-an-email"),
            ("discord", "guild_id", ""),
            ("discord", "guild_id", 42),
        )
        for provider, field, value in cases:
            with self.subTest(provider=provider, value=value):
                with self.assertRaises(SetupFlowError) as caught:
                    validate_identifier(provider, field, value)
                self.assertGreater(len(str(caught.exception)), 20)

    def test_an_unknown_provider_or_field_is_refused_rather_than_invented(self) -> None:
        for provider, field in (
            ("zillow", "api_key"),
            ("trello", "api_key"),
            ("ghl", "private_token"),
            ("discord", "bot_token"),
            ("google_workspace", "refresh_token"),
        ):
            with self.subTest(provider=provider, field=field), self.assertRaises(SetupFlowError):
                validate_identifier(provider, field, "synthetic-value-0001")

    def test_no_collected_field_is_ever_a_credential(self) -> None:
        for provider, fields in REQUIRED_IDENTIFIERS.items():
            for item in fields:
                with self.subTest(provider=provider, field=item.field):
                    for forbidden in ("token", "key", "secret", "password", "credential"):
                        self.assertNotIn(forbidden, item.field)


class DiagnosisTests(unittest.TestCase):
    def test_each_failure_kind_names_a_cause_and_the_next_correction(self) -> None:
        for provider in PROVIDERS:
            for kind in FAILURE_KINDS:
                with self.subTest(provider=provider, kind=kind):
                    text = diagnose(provider, kind)
                    self.assertIn(provider_guidance(provider).display_name, text)
                    self.assertGreater(len(text), 60)

    def test_a_scope_failure_repeats_the_exact_scopes_to_grant(self) -> None:
        text = diagnose("google_workspace", "scope")
        self.assertIn("openid", text)
        self.assertIn("no Admin SDK", text)

    def test_a_callback_failure_explains_the_loopback_redirect(self) -> None:
        text = diagnose("google_workspace", "callback")
        self.assertIn("127.0.0.1", text)

    def test_an_unrecognised_failure_is_still_answered_usefully(self) -> None:
        text = diagnose("trello", "something-new")
        self.assertIn("Trello", text)
        self.assertIn("setup status", text)
        with self.assertRaises(SetupFlowError):
            diagnose("zillow", "authentication")


class ProgressAndResumeTests(unittest.TestCase):
    def test_progress_reports_every_provider_and_what_is_missing(self) -> None:
        progress = setup_progress(synthetic.config(), NOTHING_CONNECTED)
        self.assertEqual(tuple(item.provider for item in progress), PROVIDERS)
        google = next(item for item in progress if item.provider == "google_workspace")
        self.assertIn("Google Workspace account email", " ".join(google.missing))
        self.assertIn("Send Scotty", google.next_action)
        trello = next(item for item in progress if item.provider == "trello")
        self.assertEqual(trello.missing, ())
        self.assertIn("protected intake", trello.next_action)

    def test_resume_returns_the_first_unfinished_provider_in_fixed_order(self) -> None:
        config = synthetic.config()
        self.assertEqual(
            first_unfinished(setup_progress(config, NOTHING_CONNECTED)).provider, "discord"
        )
        partial = dict(NOTHING_CONNECTED, discord=True, trello=True)
        self.assertEqual(first_unfinished(setup_progress(config, partial)).provider, "ghl")

    def test_nothing_remains_once_every_provider_is_configured_and_connected(self) -> None:
        config = synthetic.config(google_workspace=GOOGLE_SCOPE)
        self.assertIsNone(first_unfinished(setup_progress(config, dict.fromkeys(PROVIDERS, True))))

    def test_a_staged_identifier_counts_as_collected_and_advances_the_resume_point(self) -> None:
        config = synthetic.config()
        connected = dict.fromkeys(PROVIDERS, True) | {"google_workspace": False}
        before = setup_progress(config, connected)
        google_before = next(item for item in before if item.provider == "google_workspace")
        self.assertEqual(first_unfinished(before).provider, "google_workspace")
        self.assertIn("Send Scotty", google_before.next_action)

        after = setup_progress(
            config,
            connected,
            staged={"google_workspace": {"account_email": "scotty.synthetic@example.invalid"}},
        )
        google = next(item for item in after if item.provider == "google_workspace")
        self.assertEqual(google.missing, ())
        self.assertIn("browser consent", google.next_action)
        # Consent is still outstanding, so the resume point does not move past it.
        self.assertEqual(first_unfinished(after).provider, "google_workspace")


class StagingStoreTests(unittest.TestCase):
    def store(self, directory: str) -> SetupStagingStore:
        return SetupStagingStore(Path(directory) / "setup-staging.json", owner_uid=os.getuid())

    def test_a_validated_identifier_is_staged_owner_only_and_read_back(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-staging-") as directory:
            store = self.store(directory)
            self.assertEqual(store.read(), {})
            store.stage("google_workspace", "account_email", "scotty.synthetic@example.invalid")
            store.stage("ghl", "location_id", "synthetic-location-1x")
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                store.read(),
                {
                    "ghl": {"location_id": "synthetic-location-1x"},
                    "google_workspace": {"account_email": "scotty.synthetic@example.invalid"},
                },
            )

    def test_staging_refuses_a_malformed_value_or_a_credential_shaped_field(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-staging-") as directory:
            store = self.store(directory)
            for provider, field, value in (
                ("google_workspace", "account_email", "not-an-email"),
                ("trello", "api_key", "synthetic-trello-key-0001"),
                ("discord", "bot_token", "synthetic-bot-token-0001"),
                ("zillow", "anything", "synthetic"),
            ):
                with (
                    self.subTest(provider=provider, field=field),
                    self.assertRaises(SetupFlowError),
                ):
                    store.stage(provider, field, value)
            self.assertEqual(store.read(), {})

    def test_unknown_or_invalid_staged_entries_are_ignored_on_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-staging-") as directory:
            store = self.store(directory)
            store.path.write_text(
                json.dumps(
                    {
                        "google_workspace": {
                            "account_email": "scotty.synthetic@example.invalid",
                            "refresh_token": "synthetic-refresh",
                        },
                        "zillow": {"api_key": "synthetic"},
                        "trello": "not an object",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                store.read(),
                {"google_workspace": {"account_email": "scotty.synthetic@example.invalid"}},
            )

    def test_a_symlinked_or_unreadable_staging_path_is_never_followed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-staging-") as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "staging.json"
            link.symlink_to(target)
            store = SetupStagingStore(link, owner_uid=os.getuid())
            self.assertEqual(store.read(), {})
            with self.assertRaises(SetupFlowError):
                store.stage("ghl", "location_id", "synthetic-location-1x")
            self.assertEqual(target.read_text(encoding="utf-8"), "{}")


class GuidedSetupThroughTheReadToolTests(unittest.TestCase):
    """The whole guided flow must be reachable from the bounded client tool."""

    def runtime(self, **secrets):
        from test_provider_connection import runtime

        return runtime(**secrets)

    def principal(self):
        from assistant.scotty_business.policy import Principal, Role

        return Principal(
            guild_id=synthetic.CLIENT_GUILD,
            channel_id=synthetic.OPERATOR_CHANNEL,
            user_id=synthetic.OPERATOR_USER,
            role=Role.MAIN_OPERATOR,
        )

    def read(self, runtime, **args):
        return runtime.handle_read(self.principal(), {"operation": "provider_setup", **args})

    def test_the_index_reports_progress_and_where_to_resume(self) -> None:
        with self.runtime(DISCORD_BOT_TOKEN="synthetic-discord") as runtime:
            result = self.read(runtime)
            self.assertEqual(set(result["providers"]), set(PROVIDERS))
            self.assertEqual([item["provider"] for item in result["progress"]], list(PROVIDERS))
            self.assertEqual(result["resume_at"], "trello")
            self.assertTrue(result["next_action"])

    def test_one_provider_answers_with_guidance_and_its_own_progress(self) -> None:
        with self.runtime(DISCORD_BOT_TOKEN="synthetic-discord") as runtime:
            result = self.read(runtime, provider="google_workspace")
            self.assertIn("Gmail API", result["apis"])
            self.assertIn("oauth2/callback", result["callback"])
            self.assertIn("the Google Workspace account email", result["missing_identifiers"])
            self.assertIn("Send Scotty", result["next_action"])

    def test_a_valid_identifier_is_accepted_and_a_bad_one_is_corrected(self) -> None:
        with self.runtime(DISCORD_BOT_TOKEN="synthetic-discord") as runtime:
            rejected = self.read(
                runtime,
                provider="google_workspace",
                setup_field="account_email",
                raw="not-an-email",
            )
            self.assertFalse(rejected["accepted"])
            self.assertIn("email", rejected["correction"])

            accepted = self.read(
                runtime,
                provider="google_workspace",
                setup_field="account_email",
                raw="scotty.synthetic@example.invalid",
            )
            self.assertTrue(accepted["accepted"])
            self.assertEqual(
                runtime.setup_staging.read(),
                {"google_workspace": {"account_email": "scotty.synthetic@example.invalid"}},
            )
            follow_up = self.read(runtime, provider="google_workspace")
            self.assertEqual(follow_up["missing_identifiers"], [])

    def test_a_credential_field_is_never_accepted_through_the_setup_tool(self) -> None:
        with self.runtime(DISCORD_BOT_TOKEN="synthetic-discord") as runtime:
            for provider, field in (
                ("trello", "api_key"),
                ("ghl", "private_token"),
                ("discord", "bot_token"),
            ):
                with self.subTest(provider=provider):
                    result = self.read(
                        runtime, provider=provider, setup_field=field, raw="synthetic-secret-0001"
                    )
                    self.assertFalse(result["accepted"])
            self.assertEqual(runtime.setup_staging.read(), {})

    def test_a_reported_failure_is_diagnosed_with_the_next_correction(self) -> None:
        with self.runtime(DISCORD_BOT_TOKEN="synthetic-discord") as runtime:
            result = self.read(runtime, provider="trello", setup_failure="scope")
            self.assertIn("missing a required scope", result["diagnosis"])
            self.assertTrue(result["next_action"])
            unknown = self.read(runtime, provider="trello", setup_failure="mystery")
            self.assertIn("setup status", unknown["diagnosis"])


if __name__ == "__main__":
    unittest.main()
