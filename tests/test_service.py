from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime

import synthetic

from assistant.scotty_business.adapters import AmbiguousEffectError, ProviderError, ProviderRecord
from assistant.scotty_business.approvals import ApprovalError, ApprovalStore, ProposalStatus
from assistant.scotty_business.policy import Role
from assistant.scotty_business.service import ScottyService, normalize_address


def record(provider: str, source_id: str, revision: str, **fields: object) -> ProviderRecord:
    return ProviderRecord(
        provider, source_id, datetime(2026, 8, 28, tzinfo=UTC), revision, fields, ()
    )


class FakeTrello:
    def __init__(self) -> None:
        self.cards = {
            "source": record(
                "trello",
                "source",
                "s1",
                id="source",
                idBoard="board-1",
                idList="list-1",
                name="123 Synthetic Avenue",
                desc="source notes",
                idLabels=["label-1"],
                rentcast_id="property-1",
            ),
            "destination": record(
                "trello",
                "destination",
                "d1",
                id="destination",
                idBoard="board-1",
                idList="list-1",
                name="123 SYNTHETIC AVE.",
                desc="",
                idLabels=[],
                rentcast_id="property-1",
            ),
        }
        self.calls: list[str] = []
        self.readback_mismatch = False

    def get_card(self, card_id: str):
        self.calls.append(f"get:{card_id}")
        current = self.cards[card_id]
        if (
            self.readback_mismatch
            and card_id == "destination"
            and "update:destination" in self.calls
        ):
            return record("trello", "destination", "d3", **{**current.fields, "desc": "wrong"})
        return current

    def update_card(self, card_id: str, fields: dict[str, object]):
        self.calls.append(f"update:{card_id}")
        current = self.cards[card_id]
        self.cards[card_id] = record("trello", card_id, "d2", **{**current.fields, **fields})
        return self.cards[card_id]

    def archive_card(self, card_id: str):
        self.calls.append(f"archive:{card_id}")
        current = self.cards[card_id]
        self.cards[card_id] = record("trello", card_id, "s2", **{**current.fields, "closed": True})
        return self.cards[card_id]


class FakeGHL:
    def __init__(self, ambiguous: bool = False) -> None:
        self.ambiguous = ambiguous
        self.send_count = 0

    def get_contact(self, contact_id: str):
        return record(
            "ghl",
            contact_id,
            "contact-rev-1",
            id=contact_id,
            locationId="location-1",
            phone="+15550000001",
        )

    def send_sms(self, contact_id: str, destination: str, body: str):
        self.send_count += 1
        if self.ambiguous:
            raise AmbiguousEffectError("unknown")
        return {
            "message_id": "message-1",
            "conversation_id": "conversation-1",
            "contact_id": contact_id,
        }

    def get_message(self, conversation_id: str, message_id: str, contact_id: str):
        return record(
            "ghl",
            message_id,
            "message-rev-1",
            id=message_id,
            conversationId=conversation_id,
            contactId=contact_id,
            body="Synthetic follow-up",
        )


class FakeDiscord:
    def send_message(self, channel_id: str, content: str):
        return {"message_id": "discord-message-1", "channel_id": channel_id}

    def get_message(self, channel_id: str, message_id: str):
        return {"id": message_id, "channel_id": channel_id, "content": "Synthetic announcement"}


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="scotty-service-test-")
        self.now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
        self.config = synthetic.config()
        self.operator = next(p for p in self.config.principals if p.role == Role.MAIN_OPERATOR)
        self.employee = next(p for p in self.config.principals if p.role == Role.EMPLOYEE)
        self.store = ApprovalStore(
            os.path.join(self.tempdir.name, "approvals.db"), clock=lambda: self.now
        )
        self.store.initialize()
        self.trello = FakeTrello()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def service(self, *, ghl: FakeGHL | None = None) -> ScottyService:
        return ScottyService(
            self.config,
            self.store,
            trello=self.trello,
            ghl=ghl or FakeGHL(),
            rentcast=None,
            discord=FakeDiscord(),
            clock=lambda: self.now,
        )

    def test_address_normalization_is_deterministic_not_fuzzy(self) -> None:
        self.assertEqual(normalize_address(" 123 Synthetic Avenue. "), "123 synthetic avenue")
        self.assertNotEqual(
            normalize_address("123 Synthetic Ave"), normalize_address("123 Synthetic Avenue")
        )

    def test_merge_requires_exact_address_or_provider_identifier(self) -> None:
        service = self.service()
        self.trello.cards["source"] = record(
            "trello",
            "source",
            "s1",
            id="source",
            idBoard="board-1",
            idList="list-1",
            name="123 Synthetic Avenue",
        )
        self.trello.cards["destination"] = record(
            "trello",
            "destination",
            "d1",
            id="destination",
            idBoard="board-1",
            idList="list-1",
            name="123 Synthetic Ave",
        )
        with self.assertRaises(ProviderError):
            service.propose_trello_merge(self.operator, "source", "destination")

    def test_merge_preview_binds_conflicts_and_archives_only_after_readback(self) -> None:
        service = self.service()
        proposal = service.propose_trello_merge(self.operator, "source", "destination")
        # The conflict now records where the merged value came from, and
        # whether a person chose it or the rule did -- an approver reading
        # "destination" over an empty destination was reading a merge that
        # never happened.
        self.assertEqual(
            proposal.payload["conflicts"]["desc"],
            {
                "source": "source notes",
                "destination": "",
                "resolved_to": "source",
                "chosen_by_reviewer": False,
            },
        )
        self.assertIn("desc", proposal.payload["defaulted_conflicts"])
        approved = service.approve(self.operator, proposal.proposal_id, 1)
        result = service.execute(
            self.operator,
            approved.proposal_id,
            expected_version=2,
            execution_nonce=approved.execution_nonce,
        )
        self.assertEqual(result.status, ProposalStatus.VERIFIED)
        self.assertEqual(
            self.trello.calls[-4:],
            ["update:destination", "get:destination", "archive:source", "get:source"],
        )
        self.assertEqual(result.receipt["resulting_card_id"], "destination")

    def test_merge_readback_mismatch_becomes_unknown_without_archive(self) -> None:
        service = self.service()
        proposal = service.propose_trello_merge(self.operator, "source", "destination")
        approved = service.approve(self.operator, proposal.proposal_id, 1)
        self.trello.readback_mismatch = True
        result = service.execute(
            self.operator,
            approved.proposal_id,
            expected_version=2,
            execution_nonce=approved.execution_nonce,
        )
        self.assertEqual(result.status, ProposalStatus.UNKNOWN)
        self.assertNotIn("archive:source", self.trello.calls)

    def test_employee_sms_proposal_is_bound_to_operator_and_verified_by_readback(self) -> None:
        ghl = FakeGHL()
        service = self.service(ghl=ghl)
        proposal = service.propose_ghl_sms(
            self.employee,
            "contact-1",
            "+15550000001",
            "Synthetic follow-up",
        )
        self.assertEqual(proposal.approver, self.operator)
        self.assertEqual(proposal.target_ids, ("location-1", "contact-1", "+15550000001"))
        approved = service.approve(self.operator, proposal.proposal_id, 1)
        result = service.execute(
            self.operator,
            approved.proposal_id,
            expected_version=2,
            execution_nonce=approved.execution_nonce,
        )
        self.assertEqual(result.status, ProposalStatus.VERIFIED)
        self.assertEqual(ghl.send_count, 1)

    def test_ambiguous_sms_is_unknown_and_never_retried(self) -> None:
        ghl = FakeGHL(ambiguous=True)
        service = self.service(ghl=ghl)
        proposal = service.propose_ghl_sms(
            self.operator, "contact-1", "+15550000001", "Synthetic follow-up"
        )
        approved = service.approve(self.operator, proposal.proposal_id, 1)
        result = service.execute(
            self.operator,
            approved.proposal_id,
            expected_version=2,
            execution_nonce=approved.execution_nonce,
        )
        self.assertEqual(result.status, ProposalStatus.UNKNOWN)
        self.assertEqual(ghl.send_count, 1)
        with self.assertRaises(ApprovalError):
            service.execute(
                self.operator,
                result.proposal_id,
                expected_version=result.version,
                execution_nonce=result.execution_nonce,
            )
        self.assertEqual(ghl.send_count, 1)


if __name__ == "__main__":
    unittest.main()
