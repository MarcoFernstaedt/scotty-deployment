from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import synthetic

from assistant.scotty_business.adapters.http import HttpResponse, ProviderError
from assistant.scotty_business.config import ConfigError, GOOGLE_OAUTH_SCOPES, RuntimeConfig
from assistant.scotty_business.policy import Role

GOOGLE_SCOPE = {
    "account_email": "scotty.synthetic@example.invalid",
    "oauth_scopes": list(GOOGLE_OAUTH_SCOPES),
}


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object, object]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers=None,
        query=None,
        json_body=None,
    ) -> HttpResponse:
        self.calls.append((method, url, query, json_body))
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
    def test_google_authorizes_one_account_with_broad_product_scopes_not_resource_allowlists(self) -> None:
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

    def test_google_rejects_scope_downgrade_or_unnecessary_permanent_mail_delete_scope(self) -> None:
        for scopes in (
            list(GOOGLE_OAUTH_SCOPES[:-1]),
            [*GOOGLE_OAUTH_SCOPES, "https://mail.google.com/"],
        ):
            malformed = dict(GOOGLE_SCOPE, oauth_scopes=scopes)
            with self.subTest(scopes=scopes), self.assertRaises(ConfigError):
                RuntimeConfig.from_mapping(
                    synthetic.private_mapping(google_workspace=malformed)
                )


class GoogleAdapterTests(unittest.TestCase):
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

    def test_broad_account_owned_search_and_reads_do_not_require_pre_enumerated_ids(self) -> None:
        adapter, transport = self.adapter()
        self.assertEqual(adapter.search_gmail("from:customer", max_results=25)[0].source_id, "message-1")
        self.assertEqual(adapter.search_drive("name contains 'closing'", max_results=25)[0].source_id, "file-1")
        self.assertEqual(adapter.get_drive_file("previously-unknown-file").source_id, "previously-unknown-file")
        self.assertEqual(adapter.get_document("previously-unknown-doc").source_id, "previously-unknown-doc")
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

    def test_new_calendar_audience_is_consequence_gated_but_internal_edits_are_routine(self) -> None:
        from assistant.scotty_business.google_policy import GoogleActionClass, classify_google_action

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


class GoogleApprovalPolicyTests(unittest.TestCase):
    def test_employee_cannot_approve_google_consequences_but_operator_can(self) -> None:
        from assistant.scotty_business.policy import can_approve

        config = synthetic.config(google_workspace=GOOGLE_SCOPE)
        employee = config.principal_for(Role.EMPLOYEE)
        operator = config.principal_for(Role.MAIN_OPERATOR)
        self.assertFalse(can_approve(employee, "google_workspace_consequence"))
        self.assertTrue(can_approve(operator, "google_workspace_consequence"))

    def test_existing_five_tool_inventory_exposes_routine_and_consequence_google_paths(self) -> None:
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
