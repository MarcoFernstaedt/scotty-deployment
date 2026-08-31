from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import synthetic

from assistant.scotty_business.adapters.http import HttpResponse, ProviderError
from assistant.scotty_business.config import GOOGLE_OAUTH_SCOPES, ConfigError, RuntimeConfig
from assistant.scotty_business.policy import Role

GOOGLE_SCOPE = {
    "account_email": "scotty.synthetic@example.invalid",
    "oauth_scopes": list(GOOGLE_OAUTH_SCOPES),
}


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object, object, object]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers=None,
        query=None,
        json_body=None,
    ) -> HttpResponse:
        self.calls.append((method, url, query, json_body, headers))
        if url.endswith("/messages"):
            return HttpResponse(200, {}, {"messages": [{"id": "message-1"}]})
        if url.endswith("/files") and method == "GET":
            return HttpResponse(200, {}, {"files": [{"id": "file-1"}]})
        if "/documents/" in url:
            return HttpResponse(200, {}, {"documentId": "document-1"})
        if "/spreadsheets/" in url:
            return HttpResponse(200, {}, {"spreadsheetId": "spreadsheet-1"})
        if "/people/" in url or url.endswith("/people:createContact"):
            return HttpResponse(200, {}, {"resourceName": "people/contact-1", "etag": "etag-1"})
        if "/calendars/" in url:
            return HttpResponse(200, {}, {"id": "event-1"})
        return HttpResponse(200, {}, {"id": "resource-1", "etag": "etag-1"})


class GoogleConfigTests(unittest.TestCase):
    def test_google_authorizes_one_account_with_broad_product_scopes_not_resource_allowlists(
        self,
    ) -> None:
        config = RuntimeConfig.from_mapping(
            synthetic.private_mapping(google_workspace=GOOGLE_SCOPE)
        )
        assert config.google_workspace is not None
        self.assertEqual(config.google_workspace.account_email, "scotty.synthetic@example.invalid")
        self.assertEqual(set(config.google_workspace.oauth_scopes), set(GOOGLE_OAUTH_SCOPES))
        self.assertFalse(hasattr(config.google_workspace, "drive_file_ids"))
        self.assertFalse(hasattr(config.google_workspace, "gmail_label_ids"))
        self.assertIn("https://www.googleapis.com/auth/drive", GOOGLE_OAUTH_SCOPES)
        self.assertIn("https://www.googleapis.com/auth/calendar", GOOGLE_OAUTH_SCOPES)
        self.assertNotIn("https://mail.google.com/", GOOGLE_OAUTH_SCOPES)

    def test_google_rejects_scope_downgrade_or_unnecessary_permanent_mail_delete_scope(
        self,
    ) -> None:
        for scopes in (
            list(GOOGLE_OAUTH_SCOPES[:-1]),
            [*GOOGLE_OAUTH_SCOPES, "https://mail.google.com/"],
        ):
            malformed = dict(GOOGLE_SCOPE, oauth_scopes=scopes)
            with self.subTest(scopes=scopes), self.assertRaises(ConfigError):
                RuntimeConfig.from_mapping(synthetic.private_mapping(google_workspace=malformed))


class AdapterHarness(unittest.TestCase):
    def adapter(self):
        from assistant.scotty_business.adapters.google_workspace import GoogleWorkspaceAdapter

        config = RuntimeConfig.from_mapping(
            synthetic.private_mapping(google_workspace=GOOGLE_SCOPE)
        )
        assert config.google_workspace is not None
        transport = FakeTransport()
        return GoogleWorkspaceAdapter(
            transport,
            "synthetic-access-token",
            config.google_workspace,
        ), transport


class GoogleAdapterTests(AdapterHarness):
    def test_broad_account_owned_search_and_reads_do_not_require_pre_enumerated_ids(self) -> None:
        adapter, transport = self.adapter()
        self.assertEqual(
            adapter.search_gmail("from:customer", max_results=25)[0].source_id, "message-1"
        )
        self.assertEqual(
            adapter.search_drive("name contains 'closing'", max_results=25)[0].source_id, "file-1"
        )
        self.assertEqual(
            adapter.get_drive_file("previously-unknown-file").source_id, "previously-unknown-file"
        )
        self.assertEqual(
            adapter.get_document("previously-unknown-doc").source_id, "previously-unknown-doc"
        )
        self.assertEqual(
            adapter.get_spreadsheet("previously-unknown-sheet").source_id,
            "previously-unknown-sheet",
        )
        self.assertTrue(any(call[1].endswith("/files") for call in transport.calls))

    def test_routine_reversible_workspace_operations_are_available_without_proposal(self) -> None:
        adapter, _ = self.adapter()
        operations = (
            ("gmail_modify_labels", "message-1", {"addLabelIds": ["STARRED"]}),
            ("gmail_create_draft", "new", {"raw": "c3ludGhldGlj"}),
            ("calendar_create_event", "primary", {"summary": "Internal follow-up"}),
            ("calendar_update_event", "primary/event-1", {"summary": "Rescheduled"}),
            ("calendar_cancel_event", "primary/event-1", {}),
            ("drive_create_file", "new", {"name": "Internal notes"}),
            ("drive_update_file", "file-1", {"name": "Renamed"}),
            ("drive_move_file", "file-1", {"addParents": "folder-2", "removeParents": "folder-1"}),
            ("drive_trash_file", "file-1", {}),
            ("docs_create", "new", {"title": "Notes"}),
            ("docs_batch_update", "document-1", {"requests": [{"insertText": {}}]}),
            ("sheets_create", "new", {"properties": {"title": "Pipeline"}}),
            ("sheets_batch_update", "spreadsheet-1", {"requests": [{"addSheet": {}}]}),
            ("contacts_create", "new", {"names": [{"givenName": "Synthetic"}]}),
            ("contacts_update", "people/contact-1", {"etag": "etag-1", "names": []}),
        )
        for operation, resource_id, payload in operations:
            with self.subTest(operation=operation):
                self.assertTrue(adapter.execute_routine(operation, resource_id, payload).source_id)

    def test_consequence_operations_are_never_accepted_by_routine_path(self) -> None:
        adapter, _ = self.adapter()
        for operation in (
            "gmail_send_draft",
            "drive_delete_permanently",
            "drive_change_permissions",
            "contacts_delete",
            "admin_change_user",
            "credential_rotate",
            "bulk_mutation",
        ):
            with self.subTest(operation=operation), self.assertRaises(ProviderError):
                adapter.execute_routine(operation, "resource-1", {"value": True})

    def test_new_calendar_audience_is_consequence_gated_but_internal_edits_are_routine(
        self,
    ) -> None:
        from assistant.scotty_business.google_policy import (
            GoogleActionClass,
            classify_google_action,
        )

        self.assertEqual(
            classify_google_action("calendar_update_event", {"summary": "Moved"}),
            GoogleActionClass.ROUTINE,
        )
        self.assertEqual(
            classify_google_action(
                "calendar_update_event",
                {"attendees": [{"email": "new-audience@example.invalid"}]},
            ),
            GoogleActionClass.CONSEQUENCE,
        )


class GoogleConsequenceClassificationTests(unittest.TestCase):
    """Adversarial classification: bulk, oversize, nesting, and unknown actions."""

    def classify(self, operation, payload):
        from assistant.scotty_business.google_policy import classify_google_action

        return classify_google_action(operation, payload)

    def klass(self):
        from assistant.scotty_business.google_policy import GoogleActionClass

        return GoogleActionClass

    def test_a_bulk_target_list_is_consequence_gated_not_routine(self) -> None:
        klass = self.klass()
        for field in ("ids", "messageIds", "fileIds", "resourceNames", "permissions"):
            with self.subTest(field=field):
                self.assertEqual(
                    self.classify("gmail_modify_labels", {field: [f"id-{n}" for n in range(200)]}),
                    klass.CONSEQUENCE,
                )
                self.assertEqual(
                    self.classify("gmail_modify_labels", {field: ["id-1", "id-2"]}),
                    klass.ROUTINE,
                )

    def test_ordinary_single_resource_editing_stays_routine_at_any_edit_count(self) -> None:
        klass = self.klass()
        self.assertEqual(
            self.classify(
                "docs_batch_update", {"requests": [{"insertText": {}} for _ in range(400)]}
            ),
            klass.ROUTINE,
        )
        self.assertEqual(
            self.classify(
                "sheets_update_values",
                {"valueInputOption": "RAW", "data": [{"range": f"A{n}"} for n in range(80)]},
            ),
            klass.ROUTINE,
        )

    def test_a_nested_new_audience_is_still_consequence_gated(self) -> None:
        klass = self.klass()
        self.assertEqual(
            self.classify(
                "calendar_update_event",
                {"event": {"attendees": [{"email": "new@example.invalid"}]}},
            ),
            klass.CONSEQUENCE,
        )
        self.assertEqual(
            self.classify("calendar_update_event", {"event": {"summary": "Internal"}}),
            klass.ROUTINE,
        )

    def test_an_oversized_or_deeply_nested_payload_fails_closed(self) -> None:
        klass = self.klass()
        nested: dict[str, object] = {"summary": "deep"}
        for _ in range(40):
            nested = {"child": nested}
        self.assertEqual(self.classify("calendar_update_event", nested), klass.FORBIDDEN)
        self.assertEqual(
            self.classify("docs_batch_update", {"requests": [{"text": "x" * 400_000}]}),
            klass.FORBIDDEN,
        )

    def test_admin_security_billing_and_unknown_actions_stay_forbidden(self) -> None:
        klass = self.klass()
        for operation in (
            "admin_change_user",
            "admin_directory_delete",
            "credential_rotate",
            "billing_update",
            "gmail_send",
            "",
            "drive_delete",
            None,
            42,
        ):
            with self.subTest(operation=operation):
                self.assertEqual(self.classify(operation, {"value": True}), klass.FORBIDDEN)
        for payload in (None, [], "raw", 7):
            with self.subTest(payload=payload):
                self.assertEqual(self.classify("drive_update_file", payload), klass.FORBIDDEN)


class GoogleAdapterPayloadTests(AdapterHarness):
    """The adapter must refuse a payload the REST call would misapply."""

    def test_label_modification_accepts_only_bounded_label_fields(self) -> None:
        adapter, _ = self.adapter()
        for payload in (
            {},
            {"raw": "c3ludGhldGlj"},
            {"addLabelIds": "STARRED"},
            {"addLabelIds": ["STARRED"], "deleteAll": True},
            {"addLabelIds": [{"id": "STARRED"}]},
        ):
            with self.subTest(payload=payload), self.assertRaises(ProviderError):
                adapter.execute_routine("gmail_modify_labels", "message-1", payload)
        self.assertTrue(
            adapter.execute_routine(
                "gmail_modify_labels",
                "message-1",
                {"addLabelIds": ["STARRED"], "removeLabelIds": ["UNREAD"]},
            ).source_id
        )

    def test_contact_updates_name_only_known_person_fields(self) -> None:
        adapter, transport = self.adapter()
        with self.assertRaises(ProviderError):
            adapter.execute_routine(
                "contacts_update", "people/contact-1", {"etag": "etag-1", "notAField": []}
            )
        adapter.execute_routine(
            "contacts_update",
            "people/contact-1",
            {"etag": "etag-1", "names": [], "emailAddresses": []},
        )
        query = transport.calls[-1][2]
        assert query is not None
        self.assertEqual(query["updatePersonFields"], "names,emailAddresses")

    def test_a_bulk_or_forbidden_payload_never_reaches_the_transport(self) -> None:
        adapter, transport = self.adapter()
        before = len(transport.calls)
        for operation, payload in (
            ("gmail_modify_labels", {"ids": [f"m-{n}" for n in range(200)]}),
            ("drive_update_file", {"name": "x" * 400_000}),
            ("calendar_update_event", {"attendees": [{"email": "new@example.invalid"}]}),
        ):
            with self.subTest(operation=operation), self.assertRaises(ProviderError):
                adapter.execute_routine(operation, "primary/event-1", payload)
        self.assertEqual(len(transport.calls), before)


class GoogleTokenSafetyTests(unittest.TestCase):
    def test_token_store_is_owner_only_account_bound_and_never_renders_secrets(self) -> None:
        from assistant.scotty_business.google_oauth import GoogleTokenStore, OAuthToken

        with tempfile.TemporaryDirectory(prefix="scotty-google-token-") as directory:
            path = Path(directory) / "google-oauth.json"
            store = GoogleTokenStore(path, owner_uid=os.getuid(), owner_gid=os.getgid())
            token = OAuthToken(
                access_token="synthetic-access-secret",
                refresh_token="synthetic-refresh-secret",
                expires_at=4102444800,
                scopes=tuple(GOOGLE_SCOPE["oauth_scopes"]),
                account_email=GOOGLE_SCOPE["account_email"],
            )
            store.write(token)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.read(), token)
            self.assertTrue(
                store.ready(tuple(GOOGLE_SCOPE["oauth_scopes"]), GOOGLE_SCOPE["account_email"])
            )
            self.assertFalse(
                store.ready(tuple(GOOGLE_SCOPE["oauth_scopes"]), "other@example.invalid")
            )
            rendered = repr(store) + repr(token) + json.dumps(store.status())
            self.assertNotIn("synthetic-access-secret", rendered)
            self.assertNotIn("synthetic-refresh-secret", rendered)
            self.assertNotIn("authorization-code", rendered)


class GoogleTokenRefreshTests(unittest.TestCase):
    """A one-hour access token must refresh without a second browser consent."""

    def store(self, directory: str, *, expires_at: int):
        from assistant.scotty_business.google_oauth import GoogleTokenStore, OAuthToken

        path = Path(directory) / "google-oauth.json"
        store = GoogleTokenStore(path, owner_uid=os.getuid(), owner_gid=os.getgid())
        store.write(
            OAuthToken(
                access_token="synthetic-access-old",
                refresh_token="synthetic-refresh-secret",
                expires_at=expires_at,
                scopes=tuple(GOOGLE_SCOPE["oauth_scopes"]),
                account_email=GOOGLE_SCOPE["account_email"],
                client_id="synthetic-client-id",
                client_secret="synthetic-client-secret",
            )
        )
        return store

    def test_a_valid_access_token_is_reused_without_any_network_call(self) -> None:
        from assistant.scotty_business.google_oauth import ensure_access_token

        calls: list[object] = []
        with tempfile.TemporaryDirectory(prefix="scotty-google-refresh-") as directory:
            store = self.store(directory, expires_at=4102444800)
            token = ensure_access_token(
                store,
                tuple(GOOGLE_SCOPE["oauth_scopes"]),
                GOOGLE_SCOPE["account_email"],
                exchange=lambda **kwargs: calls.append(kwargs) or {},
            )
        self.assertEqual(token, "synthetic-access-old")
        self.assertEqual(calls, [])

    def test_an_expired_access_token_refreshes_and_is_persisted_owner_only(self) -> None:
        from assistant.scotty_business.google_oauth import ensure_access_token

        seen: list[dict[str, object]] = []

        def exchange(**kwargs: object) -> dict[str, object]:
            seen.append(kwargs)
            return {
                "access_token": "synthetic-access-new",
                "expires_in": 3600,
                "scope": " ".join(GOOGLE_SCOPE["oauth_scopes"]),
            }

        with tempfile.TemporaryDirectory(prefix="scotty-google-refresh-") as directory:
            store = self.store(directory, expires_at=1)
            token = ensure_access_token(
                store,
                tuple(GOOGLE_SCOPE["oauth_scopes"]),
                GOOGLE_SCOPE["account_email"],
                exchange=exchange,
            )
            stored = store.read()
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

        self.assertEqual(token, "synthetic-access-new")
        self.assertEqual(stored.access_token, "synthetic-access-new")
        self.assertEqual(stored.refresh_token, "synthetic-refresh-secret")
        self.assertEqual(len(seen), 1)
        self.assertNotIn("synthetic-access-new", repr(stored))

    def test_a_failed_or_out_of_scope_refresh_fails_closed_and_keeps_prior_state(self) -> None:
        from assistant.scotty_business.google_oauth import GoogleOAuthError, ensure_access_token

        def failing(**kwargs: object) -> dict[str, object]:
            raise GoogleOAuthError("Google OAuth refresh failed")

        def narrowed(**kwargs: object) -> dict[str, object]:
            return {"access_token": "narrow", "expires_in": 3600, "scope": "openid email"}

        def incomplete(**kwargs: object) -> dict[str, object]:
            return {"expires_in": 3600}

        for exchange in (failing, narrowed, incomplete):
            with (
                self.subTest(exchange=exchange.__name__),
                tempfile.TemporaryDirectory(prefix="scotty-google-refresh-") as directory,
            ):
                store = self.store(directory, expires_at=1)
                with self.assertRaises(GoogleOAuthError):
                    ensure_access_token(
                        store,
                        tuple(GOOGLE_SCOPE["oauth_scopes"]),
                        GOOGLE_SCOPE["account_email"],
                        exchange=exchange,
                    )
                self.assertEqual(store.read().access_token, "synthetic-access-old")

    def test_a_token_bound_to_another_account_never_refreshes(self) -> None:
        from assistant.scotty_business.google_oauth import GoogleOAuthError, ensure_access_token

        with tempfile.TemporaryDirectory(prefix="scotty-google-refresh-") as directory:
            store = self.store(directory, expires_at=1)
            with self.assertRaises(GoogleOAuthError):
                ensure_access_token(
                    store,
                    tuple(GOOGLE_SCOPE["oauth_scopes"]),
                    "someone-else@example.invalid",
                    exchange=lambda **kwargs: {},
                )

    def test_status_reports_expiry_without_revealing_any_secret(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-google-refresh-") as directory:
            store = self.store(directory, expires_at=1)
            status = store.status()
            self.assertTrue(status["configured"])
            self.assertFalse(status["access_valid"])
            self.assertTrue(status["refreshable"])
            rendered = json.dumps(status)
            for secret in (
                "synthetic-access-old",
                "synthetic-refresh-secret",
                "synthetic-client-secret",
            ):
                self.assertNotIn(secret, rendered)


class GoogleAdapterTokenProviderTests(unittest.TestCase):
    """The adapter must ask for a token per request so a refresh takes effect."""

    def test_each_request_uses_the_currently_valid_access_token(self) -> None:
        from assistant.scotty_business.adapters.google_workspace import GoogleWorkspaceAdapter

        config = RuntimeConfig.from_mapping(
            synthetic.private_mapping(google_workspace=GOOGLE_SCOPE)
        )
        assert config.google_workspace is not None
        transport = FakeTransport()
        issued = iter(["token-1", "token-2", "token-3"])
        adapter = GoogleWorkspaceAdapter(transport, lambda: next(issued), config.google_workspace)

        adapter.search_gmail("from:customer")
        adapter.search_gmail("from:customer")

        headers = [call[4] for call in transport.calls]
        self.assertEqual(
            [dict(header)["Authorization"] for header in headers],
            ["Bearer token-1", "Bearer token-2"],
        )
        self.assertNotIn("token-1", repr(headers[0]))

    def test_an_unavailable_token_provider_fails_before_any_request(self) -> None:
        from assistant.scotty_business.adapters.google_workspace import GoogleWorkspaceAdapter
        from assistant.scotty_business.google_oauth import GoogleOAuthError

        config = RuntimeConfig.from_mapping(
            synthetic.private_mapping(google_workspace=GOOGLE_SCOPE)
        )
        assert config.google_workspace is not None
        transport = FakeTransport()

        def unavailable() -> str:
            raise GoogleOAuthError("Google OAuth token state is unavailable")

        adapter = GoogleWorkspaceAdapter(transport, unavailable, config.google_workspace)
        with self.assertRaises(ProviderError):
            adapter.search_gmail("from:customer")
        self.assertEqual(transport.calls, [])

        with self.assertRaises(ProviderError):
            GoogleWorkspaceAdapter(transport, "", config.google_workspace)


class GoogleConsentCallbackTests(unittest.TestCase):
    """The loopback consent callback must not leak a code or hang on noise."""

    def parse(self, path: str, state: str):
        from assistant.scotty_business.google_oauth import parse_callback

        return parse_callback(path, state)

    def test_only_the_exact_callback_path_and_state_yields_a_code(self) -> None:
        self.assertEqual(
            self.parse("/oauth2/callback?state=s1&code=synthetic-code", "s1"),
            ("code", "synthetic-code"),
        )
        for path in (
            "/favicon.ico",
            "/",
            "/oauth2/callback",
            "/oauth2/callback?code=synthetic-code",
            "/oauth2/callback?state=other&code=synthetic-code",
            "/other?state=s1&code=synthetic-code",
        ):
            with self.subTest(path=path):
                self.assertIsNone(self.parse(path, "s1"))

    def test_a_denied_consent_is_reported_instead_of_timing_out(self) -> None:
        self.assertEqual(
            self.parse("/oauth2/callback?state=s1&error=access_denied", "s1"),
            ("error", "access_denied"),
        )

    def test_a_malformed_query_never_raises_out_of_the_handler(self) -> None:
        for path in ("/oauth2/callback?%%%", "/oauth2/callback?state", "?", ""):
            with self.subTest(path=path):
                self.assertIsNone(self.parse(path, "s1"))


class GoogleApprovalPolicyTests(unittest.TestCase):
    def test_employee_cannot_approve_google_consequences_but_operator_can(self) -> None:
        from assistant.scotty_business.policy import can_approve

        config = synthetic.config(google_workspace=GOOGLE_SCOPE)
        employee = config.principal_for(Role.EMPLOYEE)
        operator = config.principal_for(Role.MAIN_OPERATOR)
        self.assertFalse(can_approve(employee, "google_workspace_consequence"))
        self.assertTrue(can_approve(operator, "google_workspace_consequence"))

    def test_existing_five_tool_inventory_exposes_routine_and_consequence_google_paths(
        self,
    ) -> None:
        from assistant.scotty_business import _PROPOSE_SCHEMA, _READ_SCHEMA

        read_properties = _READ_SCHEMA["parameters"]["properties"]
        propose_properties = _PROPOSE_SCHEMA["parameters"]["properties"]
        self.assertIn("google_workspace", read_properties["operation"]["enum"])
        self.assertIn("google_operation", read_properties)
        self.assertIn("gmail_modify_labels", read_properties["google_operation"]["enum"])
        self.assertIn("drive_trash_file", read_properties["google_operation"]["enum"])
        self.assertIn("gmail_send_draft", propose_properties["google_operation"]["enum"])
        self.assertIn("drive_delete_permanently", propose_properties["google_operation"]["enum"])


if __name__ == "__main__":
    unittest.main()
