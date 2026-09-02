"""A batch write is verified by its final state, not by its receipt.

Docs and Sheets batch updates were read back to the document and the
spreadsheet and compared against `{"documentId": ...}` and
`{"spreadsheetId": ...}` -- the resource is the resource. Combined with a
reply count from the write's own response, that is what the contract names as
not verification: it proves the document exists and that Google sent as many
replies as there were requests, neither of which says the text was inserted.

What follows asks the readback what it would compare, and drives the whole
mutate-and-verify path against recorded shapes where a request landed, where it
did not, and where nobody can tell. The third case has to come back `unknown`:
a batch containing a request whose effect cannot be observed is a batch nobody
can call verified, however many replies came back.
"""

from __future__ import annotations

import unittest

from assistant.scotty_business.google_readback import (
    ReadbackStatus,
    applied_fully,
    plan,
    verify,
)

ENDPOINTS = {
    "gmail": "https://gmail.example/v1",
    "calendar": "https://calendar.example/v3",
    "drive": "https://drive.example/v3",
    "docs": "https://docs.example/v1",
    "sheets": "https://sheets.example/v4",
    "people": "https://people.example/v1",
}


def planned(operation, resource_id, payload, response=None):
    return plan(operation, resource_id, payload, response or {}, ENDPOINTS)


class AlreadyProvenTests(unittest.TestCase):
    """The two that were already right, held so they stay right.

    Writing values and writing a contact were checked before this change, and
    against the payload shapes the adapter really sends: a values write is a
    `values:batchUpdate` with `data` entries, and the readback batch-gets those
    ranges so a write that landed in the wrong sheet no longer satisfies it.
    These are here so the batch-update work below cannot quietly regress them.
    """

    def test_a_values_write_is_read_back_by_range_and_by_value(self) -> None:
        payload = {"data": [{"range": "Sheet1!A1:B1", "values": [["a", "b"]]}]}
        made = planned("sheets_update_values", "sheet-1", payload)
        self.assertIsNotNone(made)
        assert made is not None
        self.assertIn("values:batchGet", made.request.url)
        landed = {"valueRanges": [{"range": "Sheet1!A1:B1", "values": [["a", "b"]]}]}
        self.assertEqual(verify(made, 200, landed, fully_applied=None), ReadbackStatus.VERIFIED)
        elsewhere = {"valueRanges": [{"range": "Sheet2!A1:B1", "values": [["a", "b"]]}]}
        self.assertEqual(verify(made, 200, elsewhere, fully_applied=None), ReadbackStatus.MISMATCH)

    def test_a_values_write_with_no_range_cannot_be_proven(self) -> None:
        self.assertIsNone(planned("sheets_update_values", "sheet-1", {"data": [{"values": []}]}))

    def test_a_contact_write_is_read_back_on_the_fields_it_set(self) -> None:
        made = planned("contacts_update", "people/c1", {"names": [{"givenName": "Sam"}]})
        self.assertIsNotNone(made)
        assert made is not None
        observed = {"resourceName": "people/c1", "names": [{"givenName": "Sam"}]}
        self.assertEqual(verify(made, 200, observed, fully_applied=None), ReadbackStatus.VERIFIED)
        stale = {"resourceName": "people/c1", "names": [{"givenName": "Older"}]}
        self.assertEqual(verify(made, 200, stale, fully_applied=None), ReadbackStatus.MISMATCH)


class DocsBatchTests(unittest.TestCase):
    """A document batch is proved by what is in the document afterwards."""

    def insert(self, text: str) -> dict[str, object]:
        return {"requests": [{"insertText": {"location": {"index": 1}, "text": text}}]}

    def document(self, *runs: str) -> dict[str, object]:
        return {
            "documentId": "doc-1",
            "body": {
                "content": [
                    {"paragraph": {"elements": [{"textRun": {"content": run}}]}} for run in runs
                ]
            },
        }

    def test_the_readback_asks_for_the_content_not_just_the_identity(self) -> None:
        made = planned("docs_batch_update", "doc-1", self.insert("hello"), {"replies": [{}]})
        self.assertIsNotNone(made)
        assert made is not None
        # A field mask that returns only documentId proves the document exists.
        self.assertIn("body", str(made.request.query))

    def test_text_that_really_landed_verifies(self) -> None:
        made = planned("docs_batch_update", "doc-1", self.insert("hello"), {"replies": [{}]})
        self.assertEqual(
            verify(made, 200, self.document("hello there"), fully_applied=True),
            ReadbackStatus.VERIFIED,
        )

    def test_text_that_never_arrived_is_a_mismatch_however_many_replies_came_back(
        self,
    ) -> None:
        made = planned("docs_batch_update", "doc-1", self.insert("hello"), {"replies": [{}]})
        # Google said "one request, one reply". The document disagrees.
        self.assertTrue(applied_fully("docs_batch_update", self.insert("hello"), {"replies": [{}]}))
        self.assertEqual(
            verify(made, 200, self.document("something else"), fully_applied=True),
            ReadbackStatus.MISMATCH,
        )

    def test_a_replacement_checks_both_that_it_arrived_and_that_the_old_text_left(self) -> None:
        payload = {
            "requests": [
                {"replaceAllText": {"containsText": {"text": "old"}, "replaceText": "new"}}
            ]
        }
        made = planned("docs_batch_update", "doc-1", payload, {"replies": [{}]})
        self.assertEqual(
            verify(made, 200, self.document("new text"), fully_applied=True),
            ReadbackStatus.VERIFIED,
        )
        self.assertEqual(
            verify(made, 200, self.document("old text new"), fully_applied=True),
            ReadbackStatus.MISMATCH,
        )

    def test_a_request_whose_effect_cannot_be_observed_makes_the_batch_unknown(self) -> None:
        """The honest answer, and the one the contract asks for.

        Deleting a content range leaves nothing to look for. A batch containing
        one is not verifiable, so it must not be verified -- not even when the
        rest of it clearly landed.
        """

        payload = {
            "requests": [
                {"insertText": {"location": {"index": 1}, "text": "hello"}},
                {"deleteContentRange": {"range": {"startIndex": 5, "endIndex": 9}}},
            ]
        }
        self.assertIsNone(planned("docs_batch_update", "doc-1", payload, {"replies": [{}, {}]}))
        self.assertEqual(
            verify(None, 200, self.document("hello"), fully_applied=True),
            ReadbackStatus.UNSUPPORTED,
        )


class SheetsBatchTests(unittest.TestCase):
    """A spreadsheet batch is proved by the sheet it claims to have changed."""

    def test_adding_a_sheet_is_proved_by_the_sheet_being_there(self) -> None:
        payload = {"requests": [{"addSheet": {"properties": {"title": "Deals"}}}]}
        made = planned("sheets_batch_update", "sheet-1", payload, {"replies": [{}]})
        self.assertIsNotNone(made)
        assert made is not None
        observed = {
            "spreadsheetId": "sheet-1",
            "sheets": [{"properties": {"title": "Deals"}}],
        }
        self.assertEqual(verify(made, 200, observed, fully_applied=True), ReadbackStatus.VERIFIED)
        absent = {"spreadsheetId": "sheet-1", "sheets": [{"properties": {"title": "Sheet1"}}]}
        self.assertEqual(verify(made, 200, absent, fully_applied=True), ReadbackStatus.MISMATCH)

    def test_renaming_the_spreadsheet_is_proved_by_its_title(self) -> None:
        payload = {
            "requests": [
                {
                    "updateSpreadsheetProperties": {
                        "properties": {"title": "Q3 Pipeline"},
                        "fields": "title",
                    }
                }
            ]
        }
        made = planned("sheets_batch_update", "sheet-1", payload, {"replies": [{}]})
        observed = {"spreadsheetId": "sheet-1", "properties": {"title": "Q3 Pipeline"}}
        self.assertEqual(verify(made, 200, observed, fully_applied=True), ReadbackStatus.VERIFIED)

    def test_a_structural_request_nobody_can_observe_makes_the_batch_unknown(self) -> None:
        payload = {"requests": [{"repeatCell": {"range": {"sheetId": 0}, "cell": {}}}]}
        self.assertIsNone(planned("sheets_batch_update", "sheet-1", payload, {"replies": [{}]}))


class NoOperationIsSilentlyUnprovableTests(unittest.TestCase):
    """Every declared operation either has a readback or is named as lacking one."""

    def test_the_operations_without_an_authoritative_read_are_declared(self) -> None:
        from assistant.scotty_business.google_policy import (
            CONSEQUENCE_GOOGLE_OPERATIONS,
            ROUTINE_GOOGLE_OPERATIONS,
        )
        from assistant.scotty_business.google_readback import UNPROVABLE_OPERATIONS

        declared = ROUTINE_GOOGLE_OPERATIONS | CONSEQUENCE_GOOGLE_OPERATIONS
        # The set is a claim about the code, so it may not name an operation
        # that does not exist, and every operation outside it must plan.
        self.assertTrue(declared >= UNPROVABLE_OPERATIONS)
        payloads: dict[str, dict[str, object]] = {
            "docs_batch_update": {
                "requests": [{"insertText": {"location": {"index": 1}, "text": "x"}}]
            },
            "sheets_batch_update": {"requests": [{"addSheet": {"properties": {"title": "S"}}}]},
            "sheets_update_values": {"data": [{"range": "Sheet1!A1", "values": [["x"]]}]},
            "drive_change_permissions": {"type": "user", "role": "reader", "emailAddress": "a@b.c"},
            "contacts_create": {"names": [{"givenName": "Sam"}]},
            "contacts_update": {"names": [{"givenName": "Sam"}]},
        }
        response = {
            "id": "new-1",
            "resourceName": "people/new-1",  # a created contact's own name
            "replies": [{}],
            "documentId": "doc-1",
            "spreadsheetId": "sheet-1",
        }
        for operation in sorted(declared - UNPROVABLE_OPERATIONS):
            with self.subTest(operation=operation):
                # Contacts are addressed by their People resource name, and a
                # create learns it from the response rather than the request.
                resource = "people/c1" if operation.startswith("contacts_") else "resource-1"
                made = plan(
                    operation,
                    "" if operation == "contacts_create" else resource,
                    payloads.get(operation, {"name": "x"}),
                    response,
                    ENDPOINTS,
                )
                self.assertIsNotNone(made, f"{operation} has no authoritative read")


if __name__ == "__main__":
    unittest.main()


class OperationRegistryTests(unittest.TestCase):
    """One registry decides what is reachable, so nothing falls between two.

    The proposal tool's `google_operation` enum was a hand-written list of six.
    Classification is not: `classify_google_action` reads the payload, so a
    nominally routine write becomes a consequence when it crosses a bulk
    threshold, touches permissions, or adds an audience. An operation in that
    state was refused as routine -- it needs approval -- and refused as a
    proposal -- it is not in the enum. Unreachable, by two rules that were each
    individually correct.

    So the enums are derived from the policy's own sets, and these hold them to
    it: a write operation added to the policy is reachable the moment it is
    classified, and one removed disappears from both.
    """

    def schemas(self):
        from assistant.scotty_business import client_tool_schemas

        return {schema["name"]: schema for schema in client_tool_schemas()}

    def enum_of(self, tool: str) -> set[str]:
        schema = self.schemas()[tool]
        properties = schema["parameters"]["properties"]  # type: ignore[index]
        return set(properties["google_operation"]["enum"])

    def test_every_write_the_policy_knows_can_be_proposed(self) -> None:
        from assistant.scotty_business.google_policy import (
            CONSEQUENCE_GOOGLE_OPERATIONS,
            ROUTINE_GOOGLE_OPERATIONS,
        )

        writes = ROUTINE_GOOGLE_OPERATIONS | CONSEQUENCE_GOOGLE_OPERATIONS
        self.assertEqual(self.enum_of("scotty_propose"), writes)

    def test_an_operation_that_turns_consequential_by_payload_is_reachable(self) -> None:
        """The concrete case: a batch large enough to need approval.

        Under the hand-written enum this was refused as routine and rejected as
        a proposal, so the only way to make the change was to make it smaller.
        """

        from assistant.scotty_business.google_policy import (
            GoogleActionClass,
            classify_google_action,
        )

        payload = {"requests": [{"insertText": {"location": {"index": 1}, "text": "x"}}] * 30}
        self.assertIs(
            classify_google_action("docs_batch_update", payload), GoogleActionClass.CONSEQUENCE
        )
        self.assertIn("docs_batch_update", self.enum_of("scotty_propose"))

    def test_the_read_tool_carries_the_reads_and_the_routine_writes(self) -> None:
        """Routine work goes through the read tool on purpose.

        A reversible change needs no proposal, so it is reachable there. What
        must never be reachable there is a consequence, and that set is the
        one thing this enum may not contain.
        """

        from assistant.scotty_business.google_policy import (
            CONSEQUENCE_GOOGLE_OPERATIONS,
            GOOGLE_READ_OPERATIONS,
            ROUTINE_GOOGLE_OPERATIONS,
        )

        reads = self.enum_of("scotty_read")
        self.assertEqual(reads, set(GOOGLE_READ_OPERATIONS) | ROUTINE_GOOGLE_OPERATIONS)
        # A consequence reachable without a proposal would be the gate missing.
        self.assertFalse(reads & CONSEQUENCE_GOOGLE_OPERATIONS)
        self.assertIn("search_gmail", reads)


class ReachableEndToEndTests(unittest.TestCase):
    """The enum is one gate. This drives the whole path through the runtime."""

    def test_a_batch_too_large_to_run_routinely_can_actually_be_proposed(self) -> None:
        from test_provider_connection import principal_for, runtime

        from assistant.scotty_business.policy import Role
        from assistant.scotty_business.setup import GOOGLE_OAUTH_SCOPES

        # The exact scope set this deployment asks for; a narrower one is
        # refused at load, which is the point of it being fixed.
        accounts = {
            "main_operator": {
                "account_email": "operator.synthetic@example.invalid",
                "oauth_scopes": list(GOOGLE_OAUTH_SCOPES),
            }
        }
        with runtime({"google_workspace": accounts}, DISCORD_BOT_TOKEN="synthetic-discord") as live:
            operator = principal_for(live, Role.MAIN_OPERATOR)
            recorder = _Workspace()
            # The service holds its own reference, bound when the runtime was
            # built, so replacing the runtime's method alone changes nothing.
            live.service.google_workspace = lambda _principal: recorder
            live.google_connected[Role.MAIN_OPERATOR] = True
            live.connected["google_workspace"] = True
            # Configured for real by the fixture above, so a skip here would
            # mean the fixture stopped doing its job rather than a limitation.
            self.assertIsNotNone(live.config.google_for(Role.MAIN_OPERATOR))
            proposal = live.handle_propose(
                operator,
                {
                    "operation": "google_workspace_write",
                    "google_operation": "docs_batch_update",
                    "resource_id": "document-1",
                    "payload": {
                        "requests": [{"insertText": {"location": {"index": 1}, "text": "x"}}] * 30
                    },
                },
            )
            self.assertTrue(proposal["proposal_id"])
            # Proposed, not performed.
            self.assertEqual(recorder.calls, [])


class _Workspace:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def mutate(self, operation, resource_id, payload):
        from assistant.scotty_business.adapters.records import ProviderRecord, utc_now

        del payload
        self.calls.append((operation, resource_id))
        return ProviderRecord("google_workspace", resource_id, utc_now(), "v1", {}, ())
