from __future__ import annotations

import unittest
from datetime import UTC, datetime

from assistant.scotty_business.adapters import (
    DiscordAdapter,
    GHLAdapter,
    HttpResponse,
    ProviderError,
    RentCastAdapter,
    TrelloAdapter,
)
from assistant.scotty_business.config import TrelloScope


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> HttpResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


class TrelloAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = TrelloScope("board-1", ("list-1", "list-2"), ("label-1",), ("field-1",))

    def test_read_retains_provider_provenance_and_missing_fields(self) -> None:
        transport = FakeTransport(
            [
                HttpResponse(
                    200,
                    {},
                    {
                        "id": "card-1",
                        "idBoard": "board-1",
                        "idList": "list-1",
                        "name": "123 Synthetic Ave",
                        "dateLastActivity": "2026-08-28T12:00:00Z",
                    },
                )
            ]
        )
        adapter = TrelloAdapter(transport, "key-secret", "token-secret", self.scope)
        record = adapter.get_card("card-1", retrieved_at=datetime(2026, 8, 28, tzinfo=UTC))
        self.assertEqual(record.provider, "trello")
        self.assertEqual(record.source_id, "card-1")
        self.assertEqual(record.source_revision, "2026-08-28T12:00:00Z")
        self.assertIn("customFieldItems", record.missing_attributes)
        self.assertNotIn("key-secret", repr(transport.calls))
        self.assertNotIn("token-secret", repr(transport.calls))

    def test_cross_board_or_unconfigured_list_response_fails_closed(self) -> None:
        for body in (
            {"id": "card-1", "idBoard": "other", "idList": "list-1"},
            {"id": "card-1", "idBoard": "board-1", "idList": "other"},
        ):
            with self.subTest(body=body):
                adapter = TrelloAdapter(FakeTransport([HttpResponse(200, {}, body)]), "k", "t", self.scope)
                with self.assertRaises(ProviderError):
                    adapter.get_card("card-1")

    def test_create_update_move_and_archive_are_scoped(self) -> None:
        responses = [
            HttpResponse(200, {}, {"id": "card-1", "idBoard": "board-1", "idList": "list-1", "dateLastActivity": "r1"}),
            HttpResponse(200, {}, {"id": "card-1", "idBoard": "board-1", "idList": "list-1", "dateLastActivity": "r2"}),
            HttpResponse(200, {}, {"id": "card-1", "idBoard": "board-1", "idList": "list-2", "dateLastActivity": "r3"}),
            HttpResponse(200, {}, {"id": "card-1", "idBoard": "board-1", "idList": "list-2", "closed": True, "dateLastActivity": "r4"}),
        ]
        transport = FakeTransport(responses)
        adapter = TrelloAdapter(transport, "k", "t", self.scope)
        adapter.create_card("list-1", {"name": "Synthetic property"})
        adapter.update_card("card-1", {"name": "Updated synthetic property"})
        adapter.move_card("card-1", "list-2")
        adapter.archive_card("card-1")
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "PUT", "PUT", "PUT"])
        self.assertTrue(all(str(call["url"]).startswith("https://api.trello.com/1/") for call in transport.calls))
        with self.assertRaises(ProviderError):
            adapter.create_card("other-list", {"name": "blocked"})
        with self.assertRaises(ProviderError):
            adapter.update_card("card-1", {"idMembers": ["member-1"]})

    def test_permanent_delete_is_not_an_adapter_capability(self) -> None:
        adapter = TrelloAdapter(FakeTransport([]), "k", "t", self.scope)
        self.assertFalse(hasattr(adapter, "delete_card"))


class GHLAdapterTests(unittest.TestCase):
    def test_location_is_bound_for_reads_and_sms(self) -> None:
        transport = FakeTransport(
            [
                HttpResponse(200, {}, {"contact": {"id": "contact-1", "locationId": "location-1", "dateUpdated": "rev-1"}}),
                HttpResponse(201, {}, {"messageId": "message-1", "conversationId": "conversation-1", "contactId": "contact-1"}),
            ]
        )
        adapter = GHLAdapter(transport, "pit-secret", "location-1")
        contact = adapter.get_contact("contact-1")
        receipt = adapter.send_sms("contact-1", "+15550000001", "Synthetic body")
        self.assertEqual(contact.source_revision, "rev-1")
        self.assertEqual(receipt["message_id"], "message-1")
        send_call = transport.calls[1]
        self.assertEqual(send_call["url"], "https://services.leadconnectorhq.com/conversations/messages")
        self.assertEqual(send_call["json_body"], {"type": "SMS", "contactId": "contact-1", "message": "Synthetic body"})
        self.assertNotIn("pit-secret", repr(transport.calls))

    def test_mismatched_location_or_contact_response_is_rejected(self) -> None:
        adapter = GHLAdapter(
            FakeTransport([HttpResponse(200, {}, {"contact": {"id": "other", "locationId": "location-1"}})]),
            "pit",
            "location-1",
        )
        with self.assertRaises(ProviderError):
            adapter.get_contact("contact-1")

    def test_authoritative_message_readback_binds_conversation_and_contact(self) -> None:
        transport = FakeTransport(
            [HttpResponse(200, {}, {"messages": {"messages": [{"id": "message-1", "contactId": "contact-1", "conversationId": "conversation-1", "body": "Synthetic body"}]}})]
        )
        adapter = GHLAdapter(transport, "pit", "location-1")
        record = adapter.get_message("conversation-1", "message-1", "contact-1")
        self.assertEqual(record.source_id, "message-1")
        self.assertEqual(record.fields["body"], "Synthetic body")


class RentCastAdapterTests(unittest.TestCase):
    def test_only_configured_get_endpoints_are_available(self) -> None:
        endpoints = ("/v1/properties", "/v1/avm/value", "/v1/avm/rent/long-term")
        transport = FakeTransport([HttpResponse(200, {}, {"id": "property-1", "formattedAddress": "123 Synthetic Ave"})])
        adapter = RentCastAdapter(transport, "rent-secret", endpoints)
        record = adapter.fetch("/v1/properties", {"address": "123 Synthetic Ave"})
        self.assertEqual(record.provider, "rentcast")
        self.assertEqual(transport.calls[0]["method"], "GET")
        self.assertNotIn("rent-secret", repr(transport.calls))
        with self.assertRaises(ProviderError):
            adapter.fetch("/v1/markets", {})
        self.assertFalse(hasattr(adapter, "post"))


class DiscordAdapterTests(unittest.TestCase):
    def test_send_is_fixed_to_configured_destination_with_safe_mentions(self) -> None:
        transport = FakeTransport([HttpResponse(200, {}, {"id": "message-1", "channel_id": "210"})])
        adapter = DiscordAdapter(transport, "bot-secret", ("210",))
        receipt = adapter.send_message("210", "Fixed synthetic message")
        self.assertEqual(receipt, {"message_id": "message-1", "channel_id": "210"})
        self.assertEqual(
            transport.calls[0]["json_body"],
            {"content": "Fixed synthetic message", "allowed_mentions": {"parse": []}},
        )
        self.assertNotIn("bot-secret", repr(transport.calls))
        with self.assertRaises(ProviderError):
            adapter.send_message("999", "blocked")


if __name__ == "__main__":
    unittest.main()
