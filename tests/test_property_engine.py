"""Typed property-card work against Trello: proven, idempotent, never doubled.

Every mutation here is read back before it counts as done, an ambiguous one
becomes `unknown` and is reconciled rather than retried, and the same intent
run twice — after a timeout, a crash, or a lost acknowledgement — produces one
card rather than two.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import synthetic

from assistant.scotty_business.adapters.http import AmbiguousEffectError, ProviderError
from assistant.scotty_business.adapters.records import ProviderRecord, utc_now
from assistant.scotty_business.policy import Role
from assistant.scotty_business.property_cards import Authority, FieldValue, PropertyCard
from assistant.scotty_business.property_engine import (
    EffectLog,
    EffectStatus,
    PropertyCardEngine,
)

LIST_ID = "list-1"


def value(text, source="operator", authority=Authority.VERIFIED, at="2026-09-01T00:00:00Z"):
    return FieldValue(value=text, source=source, source_id="", retrieved_at=at, authority=authority)


def card(card_id="new", address="88 Maple Ave, Dayton, OH 45402", **fields) -> PropertyCard:
    built = PropertyCard.new(card_id).with_field("address", value(address))
    for name, text in fields.items():
        built = built.with_field(name, value(text))
    return built


class FakeTrello:
    """A Trello that stores what it is told and answers reads from that state."""

    def __init__(self) -> None:
        self.cards: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, str]] = []
        self.next_id = 1
        self.swallow_acknowledgement = False
        self.readback: dict[str, object] | None = None

    def _record(self, body: dict[str, object]) -> ProviderRecord:
        return ProviderRecord("trello", str(body["id"]), utc_now(), "rev-1", dict(body), ())

    def create_card(self, list_id, fields):
        self.calls.append(("create", list_id))
        card_id = f"trello-{self.next_id}"
        self.next_id += 1
        self.cards[card_id] = {"id": card_id, "idList": list_id, "closed": False, **fields}
        if self.swallow_acknowledgement:
            raise AmbiguousEffectError("Trello acknowledgement was lost; reconcile before retry")
        return self._record(self.cards[card_id])

    def update_card(self, card_id, fields):
        self.calls.append(("update", card_id))
        if card_id not in self.cards:
            raise ProviderError("Trello card is unknown")
        self.cards[card_id].update(fields)
        return self._record(self.cards[card_id])

    def move_card(self, card_id, list_id):
        self.calls.append(("move", card_id))
        self.cards[card_id]["idList"] = list_id
        return self._record(self.cards[card_id])

    def archive_card(self, card_id):
        self.calls.append(("archive", card_id))
        self.cards[card_id]["closed"] = True
        return self._record(self.cards[card_id])

    def get_card(self, card_id):
        self.calls.append(("get", card_id))
        if self.readback is not None:
            return self._record({"id": card_id, **self.readback})
        if card_id not in self.cards:
            raise ProviderError("Trello card is unknown")
        return self._record(self.cards[card_id])

    def list_cards(self):
        self.calls.append(("list", ""))
        return tuple(self._record(body) for body in self.cards.values())


class EngineHarness(unittest.TestCase):
    def engine(self, trello=None):
        directory = tempfile.TemporaryDirectory(prefix="scotty-cards-")
        self.addCleanup(directory.cleanup)
        provider = trello or FakeTrello()
        log = EffectLog(Path(directory.name) / "effects.db")
        log.initialize()
        return (
            PropertyCardEngine(synthetic.config(), provider, log, list_id=LIST_ID),
            provider,
            log,
        )

    def actor(self, role=Role.MAIN_OPERATOR):
        return synthetic.config().principal_for(role)


class CreationTests(EngineHarness):
    def test_a_created_card_is_read_back_before_it_counts_as_done(self) -> None:
        engine, trello, _ = self.engine()
        outcome = engine.create(self.actor(), card())
        self.assertEqual(outcome.status, EffectStatus.VERIFIED)
        self.assertIn(("get", outcome.card_id), trello.calls)

    def test_a_lost_acknowledgement_is_unknown_and_never_creates_a_second_card(self) -> None:
        engine, trello, _ = self.engine()
        trello.swallow_acknowledgement = True
        first = engine.create(self.actor(), card())
        self.assertEqual(first.status, EffectStatus.UNKNOWN)
        self.assertEqual(len(trello.cards), 1)

        # The same intent again after the crash: it reconciles, finds the card
        # that did land, and reports it rather than creating another.
        trello.swallow_acknowledgement = False
        second = engine.create(self.actor(), card())
        self.assertEqual(second.status, EffectStatus.VERIFIED)
        self.assertEqual(len(trello.cards), 1)
        self.assertTrue(second.reconciled)

    def test_the_same_property_twice_is_refused_with_its_duplicate_evidence(self) -> None:
        engine, trello, _ = self.engine()
        engine.create(self.actor(), card())
        again = engine.create(self.actor(), card(address="88 Maple Avenue, Dayton OH 45402"))
        self.assertEqual(again.status, EffectStatus.DUPLICATE)
        self.assertTrue(again.duplicates)
        self.assertEqual(len(trello.cards), 1)

    def test_a_card_without_a_readable_address_is_refused_before_any_call(self) -> None:
        engine, trello, _ = self.engine()
        outcome = engine.create(self.actor(), PropertyCard.new("new"))
        self.assertEqual(outcome.status, EffectStatus.REFUSED)
        self.assertEqual(trello.calls, [])

    def test_a_readback_that_disagrees_is_unknown_not_verified(self) -> None:
        engine, trello, _ = self.engine()
        trello.readback = {"idList": "list-2", "name": "something else"}
        outcome = engine.create(self.actor(), card())
        self.assertEqual(outcome.status, EffectStatus.UNKNOWN)


class ReceiptTests(EngineHarness):
    def test_every_effect_keeps_the_actor_source_revision_and_payload_hash(self) -> None:
        engine, _, log = self.engine()
        outcome = engine.create(self.actor(), card())
        receipt = log.receipt(outcome.effect_id)
        self.assertEqual(receipt["actor"]["role"], "main_operator")
        self.assertEqual(receipt["actor"]["user_id"], synthetic.OPERATOR_USER)
        self.assertTrue(receipt["payload_hash"])
        self.assertTrue(receipt["source_revision"])
        self.assertIn("changed_fields", receipt)

    def test_a_receipt_never_carries_a_credential_or_a_raw_provider_body(self) -> None:
        engine, _, log = self.engine()
        outcome = engine.update(
            self.actor(),
            "trello-1",
            card("trello-1", asking_price="125000"),
            existing=card("trello-1", asking_price="120000"),
        )
        rendered = str(log.receipt(outcome.effect_id))
        for forbidden in ("token", "api_key", "Authorization"):
            self.assertNotIn(forbidden, rendered)


class ReformatAndTemplateTests(EngineHarness):
    def test_reformatting_keeps_every_verified_value_and_its_provenance(self) -> None:
        engine, _, _ = self.engine()
        original = card("trello-1", asking_price="125000", seller_name="A. Synthetic")
        reformatted = engine.reformat(original)
        self.assertEqual(reformatted.fields["asking_price"].value, "125000")
        self.assertEqual(reformatted.fields["asking_price"].source, "operator")
        self.assertEqual(reformatted.fields["asking_price"].authority, Authority.VERIFIED)

    def test_a_template_fills_only_fields_the_card_does_not_already_have(self) -> None:
        engine, _, _ = self.engine()
        original = card("trello-1", seller_stage="contacted")
        applied = engine.apply_template(
            original, {"seller_stage": "new", "next_step": "call the seller"}
        )
        self.assertEqual(applied.fields["seller_stage"].value, "contacted")
        self.assertEqual(applied.fields["next_step"].value, "call the seller")
        self.assertEqual(applied.fields["next_step"].authority, Authority.CONFIGURED)


class BulkDryRunTests(EngineHarness):
    def test_a_bulk_plan_reports_the_exact_count_and_diff_without_calling(self) -> None:
        engine, trello, _ = self.engine()
        for index in range(3):
            engine.create(self.actor(), card(address=f"{index + 1} Oak Ln, Dayton, OH 45402"))
        trello.calls.clear()

        plan = engine.dry_run(
            self.actor(),
            "move",
            [f"trello-{index + 1}" for index in range(3)],
            {"list_id": "list-2"},
        )
        self.assertEqual(plan.affected, 3)
        self.assertEqual(len(plan.changes), 3)
        self.assertTrue(all(change["to"] == "list-2" for change in plan.changes))
        # A dry run reads to build the diff and mutates nothing at all.
        self.assertTrue(all(kind in {"get", "list"} for kind, _ in trello.calls))

    def test_a_batch_larger_than_the_bounded_review_is_refused(self) -> None:
        engine, _, _ = self.engine()
        with self.assertRaises(ValueError):
            engine.dry_run(self.actor(), "move", [f"card-{index}" for index in range(60)], {})

    def test_an_unknown_bulk_operation_fails_closed(self) -> None:
        engine, _, _ = self.engine()
        with self.assertRaises(ValueError):
            engine.dry_run(self.actor(), "delete_everything", ["trello-1"], {})


class ArchiveGateTests(EngineHarness):
    def test_archiving_is_a_consequence_and_is_not_done_by_the_routine_path(self) -> None:
        from assistant.scotty_business.property_engine import consequence_operations

        self.assertIn("archive", consequence_operations())
        engine, trello, _ = self.engine()
        with self.assertRaises(PermissionError):
            engine.routine(self.actor(), "archive", "trello-1", {})
        self.assertEqual(trello.calls, [])


class RuntimeSurfaceTests(unittest.TestCase):
    """The typed operations a client actually reaches, bound to their actor."""

    def runtime(self):
        from test_provider_connection import runtime

        return runtime(
            DISCORD_BOT_TOKEN="synthetic-discord",
            SCOTTY_TRELLO_API_KEY="synthetic-key",
            SCOTTY_TRELLO_TOKEN="synthetic-token",  # noqa: S106 - synthetic
        )

    def actor(self, runtime, role=Role.MAIN_OPERATOR):
        return runtime.config.principal_for(role)

    def test_address_normalization_needs_no_provider_at_all(self) -> None:
        with self.runtime() as runtime:
            answer = runtime.handle_read(
                self.actor(runtime),
                {
                    "operation": "property_card",
                    "card_operation": "normalize_address",
                    "address": "1234 North West Elm Street, Springfield, Illinois 62704",
                },
            )
            self.assertEqual(answer["postal_code"], "62704")
            self.assertTrue(answer["complete"])

    def test_a_merge_preview_through_the_tool_surfaces_the_conflict(self) -> None:
        with self.runtime() as runtime:
            left = card("card-1", asking_price="125000").as_json()
            right = card("card-2", asking_price="130000").as_json()
            answer = runtime.handle_read(
                self.actor(runtime),
                {
                    "operation": "property_card",
                    "card_operation": "preview_merge",
                    "card": left,
                    "other_card": right,
                },
            )
            self.assertIn("asking_price", answer["unresolved"])
            self.assertTrue(answer["payload_hash"])

    def test_an_unknown_card_operation_fails_closed(self) -> None:
        with self.runtime() as runtime, self.assertRaises(ValueError):
            runtime.handle_read(
                self.actor(runtime),
                {"operation": "property_card", "card_operation": "drop_board"},
            )

    def test_the_property_tool_refuses_a_model_supplied_actor(self) -> None:
        from assistant.scotty_business.provider_identity import ProviderIdentityError

        with self.runtime() as runtime, self.assertRaises(ProviderIdentityError):
            runtime.handle_read(
                self.actor(runtime),
                {
                    "operation": "property_card",
                    "card_operation": "normalize_address",
                    "address": "1 Oak Ln, Dayton, OH 45402",
                    "as_user": "302000000000000001",
                },
            )


if __name__ == "__main__":
    unittest.main()


class AmbiguousRetryTests(EngineHarness):
    """An unresolved change is reconciled, never repeated."""

    def test_an_unresolved_update_reconciles_instead_of_writing_again(self) -> None:
        engine, trello, _ = self.engine()
        engine.create(self.actor(), card())
        card_id = next(iter(trello.cards))
        target = card(card_id, asking_price="125000")
        before = card(card_id)

        trello.readback = {"name": "88 Maple Ave, Dayton, OH 45402"}
        first = engine.update(self.actor(), card_id, target, existing=before)
        self.assertEqual(first.status, EffectStatus.UNKNOWN)
        writes = [call for call in trello.calls if call[0] == "update"]

        # The card actually did take the change; only the answer was lost.
        trello.readback = None
        second = engine.update(self.actor(), card_id, target, existing=before)
        self.assertEqual(second.status, EffectStatus.VERIFIED)
        self.assertTrue(second.reconciled)
        self.assertEqual([call for call in trello.calls if call[0] == "update"], writes)

    def test_an_unresolved_move_reconciles_instead_of_moving_again(self) -> None:
        engine, trello, log = self.engine()
        engine.create(self.actor(), card())
        card_id = next(iter(trello.cards))

        record, _ = log.claim(
            operation="move",
            key=f"{card_id}:list-2",
            actor=self.actor(),
            payload_hash="list-2",
            source_revision="property-card-v2",
        )
        self.assertEqual(record.status, EffectStatus.UNKNOWN)
        trello.calls.clear()

        # The list still says list-1, so the earlier move is still unresolved.
        pending = engine.routine(self.actor(), "move", card_id, {"list_id": "list-2"})
        self.assertEqual(pending.status, EffectStatus.UNKNOWN)
        self.assertNotIn("move", {kind for kind, _ in trello.calls})

        # Once the card reads back in the destination, it reconciles verified.
        trello.cards[card_id]["idList"] = "list-2"
        settled = engine.routine(self.actor(), "move", card_id, {"list_id": "list-2"})
        self.assertEqual(settled.status, EffectStatus.VERIFIED)
        self.assertTrue(settled.reconciled)
        self.assertNotIn("move", {kind for kind, _ in trello.calls})
