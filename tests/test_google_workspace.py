from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import synthetic
from synthetic_google import SyntheticGoogle

from assistant.scotty_business.adapters.http import HttpResponse, ProviderError
from assistant.scotty_business.config import GOOGLE_OAUTH_SCOPES, ConfigError, RuntimeConfig
from assistant.scotty_business.policy import Role

GOOGLE_SCOPE = {
    "account_email": "scotty.synthetic@example.invalid",
    "oauth_scopes": list(GOOGLE_OAUTH_SCOPES),
}
EMPLOYEE_SCOPE = {
    "account_email": "employee.synthetic@example.invalid",
    "oauth_scopes": list(GOOGLE_OAUTH_SCOPES),
}
#: Each client user connects their own Workspace account.
GOOGLE_ACCOUNTS = {"main_operator": GOOGLE_SCOPE, "employee": EMPLOYEE_SCOPE}


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
            synthetic.private_mapping(google_workspace=GOOGLE_ACCOUNTS)
        )
        assert config.google_for(Role.MAIN_OPERATOR) is not None
        self.assertEqual(
            config.google_for(Role.MAIN_OPERATOR).account_email, "scotty.synthetic@example.invalid"
        )
        self.assertEqual(
            set(config.google_for(Role.MAIN_OPERATOR).oauth_scopes), set(GOOGLE_OAUTH_SCOPES)
        )
        self.assertFalse(hasattr(config.google_for(Role.MAIN_OPERATOR), "drive_file_ids"))
        self.assertFalse(hasattr(config.google_for(Role.MAIN_OPERATOR), "gmail_label_ids"))
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
                RuntimeConfig.from_mapping(
                    synthetic.private_mapping(google_workspace={"main_operator": malformed})
                )


class AdapterHarness(unittest.TestCase):
    def adapter(self):
        from assistant.scotty_business.adapters.google_workspace import GoogleWorkspaceAdapter

        config = RuntimeConfig.from_mapping(
            synthetic.private_mapping(google_workspace=GOOGLE_ACCOUNTS)
        )
        assert config.google_for(Role.MAIN_OPERATOR) is not None
        transport = FakeTransport()
        return GoogleWorkspaceAdapter(
            transport,
            "synthetic-access-token",
            config.google_for(Role.MAIN_OPERATOR),
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
        adapter.transport = SyntheticGoogle()
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
        for field in ("ids", "messageIds", "fileIds", "resourceNames"):
            with self.subTest(field=field):
                self.assertEqual(
                    self.classify("gmail_modify_labels", {field: [f"id-{n}" for n in range(200)]}),
                    klass.CONSEQUENCE,
                )
                self.assertEqual(
                    self.classify("gmail_modify_labels", {field: ["id-1", "id-2"]}),
                    klass.ROUTINE,
                )

    def test_a_permissions_list_is_gated_at_any_size(self) -> None:
        """Sharing is never a question of volume, so no small list is routine."""

        klass = self.klass()
        for entries in (["id-1"], [f"id-{n}" for n in range(200)]):
            with self.subTest(count=len(entries)):
                self.assertEqual(
                    self.classify("drive_update_file", {"permissions": entries}),
                    klass.CONSEQUENCE,
                )

    def test_a_small_bounded_edit_stays_routine_up_to_its_limit(self) -> None:
        from assistant.scotty_business.google_policy import (
            MAX_DOCS_REQUESTS,
            MAX_SHEETS_RANGES,
            MAX_SHEETS_REQUESTS,
        )

        klass = self.klass()
        for operation, payload in (
            (
                "docs_batch_update",
                {"requests": [{"insertText": {}} for _ in range(MAX_DOCS_REQUESTS)]},
            ),
            (
                "sheets_batch_update",
                {"requests": [{"updateCells": {}} for _ in range(MAX_SHEETS_REQUESTS)]},
            ),
            (
                "sheets_update_values",
                {
                    "valueInputOption": "RAW",
                    "data": [{"range": f"A{n}"} for n in range(MAX_SHEETS_RANGES)],
                },
            ),
        ):
            with self.subTest(operation=operation):
                self.assertEqual(self.classify(operation, payload), klass.ROUTINE)

    def test_one_unit_past_the_limit_becomes_consequence_gated(self) -> None:
        from assistant.scotty_business.google_policy import (
            MAX_DOCS_REQUESTS,
            MAX_SHEETS_CELLS,
            MAX_SHEETS_RANGES,
            MAX_SHEETS_REQUESTS,
        )

        klass = self.klass()
        oversized_rows = [["x"] for _ in range(MAX_SHEETS_CELLS + 1)]
        for operation, payload in (
            (
                "docs_batch_update",
                {"requests": [{"insertText": {}} for _ in range(MAX_DOCS_REQUESTS + 1)]},
            ),
            (
                "sheets_batch_update",
                {"requests": [{"updateCells": {}} for _ in range(MAX_SHEETS_REQUESTS + 1)]},
            ),
            (
                "sheets_update_values",
                {
                    "valueInputOption": "RAW",
                    "data": [{"range": f"A{n}"} for n in range(MAX_SHEETS_RANGES + 1)],
                },
            ),
            (
                "sheets_update_values",
                {
                    "valueInputOption": "RAW",
                    "data": [{"range": "A1:A", "values": oversized_rows}],
                },
            ),
        ):
            with self.subTest(operation=operation, units=len(str(payload))):
                self.assertEqual(self.classify(operation, payload), klass.CONSEQUENCE)

    def test_a_destructive_request_inside_a_batch_is_consequence_gated(self) -> None:
        klass = self.klass()
        for operation, request in (
            ("docs_batch_update", {"deleteContentRange": {"range": {}}}),
            ("docs_batch_update", {"deleteTableRow": {}}),
            ("sheets_batch_update", {"deleteSheet": {"sheetId": 0}}),
            ("sheets_batch_update", {"deleteDimension": {}}),
            ("sheets_batch_update", {"deleteRange": {}}),
        ):
            with self.subTest(request=next(iter(request))):
                self.assertEqual(
                    self.classify(operation, {"requests": [request]}), klass.CONSEQUENCE
                )

    def test_a_routine_write_that_touches_permissions_is_consequence_gated(self) -> None:
        klass = self.klass()
        for operation, payload in (
            ("drive_update_file", {"permissions": [{"role": "reader", "type": "anyone"}]}),
            ("drive_create_file", {"name": "notes", "permissionIds": ["anyone"]}),
            ("drive_update_file", {"metadata": {"permissions": [{"type": "anyone"}]}}),
            ("drive_update_file", {"writersCanShare": True}),
        ):
            with self.subTest(operation=operation):
                self.assertEqual(self.classify(operation, payload), klass.CONSEQUENCE)

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
        adapter.transport = SyntheticGoogle()
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
        adapter, _ = self.adapter()
        transport = SyntheticGoogle()
        adapter.transport = transport
        with self.assertRaises(ProviderError):
            adapter.execute_routine(
                "contacts_update", "people/contact-1", {"etag": "etag-1", "notAField": []}
            )
        adapter.execute_routine(
            "contacts_update",
            "people/contact-1",
            {"etag": "etag-1", "names": [], "emailAddresses": []},
        )
        update = next(url for method, url in transport.calls if method == "PATCH")
        self.assertIn("updateContact", update)

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


class GoogleRequestShapeTests(AdapterHarness):
    """The REST calls must be ones Google actually accepts."""

    def test_an_empty_search_result_is_empty_not_an_error(self) -> None:
        from assistant.scotty_business.adapters.google_workspace import GoogleWorkspaceAdapter
        from assistant.scotty_business.adapters.http import HttpResponse

        class EmptyTransport:
            def request(self, method, url, *, headers=None, query=None, json_body=None):
                # Gmail and People omit the collection entirely on zero results.
                return HttpResponse(200, {}, {"resultSizeEstimate": 0})

        config = RuntimeConfig.from_mapping(
            synthetic.private_mapping(google_workspace=GOOGLE_ACCOUNTS)
        )
        assert config.google_for(Role.MAIN_OPERATOR) is not None
        adapter = GoogleWorkspaceAdapter(
            EmptyTransport(), "synthetic-access-token", config.google_for(Role.MAIN_OPERATOR)
        )
        self.assertEqual(adapter.search_gmail("from:nobody"), ())
        self.assertEqual(adapter.list_contacts(), ())

    def test_a_malformed_collection_is_still_refused(self) -> None:
        from assistant.scotty_business.adapters.google_workspace import GoogleWorkspaceAdapter
        from assistant.scotty_business.adapters.http import HttpResponse

        class MalformedTransport:
            def request(self, method, url, *, headers=None, query=None, json_body=None):
                return HttpResponse(200, {}, {"messages": "not a list"})

        config = RuntimeConfig.from_mapping(
            synthetic.private_mapping(google_workspace=GOOGLE_ACCOUNTS)
        )
        assert config.google_for(Role.MAIN_OPERATOR) is not None
        adapter = GoogleWorkspaceAdapter(
            MalformedTransport(), "synthetic-access-token", config.google_for(Role.MAIN_OPERATOR)
        )
        with self.assertRaises(ProviderError):
            adapter.search_gmail("from:nobody")

    def test_a_multi_word_calendar_search_is_sent_not_rejected(self) -> None:
        adapter, transport = self.adapter()
        adapter.list_calendar_events("primary", query="closing walkthrough Tuesday")
        query = transport.calls[-1][2]
        assert query is not None
        self.assertEqual(query["q"], "closing walkthrough Tuesday")

    def test_boolean_parameters_are_sent_in_the_form_google_accepts(self) -> None:
        adapter, transport = self.adapter()
        adapter.list_calendar_events("primary")
        adapter.get_spreadsheet("sheet-1")
        for call in transport.calls:
            query = call[2] or {}
            for key, value in query.items():
                with self.subTest(key=key):
                    self.assertNotIsInstance(value, bool)
        self.assertEqual(transport.calls[0][2]["singleEvents"], "true")
        self.assertEqual(transport.calls[-1][2]["includeGridData"], "false")


class BoundedGoogleReadTests(unittest.TestCase):
    """Drive content and Sheets values must be typed, validated, and bounded."""

    def adapter(self, routes):
        from assistant.scotty_business.adapters.google_workspace import GoogleWorkspaceAdapter
        from assistant.scotty_business.adapters.http import HttpResponse

        calls: list[tuple[str, object, bool]] = []

        class Transport:
            def request(self, method, url, *, headers=None, query=None, json_body=None, text=False):
                calls.append((url, query, text))
                for suffix, response in routes.items():
                    if suffix in url:
                        return response
                return HttpResponse(200, {}, {})

        config = RuntimeConfig.from_mapping(
            synthetic.private_mapping(google_workspace=GOOGLE_ACCOUNTS)
        )
        assert config.google_for(Role.MAIN_OPERATOR) is not None
        return (
            GoogleWorkspaceAdapter(
                Transport(), "synthetic-access-token", config.google_for(Role.MAIN_OPERATOR)
            ),
            calls,
        )

    def metadata(self, mime, size=None):
        from assistant.scotty_business.adapters.http import HttpResponse

        body = {"id": "file-1", "name": "Notes", "mimeType": mime, "etag": "etag-1"}
        if size is not None:
            body["size"] = size
        return HttpResponse(200, {}, body)

    def test_a_google_native_document_is_exported_to_bounded_text(self) -> None:
        from assistant.scotty_business.adapters.http import HttpResponse

        adapter, calls = self.adapter(
            {
                "/files/file-1/export": HttpResponse(200, {}, "synthetic body"),
                "/files/file-1": self.metadata("application/vnd.google-apps.document"),
            }
        )
        record = adapter.read_drive_file("file-1")
        self.assertEqual(record.fields["text"], "synthetic body")
        self.assertEqual(record.fields["readAs"], "text/plain")
        export = next(call for call in calls if "export" in call[0])
        self.assertEqual(export[1]["mimeType"], "text/plain")
        self.assertTrue(export[2], "an export must be read as text, not parsed as JSON")

    def test_a_stored_text_file_is_downloaded_with_alt_media(self) -> None:
        from assistant.scotty_business.adapters.http import HttpResponse

        adapter, calls = self.adapter({"/files/file-1": self.metadata("text/csv", size="12")})
        # The metadata read and the content read share a URL prefix, so the
        # content route is resolved by the alt=media query rather than the path.
        adapter.transport = _SequencedTransport(
            [self.metadata("text/csv", size="12"), HttpResponse(200, {}, "a,b\n1,2")]
        )
        record = adapter.read_drive_file("file-1")
        self.assertEqual(record.fields["text"], "a,b\n1,2")
        self.assertEqual(adapter.transport.calls[-1][1]["alt"], "media")

    def test_an_unsupported_file_type_is_refused_explicitly(self) -> None:
        from assistant.scotty_business.adapters.http import ProviderError

        for mime in ("image/png", "application/pdf", "application/octet-stream", ""):
            with self.subTest(mime=mime):
                adapter, _ = self.adapter({"/files/file-1": self.metadata(mime)})
                with self.assertRaises(ProviderError):
                    adapter.read_drive_file("file-1")

    def test_an_oversize_file_is_refused_before_it_is_downloaded(self) -> None:
        from assistant.scotty_business.adapters.google_workspace import MAX_DRIVE_TEXT_BYTES
        from assistant.scotty_business.adapters.http import ProviderError

        adapter, calls = self.adapter(
            {"/files/file-1": self.metadata("text/plain", size=str(MAX_DRIVE_TEXT_BYTES + 1))}
        )
        with self.assertRaises(ProviderError):
            adapter.read_drive_file("file-1")
        self.assertEqual(len(calls), 1, "only the metadata read may happen")

    def test_an_oversize_body_is_refused_even_when_metadata_understated_it(self) -> None:
        from assistant.scotty_business.adapters.google_workspace import MAX_DRIVE_TEXT_BYTES
        from assistant.scotty_business.adapters.http import HttpResponse, ProviderError

        adapter, _ = self.adapter({})
        adapter.transport = _SequencedTransport(
            [
                self.metadata("text/plain", size="10"),
                HttpResponse(200, {}, "x" * (MAX_DRIVE_TEXT_BYTES + 1)),
            ]
        )
        with self.assertRaises(ProviderError):
            adapter.read_drive_file("file-1")

    def test_a_malformed_content_or_metadata_response_is_refused(self) -> None:
        from assistant.scotty_business.adapters.http import HttpResponse, ProviderError

        adapter, _ = self.adapter({})
        adapter.transport = _SequencedTransport(
            [self.metadata("text/plain"), HttpResponse(200, {}, {"not": "text"})]
        )
        with self.assertRaises(ProviderError):
            adapter.read_drive_file("file-1")

        other, _ = self.adapter({"/files/file-1": HttpResponse(200, {}, {"id": "file-1"})})
        with self.assertRaises(ProviderError):
            other.read_drive_file("file-1")

    def test_one_validated_range_of_values_is_read(self) -> None:
        from assistant.scotty_business.adapters.http import HttpResponse

        adapter, calls = self.adapter(
            {
                "/values/": HttpResponse(
                    200, {}, {"range": "Sheet1!A1:B2", "values": [["a", "b"], ["1", "2"]]}
                )
            }
        )
        record = adapter.get_sheet_values("sheet-1", "Sheet1!A1:B2")
        self.assertEqual(record.fields["values"], [["a", "b"], ["1", "2"]])
        self.assertIn("Sheet1%21A1%3AB2", calls[-1][0])

    def test_a_malformed_range_never_reaches_the_transport(self) -> None:
        from assistant.scotty_business.adapters.http import ProviderError

        adapter, calls = self.adapter({})
        for bad in ("", "A1; DROP", "Sheet1!A1:B2:C3", "../secret", "A" * 300, 7, None):
            with self.subTest(range=bad), self.assertRaises(ProviderError):
                adapter.get_sheet_values("sheet-1", bad)
        self.assertEqual(calls, [])

    def test_a_bounded_batch_of_ranges_is_read_and_an_unbounded_one_refused(self) -> None:
        from assistant.scotty_business.adapters.google_workspace import MAX_SHEETS_READ_RANGES
        from assistant.scotty_business.adapters.http import HttpResponse, ProviderError

        adapter, calls = self.adapter(
            {
                "values:batchGet": HttpResponse(
                    200,
                    {},
                    {"valueRanges": [{"range": "Sheet1!A1", "values": [["a"]]}]},
                )
            }
        )
        record = adapter.batch_get_sheet_values("sheet-1", ["Sheet1!A1", "Sheet1!B1"])
        self.assertEqual(len(record.fields["valueRanges"]), 1)
        self.assertEqual(calls[-1][1]["ranges"], ["Sheet1!A1", "Sheet1!B1"])

        for ranges in ([], [f"A{n}" for n in range(MAX_SHEETS_READ_RANGES + 1)], "A1", None):
            with self.subTest(ranges=ranges), self.assertRaises(ProviderError):
                adapter.batch_get_sheet_values("sheet-1", ranges)

    def test_an_oversize_or_malformed_values_response_is_refused(self) -> None:
        from assistant.scotty_business.adapters.google_workspace import MAX_SHEETS_READ_CELLS
        from assistant.scotty_business.adapters.http import HttpResponse, ProviderError

        huge = [["x"] for _ in range(MAX_SHEETS_READ_CELLS + 1)]
        for body in (
            {"range": "A1", "values": huge},
            {"range": "A1", "values": "not a list"},
            ["not", "an", "object"],
        ):
            with self.subTest(body=str(body)[:30]):
                adapter, _ = self.adapter({"/values/": HttpResponse(200, {}, body)})
                with self.assertRaises(ProviderError):
                    adapter.get_sheet_values("sheet-1", "A1")

        adapter, _ = self.adapter(
            {"values:batchGet": HttpResponse(200, {}, {"valueRanges": "not a list"})}
        )
        with self.assertRaises(ProviderError):
            adapter.batch_get_sheet_values("sheet-1", ["A1"])


class _SequencedTransport:
    """Returns scripted responses in order, recording each call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, object]] = []

    def request(self, method, url, *, headers=None, query=None, json_body=None, text=False):
        self.calls.append((url, query or {}))
        return self.responses.pop(0)


class GoogleScopeNormalizationTests(unittest.TestCase):
    """Google expands the openid shorthand scopes in what it returns."""

    def test_the_expanded_scope_form_google_returns_is_accepted(self) -> None:
        from assistant.scotty_business.google_oauth import canonical_scopes

        configured = canonical_scopes(GOOGLE_OAUTH_SCOPES)
        granted = canonical_scopes(
            (
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/documents",
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/contacts",
            )
        )
        self.assertEqual(configured, granted)

    def test_a_genuinely_narrower_grant_is_still_refused(self) -> None:
        from assistant.scotty_business.google_oauth import canonical_scopes

        narrowed = canonical_scopes(("openid", "https://www.googleapis.com/auth/userinfo.email"))
        self.assertNotEqual(canonical_scopes(GOOGLE_OAUTH_SCOPES), narrowed)

    def test_a_stored_token_in_the_expanded_form_still_reads_as_ready(self) -> None:
        from assistant.scotty_business.google_oauth import GoogleTokenStore, OAuthToken

        expanded = tuple(
            "https://www.googleapis.com/auth/userinfo.email" if scope == "email" else scope
            for scope in GOOGLE_OAUTH_SCOPES
        )
        with tempfile.TemporaryDirectory(prefix="scotty-google-scope-") as directory:
            path = Path(directory) / "google-oauth.json"
            store = GoogleTokenStore(path, owner_uid=os.getuid(), owner_gid=os.getgid())
            store.write(
                OAuthToken(
                    access_token="synthetic-access",
                    refresh_token="synthetic-refresh",
                    expires_at=4102444800,
                    scopes=expanded,
                    account_email=GOOGLE_SCOPE["account_email"],
                    client_id="synthetic-client-id",
                    client_secret="synthetic-client-secret",
                )
            )
            self.assertTrue(store.ready(GOOGLE_OAUTH_SCOPES, GOOGLE_SCOPE["account_email"]))


class AuthoritativeReadbackTests(unittest.TestCase):
    """A mutation is verified by an independent read, never by its own reply."""

    def adapter(self):
        from assistant.scotty_business.adapters.google_workspace import GoogleWorkspaceAdapter

        config = RuntimeConfig.from_mapping(
            synthetic.private_mapping(google_workspace=GOOGLE_ACCOUNTS)
        )
        assert config.google_for(Role.MAIN_OPERATOR) is not None
        google = SyntheticGoogle()
        return GoogleWorkspaceAdapter(
            google, "synthetic-access-token", config.google_for(Role.MAIN_OPERATOR)
        ), google

    def test_every_mutation_reads_the_resource_back_before_reporting_success(self) -> None:
        adapter, google = self.adapter()
        adapter.execute_routine("drive_update_file", "file-1", {"name": "Renamed"})
        methods = [method for method, _ in google.calls]
        self.assertEqual(methods, ["PATCH", "GET"])
        self.assertEqual(google.files["file-1"]["name"], "Renamed")

    def test_a_readback_that_disagrees_is_unverified_not_success(self) -> None:
        from assistant.scotty_business.adapters.http import AmbiguousEffectError

        adapter, google = self.adapter()
        google.readback_status = 200
        google.readback_body = {"id": "file-1", "name": "Something else"}
        with self.assertRaises(AmbiguousEffectError) as caught:
            adapter.execute_routine("drive_update_file", "file-1", {"name": "Renamed"})
        self.assertIn("reconcile before retry", str(caught.exception))

    def test_an_absent_resource_after_a_write_is_unverified(self) -> None:
        from assistant.scotty_business.adapters.http import AmbiguousEffectError

        adapter, google = self.adapter()
        google.readback_status = 404
        google.readback_body = {}
        with self.assertRaises(AmbiguousEffectError):
            adapter.execute_routine("drive_update_file", "file-1", {"name": "Renamed"})

    def test_an_unavailable_or_malformed_readback_is_unverified(self) -> None:
        from assistant.scotty_business.adapters.http import AmbiguousEffectError

        for status, body in ((500, {}), (429, {}), (200, "not an object"), (200, None)):
            with self.subTest(status=status):
                adapter, google = self.adapter()
                google.readback_status = status
                google.readback_body = body
                with self.assertRaises(AmbiguousEffectError):
                    adapter.execute_routine("drive_update_file", "file-1", {"name": "Renamed"})

    def test_a_partly_applied_batch_is_unverified_even_if_the_resource_exists(self) -> None:
        from assistant.scotty_business.adapters.http import AmbiguousEffectError

        for operation, resource in (
            ("docs_batch_update", "document-1"),
            ("sheets_batch_update", "spreadsheet-1"),
        ):
            with self.subTest(operation=operation):
                adapter, google = self.adapter()
                google.drop_batch_replies = 1
                with self.assertRaises(AmbiguousEffectError) as caught:
                    adapter.execute_routine(
                        operation, resource, {"requests": [{"insertText": {}}, {"insertText": {}}]}
                    )
                self.assertIn("only part", str(caught.exception))

    def test_a_fully_applied_batch_verifies(self) -> None:
        adapter, google = self.adapter()
        record = adapter.execute_routine(
            "docs_batch_update", "document-1", {"requests": [{"insertText": {}}]}
        )
        self.assertEqual(record.source_id, "document-1")

    def test_a_write_whose_response_never_arrives_is_unverified(self) -> None:
        from assistant.scotty_business.adapters.http import AmbiguousEffectError

        class TimingOutTransport(SyntheticGoogle):
            def request(self, method, url, **kwargs):
                if method in {"POST", "PATCH", "PUT", "DELETE"}:
                    # The effect may already have landed on the provider side.
                    raise AmbiguousEffectError(
                        "provider mutation outcome is unknown; reconcile before any retry"
                    )
                return super().request(method, url, **kwargs)

        adapter, _ = self.adapter()
        adapter.transport = TimingOutTransport()
        with self.assertRaises(AmbiguousEffectError):
            adapter.execute_routine("drive_update_file", "file-1", {"name": "Renamed"})

    def test_reconciliation_after_an_unverified_write_reads_the_true_state(self) -> None:
        from assistant.scotty_business.adapters.http import AmbiguousEffectError

        adapter, google = self.adapter()
        google.readback_status = 500
        google.readback_body = {}
        with self.assertRaises(AmbiguousEffectError):
            adapter.execute_routine("drive_update_file", "file-1", {"name": "Renamed"})
        # The write did land. Reconciling shows that, so the caller must not
        # repeat the mutation blindly.
        self.assertEqual(adapter.get_drive_file("file-1").fields["name"], "Renamed")

    def test_an_operation_with_no_authoritative_readback_is_never_verified(self) -> None:
        from assistant.scotty_business.google_readback import ReadbackStatus, verify

        self.assertEqual(
            verify(None, 200, {"id": "x"}, fully_applied=None), ReadbackStatus.UNSUPPORTED
        )

    def test_a_deletion_verifies_only_when_the_resource_is_really_gone(self) -> None:
        from assistant.scotty_business.adapters.http import AmbiguousEffectError

        adapter, google = self.adapter()
        adapter.mutate("drive_delete_permanently", "file-1", {})
        self.assertNotIn("file-1", google.files)

        other, other_google = self.adapter()
        other_google.readback_status = 200
        other_google.readback_body = {"id": "file-1"}
        with self.assertRaises(AmbiguousEffectError) as caught:
            other.mutate("drive_delete_permanently", "file-1", {})
        self.assertIn("still present", str(caught.exception))

    def test_a_cancelled_event_verifies_by_its_status(self) -> None:
        adapter, google = self.adapter()
        created = adapter.execute_routine(
            "calendar_create_event", "primary", {"summary": "Internal sync"}
        )
        adapter.execute_routine("calendar_cancel_event", f"primary/{created.source_id}", {})
        self.assertEqual(google.events[("primary", created.source_id)]["status"], "cancelled")

    def test_a_removal_is_verified_only_when_the_value_is_really_gone(self) -> None:
        """Subset matching proves an addition. Only absence proves a removal."""

        from assistant.scotty_business.adapters.http import AmbiguousEffectError

        adapter, google = self.adapter()
        adapter.execute_routine(
            "gmail_modify_labels",
            "message-1",
            {"addLabelIds": ["Label_1"], "removeLabelIds": ["UNREAD"]},
        )
        self.assertNotIn("UNREAD", google.messages["message-1"]["labelIds"])

        # A provider that acknowledges the removal but keeps the label is the
        # exact case a present-only comparison would call verified.
        stubborn, google = self.adapter()
        google.readback_status = 200
        google.readback_body = {"id": "message-1", "labelIds": ["INBOX", "UNREAD", "Label_1"]}
        with self.assertRaises(AmbiguousEffectError):
            stubborn.execute_routine(
                "gmail_modify_labels",
                "message-1",
                {"addLabelIds": ["Label_1"], "removeLabelIds": ["UNREAD"]},
            )

    def test_a_move_is_verified_only_when_the_old_parent_is_gone(self) -> None:
        from assistant.scotty_business.adapters.http import AmbiguousEffectError

        adapter, google = self.adapter()
        google.files["file-1"]["parents"] = ["folder-old"]
        adapter.execute_routine(
            "drive_move_file",
            "file-1",
            {"addParents": "folder-new", "removeParents": "folder-old"},
        )
        self.assertEqual(google.files["file-1"]["parents"], ["folder-new"])

        copied, google = self.adapter()
        google.readback_status = 200
        # A copy rather than a move: the file is in both places.
        google.readback_body = {"id": "file-1", "parents": ["folder-old", "folder-new"]}
        with self.assertRaises(AmbiguousEffectError):
            copied.execute_routine(
                "drive_move_file",
                "file-1",
                {"addParents": "folder-new", "removeParents": "folder-old"},
            )

    def test_a_draft_is_verified_by_identity_not_by_the_bytes_gmail_rewrote(self) -> None:
        adapter, google = self.adapter()
        record = adapter.execute_routine("gmail_create_draft", "", {"raw": "c3ludGhldGlj"})
        self.assertTrue(record.source_id)
        # Gmail normalizes headers when it stores a draft, so a byte comparison
        # would report a false mismatch on a draft that was stored correctly.
        self.assertEqual([method for method, _ in google.calls], ["POST", "GET"])

    def test_every_readback_asks_for_the_fields_it_intends_to_compare(self) -> None:
        """A field mask narrower than the comparison can never verify."""

        from assistant.scotty_business.google_readback import plan

        endpoints = {
            "gmail": "https://gmail.example.invalid",
            "calendar": "https://calendar.example.invalid",
            "drive": "https://drive.example.invalid",
            "docs": "https://docs.example.invalid",
            "sheets": "https://sheets.example.invalid",
            "people": "https://people.example.invalid",
        }
        cases = (
            ("drive_update_file", "file-1", {"name": "Renamed"}, {}),
            ("drive_move_file", "file-1", {"addParents": "folder-new"}, {}),
            ("drive_trash_file", "file-1", {}, {}),
            (
                "drive_change_permissions",
                "file-1",
                {"type": "user", "role": "reader"},
                {},
            ),
            ("docs_create", "", {"title": "Synthetic"}, {"documentId": "document-1"}),
            (
                "docs_batch_update",
                "document-1",
                {"requests": [{"insertText": {}}]},
                {"documentId": "document-1"},
            ),
            (
                "contacts_update",
                "people/c1",
                {"names": [{"givenName": "Synthetic"}]},
                {},
            ),
        )
        for operation, resource, payload, response in cases:
            with self.subTest(operation=operation):
                plan_ = plan(operation, resource, payload, response, endpoints)
                assert plan_ is not None
                query = plan_.request.query or {}
                mask = str(query.get("fields") or query.get("personFields") or "")
                if not mask:
                    continue
                requested = {
                    part.split("(")[0] for part in mask.replace(" ", "").split(",") if part
                }
                if "personFields" in query:
                    # People API returns the resource identity outside the mask.
                    requested |= {"resourceName", "etag"}
                compared = set(plan_.expected) | set(plan_.absent)
                self.assertLessEqual(
                    compared,
                    requested,
                    f"{operation} compares fields its readback never asks for",
                )


class UnverifiedConsequenceLedgerTests(unittest.TestCase):
    """An unverified consequence write lands in the ledger as unknown."""

    def service(self, google):
        import tempfile

        from assistant.scotty_business.approvals import ApprovalStore
        from assistant.scotty_business.service import ScottyService

        directory = tempfile.TemporaryDirectory(prefix="scotty-readback-")
        self.addCleanup(directory.cleanup)
        store = ApprovalStore(f"{directory.name}/approvals.db")
        store.initialize()
        unused = object()
        return (
            ScottyService(
                synthetic.config(google_workspace=GOOGLE_ACCOUNTS),
                store,
                trello=unused,
                ghl=unused,
                rentcast=None,
                discord=unused,
                google_workspace=google,
            ),
            store,
        )

    def adapter(self, transport):
        from assistant.scotty_business.adapters.google_workspace import GoogleWorkspaceAdapter

        config = RuntimeConfig.from_mapping(
            synthetic.private_mapping(google_workspace=GOOGLE_ACCOUNTS)
        )
        assert config.google_for(Role.MAIN_OPERATOR) is not None
        return GoogleWorkspaceAdapter(
            transport, "synthetic-access-token", config.google_for(Role.MAIN_OPERATOR)
        )

    def execute(self, google):
        from assistant.scotty_business.approvals import ProposalStatus
        from assistant.scotty_business.policy import Role

        service, store = self.service(self.adapter(google))
        operator = synthetic.config().principal_for(Role.MAIN_OPERATOR)
        proposal = service.propose_google_workspace_write(
            operator, "drive_delete_permanently", "file-1", {}
        )
        approved = store.approve(proposal.proposal_id, operator, proposal.version)
        return (
            service.execute(
                operator,
                proposal.proposal_id,
                expected_version=approved.version,
                execution_nonce=approved.execution_nonce,
            ),
            ProposalStatus,
            store,
            operator,
        )

    def test_a_verified_consequence_write_is_recorded_verified(self) -> None:
        google = SyntheticGoogle()
        outcome, statuses, _, _ = self.execute(google)
        self.assertEqual(outcome.status, statuses.VERIFIED)
        self.assertNotIn("file-1", google.files)

    def test_an_unverified_consequence_write_is_recorded_unknown(self) -> None:
        google = SyntheticGoogle()
        # The delete lands, but the readback still shows the file, so the
        # outcome is ambiguous and must never be recorded as verified.
        google.readback_status = 200
        google.readback_body = {"id": "file-1"}
        outcome, statuses, _, _ = self.execute(google)
        self.assertEqual(outcome.status, statuses.UNKNOWN)
        self.assertIs(outcome.receipt.get("verified"), False)

    def test_an_unknown_outcome_is_terminal_and_is_never_retried(self) -> None:
        from assistant.scotty_business.approvals import ApprovalError

        google = SyntheticGoogle()
        google.readback_status = 500
        google.readback_body = {}
        outcome, statuses, store, operator = self.execute(google)
        self.assertEqual(outcome.status, statuses.UNKNOWN)
        # The write is reconciled by a person, never repeated by Scotty: the
        # ledger refuses to claim an already-terminal proposal again.
        with self.assertRaises(ApprovalError):
            store.claim_execution(
                outcome.proposal_id,
                operator,
                expected_version=outcome.version,
                execution_nonce=outcome.execution_nonce,
                current_source_revision="configured-google-resource-v1",
            )


class ReadbackNormalizationTests(unittest.TestCase):
    """Equal state written differently must compare equal; different must not."""

    def matches(self, intended, observed):
        from assistant.scotty_business.google_readback import matches

        return matches(intended, observed)

    def test_whitespace_and_instant_representation_are_normalized(self) -> None:
        self.assertTrue(self.matches({"summary": "Team sync"}, {"summary": " Team sync "}))
        self.assertTrue(
            self.matches(
                {"start": {"dateTime": "2026-01-01T10:00:00-07:00"}},
                {"start": {"dateTime": "2026-01-01T17:00:00Z"}},
            )
        )

    def test_list_order_does_not_matter_but_membership_does(self) -> None:
        self.assertTrue(self.matches({"labelIds": ["A", "B"]}, {"labelIds": ["B", "C", "A"]}))
        self.assertFalse(self.matches({"labelIds": ["A", "B"]}, {"labelIds": ["A"]}))

    def test_only_the_fields_the_intent_named_are_required(self) -> None:
        self.assertTrue(self.matches({"name": "Notes"}, {"name": "Notes", "size": "12"}))
        self.assertFalse(self.matches({"name": "Notes"}, {"size": "12"}))

    def test_a_different_value_or_shape_never_matches(self) -> None:
        for intended, observed in (
            ({"name": "Notes"}, {"name": "Other"}),
            ({"labelIds": ["A"]}, {"labelIds": "A"}),
            ({"start": {"dateTime": "2026-01-01T10:00:00Z"}}, {"start": "2026-01-01T10:00:00Z"}),
            ({"trashed": True}, {"trashed": False}),
        ):
            with self.subTest(intended=intended):
                self.assertFalse(self.matches(intended, observed))


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
            synthetic.private_mapping(google_workspace=GOOGLE_ACCOUNTS)
        )
        assert config.google_for(Role.MAIN_OPERATOR) is not None
        transport = FakeTransport()
        issued = iter(["token-1", "token-2", "token-3"])
        adapter = GoogleWorkspaceAdapter(
            transport, lambda: next(issued), config.google_for(Role.MAIN_OPERATOR)
        )

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
            synthetic.private_mapping(google_workspace=GOOGLE_ACCOUNTS)
        )
        assert config.google_for(Role.MAIN_OPERATOR) is not None
        transport = FakeTransport()

        def unavailable() -> str:
            raise GoogleOAuthError("Google OAuth token state is unavailable")

        adapter = GoogleWorkspaceAdapter(
            transport, unavailable, config.google_for(Role.MAIN_OPERATOR)
        )
        with self.assertRaises(ProviderError):
            adapter.search_gmail("from:customer")
        self.assertEqual(transport.calls, [])

        with self.assertRaises(ProviderError):
            GoogleWorkspaceAdapter(transport, "", config.google_for(Role.MAIN_OPERATOR))


class HeadlessConsentTests(unittest.TestCase):
    """Consent on a server with no browser, and no secret in what is shown."""

    CLIENT = {
        "installed": {
            "client_id": "synthetic-client-id.apps.googleusercontent.com",
            "client_secret": "synthetic-client-secret-0001",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    def client_file(self, directory: Path) -> Path:
        path = directory / "google-oauth-client.json"
        path.write_text(json.dumps(self.CLIENT), encoding="utf-8")
        path.chmod(0o600)
        return path

    def request(self, directory: Path):
        from assistant.scotty_business.google_oauth import begin_consent

        return begin_consent(
            self.client_file(directory), GOOGLE_OAUTH_SCOPES, owner_uid=os.getuid()
        )

    def test_the_server_never_opens_a_browser(self) -> None:
        source = Path("assistant/scotty_business/google_oauth.py").read_text(encoding="utf-8")
        self.assertNotIn("webbrowser", source)
        self.assertNotIn("HTTPServer", source)

    def test_the_authorization_url_is_complete_and_carries_no_secret(self) -> None:
        import urllib.parse

        with tempfile.TemporaryDirectory(prefix="scotty-consent-") as directory:
            request = self.request(Path(directory))
        parsed = urllib.parse.urlsplit(request.authorization_url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            "https://accounts.google.com/o/oauth2/auth",
        )
        self.assertEqual(query["client_id"], self.CLIENT["installed"]["client_id"])
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(query["access_type"], "offline")
        self.assertEqual(set(query["scope"].split()), set(GOOGLE_OAUTH_SCOPES))
        self.assertNotIn("synthetic-client-secret-0001", request.authorization_url)
        self.assertNotIn(request.verifier, request.authorization_url)

    def test_what_is_presented_to_trent_contains_no_secret(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-consent-") as directory:
            request = self.request(Path(directory))
            rendered = json.dumps(dict(request.presentable()))
        # The client secret and the PKCE verifier are the two values that must
        # never leave the root-owned side. The state and the challenge are
        # designed to travel in the URL.
        self.assertNotIn("synthetic-client-secret-0001", rendered)
        self.assertNotIn(request.verifier, rendered)

    def test_a_pasted_redirect_completes_the_exchange(self) -> None:
        from assistant.scotty_business.google_oauth import (
            GoogleTokenStore,
            complete_consent,
        )

        seen: list[dict[str, object]] = []

        def exchange(**kwargs: object) -> dict[str, object]:
            seen.append(kwargs)
            return {
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "expires_in": 3600,
                "scope": " ".join(GOOGLE_OAUTH_SCOPES),
            }

        with tempfile.TemporaryDirectory(prefix="scotty-consent-") as directory:
            root = Path(directory)
            request = self.request(root)
            store = GoogleTokenStore(
                root / "token.json", owner_uid=os.getuid(), owner_gid=os.getgid()
            )
            account = complete_consent(
                self.client_file(root),
                store,
                request,
                f"{request.redirect_uri}?state={request.state}&code=synthetic-code",
                owner_uid=os.getuid(),
                exchange=exchange,
                verify_account=lambda token: "scotty.synthetic@example.invalid",
            )
            self.assertEqual(account, "scotty.synthetic@example.invalid")
            self.assertTrue(store.ready(GOOGLE_OAUTH_SCOPES, "scotty.synthetic@example.invalid"))
        fields = seen[0]["fields"]
        self.assertEqual(fields["code"], "synthetic-code")
        self.assertEqual(fields["code_verifier"], request.verifier)
        self.assertEqual(fields["redirect_uri"], request.redirect_uri)

    def test_a_wrong_state_declined_consent_or_bad_address_is_refused(self) -> None:
        from assistant.scotty_business.google_oauth import (
            GoogleOAuthError,
            authorization_code,
        )

        with tempfile.TemporaryDirectory(prefix="scotty-consent-") as directory:
            request = self.request(Path(directory))
        for pasted in (
            f"{request.redirect_uri}?state=someone-else&code=synthetic-code",
            f"{request.redirect_uri}?state={request.state}&error=access_denied",
            f"{request.redirect_uri}?state={request.state}",
            "https://evil.example.invalid/?state=x&code=y",
            "not a url",
            "",
            None,
            "x" * 5000,
        ):
            with self.subTest(pasted=str(pasted)[:30]), self.assertRaises(GoogleOAuthError):
                authorization_code(pasted, request.state)

    def test_an_expired_code_fails_closed_and_stores_nothing(self) -> None:
        from assistant.scotty_business.google_oauth import (
            GoogleOAuthError,
            GoogleTokenStore,
            complete_consent,
        )

        def rejecting(**kwargs: object) -> dict[str, object]:
            raise GoogleOAuthError("Google OAuth token exchange failed")

        with tempfile.TemporaryDirectory(prefix="scotty-consent-") as directory:
            root = Path(directory)
            request = self.request(root)
            store = GoogleTokenStore(
                root / "token.json", owner_uid=os.getuid(), owner_gid=os.getgid()
            )
            with self.assertRaises(GoogleOAuthError):
                complete_consent(
                    self.client_file(root),
                    store,
                    request,
                    f"{request.redirect_uri}?state={request.state}&code=expired",
                    owner_uid=os.getuid(),
                    exchange=rejecting,
                )
            self.assertFalse((root / "token.json").exists())

    def test_a_narrowed_grant_is_refused(self) -> None:
        from assistant.scotty_business.google_oauth import (
            GoogleOAuthError,
            GoogleTokenStore,
            complete_consent,
        )

        def narrowed(**kwargs: object) -> dict[str, object]:
            return {
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "expires_in": 3600,
                "scope": "openid email",
            }

        with tempfile.TemporaryDirectory(prefix="scotty-consent-") as directory:
            root = Path(directory)
            request = self.request(root)
            store = GoogleTokenStore(
                root / "token.json", owner_uid=os.getuid(), owner_gid=os.getgid()
            )
            with self.assertRaises(GoogleOAuthError):
                complete_consent(
                    self.client_file(root),
                    store,
                    request,
                    f"{request.redirect_uri}?state={request.state}&code=synthetic-code",
                    owner_uid=os.getuid(),
                    exchange=narrowed,
                )
            self.assertFalse((root / "token.json").exists())

    def test_the_client_is_imported_owner_only_and_validated(self) -> None:
        from assistant.scotty_business.google_oauth import GoogleOAuthError, import_client

        with tempfile.TemporaryDirectory(prefix="scotty-consent-") as directory:
            root = Path(directory)
            destination = root / "protected" / "client.json"
            import_client(self.client_file(root), destination, owner_uid=os.getuid())
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertIn("client_secret", destination.read_text(encoding="utf-8"))

            wrong = root / "wrong.json"
            wrong.write_text(json.dumps({"web": self.CLIENT["installed"]}), encoding="utf-8")
            wrong.chmod(0o600)
            with self.assertRaises(GoogleOAuthError):
                import_client(wrong, root / "other.json", owner_uid=os.getuid())

    def test_the_published_prompt_is_readable_and_secret_free(self) -> None:
        from assistant.scotty_business.google_oauth import (
            publish_consent_prompt,
            read_consent_prompt,
        )

        with tempfile.TemporaryDirectory(prefix="scotty-consent-") as directory:
            root = Path(directory)
            request = self.request(root)
            prompt_path = root / "google-consent.json"
            publish_consent_prompt(prompt_path, request, owner_uid=os.getuid())
            prompt = read_consent_prompt(prompt_path)
            assert prompt is not None
            self.assertEqual(prompt["authorization_url"], request.authorization_url)
            body = prompt_path.read_text(encoding="utf-8")
            self.assertNotIn("synthetic-client-secret-0001", body)
            self.assertNotIn(request.verifier, body)
            # Completing consent needs the verifier and the client secret, both
            # root-only, so a readable state cannot be used to finish the flow.
            self.assertNotIn("code_verifier", body)

    def test_an_absent_or_forged_prompt_is_ignored(self) -> None:
        from assistant.scotty_business.google_oauth import read_consent_prompt

        with tempfile.TemporaryDirectory(prefix="scotty-consent-") as directory:
            root = Path(directory)
            self.assertIsNone(read_consent_prompt(root / "absent.json"))
            forged = root / "forged.json"
            forged.write_text(
                json.dumps({"authorization_url": "https://evil.example.invalid/"}),
                encoding="utf-8",
            )
            self.assertIsNone(read_consent_prompt(forged))
            link = root / "link.json"
            link.symlink_to(forged)
            self.assertIsNone(read_consent_prompt(link))

    def test_local_setup_reads_the_redirect_through_hidden_input(self) -> None:
        from assistant.scotty_business.setup import SetupError, connect_google_workspace

        prompts: list[str] = []
        printed: list[str] = []

        with tempfile.TemporaryDirectory(prefix="scotty-consent-") as directory:
            root = Path(directory)
            client = self.client_file(root)

            def hidden(prompt: str) -> str:
                prompts.append(prompt)
                # The state is not known to the caller, so this fails closed.
                return "http://localhost:8765/oauth2/callback?state=x&code=y"

            with self.assertRaises(SetupError):
                connect_google_workspace(
                    "scotty.synthetic@example.invalid",
                    client_path=client,
                    token_path=root / "token.json",
                    prompt_path=root / "prompt.json",
                    hidden_fn=hidden,
                    output=printed.append,
                    owner_uid=os.getuid(),
                    runtime_uid=os.getuid(),
                )
        self.assertTrue(any("hidden" in prompt.lower() for prompt in prompts))
        self.assertTrue(any("accounts.google.com" in line for line in printed))
        self.assertFalse((root / "token.json").exists())

    def test_local_setup_imports_the_desktop_client_before_consent(self) -> None:
        """The documented import step is reachable from local setup itself."""

        from assistant.scotty_business.setup import SetupError, connect_google_workspace

        def attempt(root: Path, source: Path, protected: Path) -> None:
            connect_google_workspace(
                "scotty.synthetic@example.invalid",
                client_path=protected,
                token_path=root / "token.json",
                prompt_path=root / "prompt.json",
                input_fn=lambda _: str(source),
                hidden_fn=lambda _: ("http://localhost:8765/oauth2/callback?state=x&code=y"),
                output=lambda _: None,
                owner_uid=os.getuid(),
                runtime_uid=os.getuid(),
            )

        with tempfile.TemporaryDirectory(prefix="scotty-consent-") as directory:
            root = Path(directory)
            downloaded = self.client_file(root)
            protected = root / "protected" / "google-oauth-client.json"
            # The exchange still fails closed on the unknown state; what this
            # proves is that the client arrived through local setup instead of
            # having to be placed by hand for unreachable code.
            with self.assertRaises(SetupError):
                attempt(root, downloaded, protected)
            self.assertTrue(protected.is_file())
            self.assertEqual(protected.stat().st_mode & 0o777, 0o600)
            self.assertFalse((root / "token.json").exists())

            wrong = root / "web-client.json"
            wrong.write_text(json.dumps({"web": self.CLIENT["installed"]}), encoding="utf-8")
            wrong.chmod(0o600)
            refused = root / "refused" / "client.json"
            with self.assertRaises(SetupError):
                attempt(root, wrong, refused)
            self.assertFalse(refused.exists())

    def test_an_already_imported_client_is_never_re_imported(self) -> None:
        from assistant.scotty_business.setup import SetupError, connect_google_workspace

        asked: list[str] = []
        with tempfile.TemporaryDirectory(prefix="scotty-consent-") as directory:
            root = Path(directory)
            client = self.client_file(root)
            with self.assertRaises(SetupError):
                connect_google_workspace(
                    "scotty.synthetic@example.invalid",
                    client_path=client,
                    token_path=root / "token.json",
                    prompt_path=root / "prompt.json",
                    input_fn=lambda prompt: asked.append(prompt) or "",
                    hidden_fn=lambda _: ("http://localhost:8765/oauth2/callback?state=x&code=y"),
                    output=lambda _: None,
                    owner_uid=os.getuid(),
                    runtime_uid=os.getuid(),
                )
        self.assertEqual(asked, [])

    def test_the_consent_prompt_never_outlives_its_attempt(self) -> None:
        """A published URL whose verifier is gone must not be shown as live."""

        from assistant.scotty_business.google_oauth import read_consent_prompt
        from assistant.scotty_business.setup import SetupError, connect_google_workspace

        with tempfile.TemporaryDirectory(prefix="scotty-consent-") as directory:
            root = Path(directory)
            client = self.client_file(root)
            prompt_path = root / "prompt.json"
            published: list[str] = []

            with self.assertRaises(SetupError):
                connect_google_workspace(
                    "scotty.synthetic@example.invalid",
                    client_path=client,
                    token_path=root / "token.json",
                    prompt_path=prompt_path,
                    hidden_fn=lambda _: (
                        # While the attempt is live the URL is showable.
                        published.append(str(read_consent_prompt(prompt_path)))
                        or "http://localhost:8765/oauth2/callback?state=x&code=y"
                    ),
                    output=lambda _: None,
                    owner_uid=os.getuid(),
                    runtime_uid=os.getuid(),
                )
            self.assertTrue(any("accounts.google.com" in line for line in published))
            # The attempt is over, so the PKCE verifier is gone with it.
            self.assertFalse(prompt_path.exists())
            self.assertIsNone(read_consent_prompt(prompt_path))

    def test_a_stale_prompt_is_cleared_once_the_account_is_connected(self) -> None:
        from assistant.scotty_business.google_oauth import (
            GoogleTokenStore,
            OAuthToken,
            publish_consent_prompt,
            read_consent_prompt,
        )
        from assistant.scotty_business.setup import connect_google_workspace

        with tempfile.TemporaryDirectory(prefix="scotty-consent-") as directory:
            root = Path(directory)
            prompt_path = root / "prompt.json"
            publish_consent_prompt(prompt_path, self.request(root), owner_uid=os.getuid())
            token_path = root / "token.json"
            GoogleTokenStore(token_path, owner_uid=os.getuid(), owner_gid=os.getgid()).write(
                OAuthToken(
                    access_token="synthetic-access",
                    refresh_token="synthetic-refresh",
                    expires_at=int(time.time()) + 3600,
                    scopes=GOOGLE_OAUTH_SCOPES,
                    account_email="scotty.synthetic@example.invalid",
                    client_id="synthetic-client-id.apps.googleusercontent.com",
                    client_secret="synthetic-client-secret-0001",
                )
            )
            connect_google_workspace(
                "scotty.synthetic@example.invalid",
                client_path=self.client_file(root),
                token_path=token_path,
                prompt_path=prompt_path,
                input_fn=lambda _: "",
                hidden_fn=lambda _: "",
                output=lambda _: None,
                owner_uid=os.getuid(),
                runtime_uid=os.getuid(),
            )
            self.assertFalse(prompt_path.exists())
            self.assertIsNone(read_consent_prompt(prompt_path))


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


class PerUserWorkspaceAuthorityTests(unittest.TestCase):
    """Each client user works in their own Workspace and never the other's."""

    def runtime(self):
        from test_provider_connection import runtime

        return runtime(DISCORD_BOT_TOKEN="synthetic-discord")

    def principal(self, role):
        from assistant.scotty_business.policy import Principal, Role

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

    def connect(self, runtime):
        """Replace the Workspace port with a recorder so authorization is visible."""

        from assistant.scotty_business.adapters.records import ProviderRecord, utc_now

        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def _record(self, source_id: str) -> ProviderRecord:
                return ProviderRecord("google_workspace", source_id, utc_now(), "v1", {}, ())

            def execute_routine(self, operation, resource_id, payload):
                self.calls.append((operation, resource_id))
                return self._record(resource_id)

            def search_gmail(self, query, *, max_results=50):
                self.calls.append(("search_gmail", query))
                return (self._record("message-1"),)

            def __getattr__(self, name):
                def getter(resource_id, *rest):
                    self.calls.append((name, resource_id))
                    return self._record(resource_id)

                return getter

        from assistant.scotty_business.policy import Role

        recorders = {role: Recorder() for role in (Role.MAIN_OPERATOR, Role.EMPLOYEE)}
        runtime.google_adapters = dict(recorders)
        runtime.google_connected = dict.fromkeys(recorders, True)
        runtime.connected["google_workspace"] = True
        return recorders

    def test_a_users_routine_work_never_touches_the_other_users_workspace(self) -> None:
        """Consent is personal, so the actor decides which mailbox is reached."""

        from assistant.scotty_business.google_policy import ROUTINE_GOOGLE_OPERATIONS
        from assistant.scotty_business.policy import Role

        for actor, bystander in (
            (Role.EMPLOYEE, Role.MAIN_OPERATOR),
            (Role.MAIN_OPERATOR, Role.EMPLOYEE),
        ):
            with self.subTest(actor=actor), self.runtime() as runtime:
                recorders = self.connect(runtime)
                for operation in sorted(ROUTINE_GOOGLE_OPERATIONS):
                    runtime.handle_read(
                        self.principal(actor),
                        {
                            "operation": "google_workspace",
                            "google_operation": operation,
                            "resource_id": "resource-1",
                            "payload": {"name": "synthetic"},
                        },
                    )
                self.assertEqual(len(recorders[actor].calls), len(ROUTINE_GOOGLE_OPERATIONS))
                self.assertEqual(recorders[bystander].calls, [])

    def test_an_unlinked_user_is_told_to_connect_rather_than_borrowing_an_account(
        self,
    ) -> None:
        from assistant.scotty_business.policy import Role
        from assistant.scotty_business.runtime import ProviderNotConnected

        with self.runtime() as runtime:
            recorders = self.connect(runtime)
            # The employee has not completed consent, so they have no adapter.
            del runtime.google_adapters[Role.EMPLOYEE]
            runtime.google_connected[Role.EMPLOYEE] = False
            with self.assertRaises(ProviderNotConnected):
                runtime.handle_read(
                    self.principal(Role.EMPLOYEE),
                    {
                        "operation": "google_workspace",
                        "google_operation": "search_gmail",
                        "payload": {"query": "invoice"},
                    },
                )
            self.assertEqual(recorders[Role.MAIN_OPERATOR].calls, [])

    def test_a_tool_argument_can_never_choose_whose_workspace_is_used(self) -> None:
        from assistant.scotty_business.policy import Role
        from assistant.scotty_business.provider_identity import ProviderIdentityError

        with self.runtime() as runtime:
            recorders = self.connect(runtime)
            for override in (
                {"account_email": "operator.synthetic@example.invalid"},
                {"actor": "main_operator"},
                {"role": "main_operator"},
                {"payload": {"on_behalf_of": "301000000000000001"}},
            ):
                with self.subTest(override=override), self.assertRaises(ProviderIdentityError):
                    runtime.handle_read(
                        self.principal(Role.EMPLOYEE),
                        {
                            "operation": "google_workspace",
                            "google_operation": "search_gmail",
                            **override,
                        },
                    )
            self.assertEqual(recorders[Role.MAIN_OPERATOR].calls, [])
            self.assertEqual(recorders[Role.EMPLOYEE].calls, [])

    def test_the_new_bounded_reads_are_available_to_both_client_roles(self) -> None:
        from assistant.scotty_business.policy import Role

        with self.runtime() as runtime:
            recorders = self.connect(runtime)
            for role in (Role.EMPLOYEE, Role.MAIN_OPERATOR):
                for operation, payload in (
                    ("read_drive_file", {}),
                    ("get_sheet_values", {"range": "Sheet1!A1:B2"}),
                    ("batch_get_sheet_values", {"ranges": ["Sheet1!A1"]}),
                ):
                    with self.subTest(role=role, operation=operation):
                        runtime.handle_read(
                            self.principal(role),
                            {
                                "operation": "google_workspace",
                                "google_operation": operation,
                                "resource_id": "resource-1",
                                "payload": payload,
                            },
                        )
            self.assertEqual(sum(len(recorder.calls) for recorder in recorders.values()), 6)

    def test_a_malformed_range_argument_is_refused_by_the_runtime(self) -> None:
        from assistant.scotty_business.policy import Role

        with self.runtime() as runtime:
            recorders = self.connect(runtime)
            for operation, payload in (
                ("get_sheet_values", {}),
                ("get_sheet_values", {"range": 7}),
                ("batch_get_sheet_values", {}),
                ("batch_get_sheet_values", {"ranges": "Sheet1!A1"}),
            ):
                with self.subTest(operation=operation), self.assertRaises(ValueError):
                    runtime.handle_read(
                        self.principal(Role.MAIN_OPERATOR),
                        {
                            "operation": "google_workspace",
                            "google_operation": operation,
                            "resource_id": "resource-1",
                            "payload": payload,
                        },
                    )
            self.assertEqual(recorders[Role.MAIN_OPERATOR].calls, [])

    def test_the_new_reads_report_not_connected_rather_than_failing_oddly(self) -> None:
        from assistant.scotty_business.policy import Role
        from assistant.scotty_business.runtime import ProviderNotConnected

        with self.runtime() as runtime:
            for operation, payload in (
                ("read_drive_file", {}),
                ("get_sheet_values", {"range": "A1"}),
                ("batch_get_sheet_values", {"ranges": ["A1"]}),
            ):
                with self.subTest(operation=operation), self.assertRaises(ProviderNotConnected):
                    runtime.handle_read(
                        self.principal(Role.MAIN_OPERATOR),
                        {
                            "operation": "google_workspace",
                            "google_operation": operation,
                            "resource_id": "resource-1",
                            "payload": payload,
                        },
                    )

    def test_an_employee_may_still_read_the_workspace(self) -> None:
        from assistant.scotty_business.policy import Role

        with self.runtime() as runtime:
            recorders = self.connect(runtime)
            result = runtime.handle_read(
                self.principal(Role.EMPLOYEE),
                {
                    "operation": "google_workspace",
                    "google_operation": "search_gmail",
                    "payload": {"query": "from:customer"},
                },
            )
            self.assertTrue(result)
            self.assertEqual(recorders[Role.EMPLOYEE].calls, [("search_gmail", "from:customer")])
            self.assertEqual(recorders[Role.MAIN_OPERATOR].calls, [])

    def test_the_main_operator_may_perform_routine_workspace_work(self) -> None:
        from assistant.scotty_business.policy import Role

        with self.runtime() as runtime:
            recorders = self.connect(runtime)
            runtime.handle_read(
                self.principal(Role.MAIN_OPERATOR),
                {
                    "operation": "google_workspace",
                    "google_operation": "drive_update_file",
                    "resource_id": "file-1",
                    "payload": {"name": "Renamed"},
                },
            )
            self.assertEqual(recorders[Role.MAIN_OPERATOR].calls, [("drive_update_file", "file-1")])


class GoogleApprovalPolicyTests(unittest.TestCase):
    def test_employee_cannot_approve_google_consequences_but_operator_can(self) -> None:
        from assistant.scotty_business.policy import can_approve

        config = synthetic.config(google_workspace=GOOGLE_ACCOUNTS)
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
