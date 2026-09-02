"""The Trello surface the contract promises, exercised rather than described.

Three kinds of gap were open here. Operations the contract names had no
executable path -- `label` was declared routine and fell through to "not
permitted"; there was no unarchive at all; bulk update and move were classified
as consequences and then never executed; a merge preview could have its
conflicts chosen and the choice could not be committed. Filtering, querying and
sorting a board existed only as a raw card list.

And underneath those, a correctness one: an approved create, update, move or
archive was marked `verified` on the strength of the provider's own reply to
the write. That is the thing the contract says never to do. Trello answering
"200, here is the card" is Trello describing what it believes it did; a second,
independent read is what establishes that it did it. The merge path already
read back, and the others did not.

These tests are written against the effect, not the call: they drive the
adapter with recorded shapes, then ask what state the ledger settled on.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from assistant.scotty_business.adapters.http import AmbiguousEffectError, ProviderError
from assistant.scotty_business.adapters.records import ProviderRecord
from assistant.scotty_business.approvals import ApprovalError
from assistant.scotty_business.policy import Principal, Role

MOMENT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def actor(role: Role = Role.MAIN_OPERATOR) -> Principal:
    return Principal(
        guild_id="G", channel_id="C", user_id=f"U-{role.value}", role=role, message_id="M"
    )


class FakeTrello:
    """A board that answers reads from state and records every write.

    The point of holding state is that a readback can disagree with the write
    that preceded it. `acknowledge_only` makes the provider answer a write
    successfully and then not actually change anything -- which is exactly the
    case an acknowledgement-trusting verification gets wrong.
    """

    def __init__(self, cards: dict[str, dict[str, object]] | None = None) -> None:
        self.cards: dict[str, dict[str, object]] = cards or {}
        self.calls: list[tuple[str, str]] = []
        self.acknowledge_only = False
        self.ambiguous_on: set[str] = set()
        #: Whether list_all_cards really saw the whole board. Trello pages, so
        #: a large board comes back truncated and says so.
        self.board_complete = True

    def _record(self, card_id: str) -> ProviderRecord:
        fields = dict(self.cards.get(card_id, {}))
        # A revision that changes with the card, built from the whole body so
        # that a list-valued field such as `idLabels` counts toward it too.
        revision = json.dumps(fields, sort_keys=True, default=str)
        return ProviderRecord("trello", card_id, MOMENT, revision, fields, ())

    def get_card(self, card_id: str, *, retrieved_at=None) -> ProviderRecord:
        del retrieved_at
        self.calls.append(("get_card", card_id))
        if card_id not in self.cards:
            raise ProviderError("no such card")
        return self._record(card_id)

    def _write(self, name: str, card_id: str, apply) -> ProviderRecord:
        self.calls.append((name, card_id))
        if name in self.ambiguous_on:
            raise AmbiguousEffectError(f"{name} outcome unknown")
        if not self.acknowledge_only:
            apply()
        return self._record(card_id)

    def create_card(self, list_id, fields) -> ProviderRecord:
        card_id = f"card-{len(self.cards) + 1}"

        def apply() -> None:
            self.cards[card_id] = {"idList": list_id, **dict(fields)}

        return self._write("create_card", card_id, apply)

    def update_card(self, card_id, fields) -> ProviderRecord:
        return self._write("update_card", card_id, lambda: self.cards[card_id].update(fields))

    def move_card(self, card_id, list_id) -> ProviderRecord:
        return self._write(
            "move_card", card_id, lambda: self.cards[card_id].update({"idList": list_id})
        )

    def archive_card(self, card_id) -> ProviderRecord:
        return self._write(
            "archive_card", card_id, lambda: self.cards[card_id].update({"closed": True})
        )

    def unarchive_card(self, card_id) -> ProviderRecord:
        return self._write(
            "unarchive_card", card_id, lambda: self.cards[card_id].update({"closed": False})
        )

    def set_labels(self, card_id, label_ids) -> ProviderRecord:
        return self._write(
            "set_labels", card_id, lambda: self.cards[card_id].update({"idLabels": list(label_ids)})
        )

    def list_all_cards(self):
        return (
            tuple(self._record(card_id) for card_id in sorted(self.cards)),
            self.board_complete,
        )


class VerificationTests(unittest.TestCase):
    """An acknowledgement is not a verification, on any path."""

    def service(self, trello):
        import synthetic

        from assistant.scotty_business.approvals import ApprovalStore
        from assistant.scotty_business.service import ScottyService

        directory = tempfile.TemporaryDirectory(prefix="scotty-trello-ops-")
        self.addCleanup(directory.cleanup)
        store = ApprovalStore(Path(directory.name) / "approvals.db")
        store.initialize()
        unused = object()
        return (
            ScottyService(
                synthetic.config(),
                store,
                trello=lambda _actor: trello,
                ghl=unused,
                rentcast=None,
                discord=unused,
            ),
            store,
        )

    def approved(self, service, store, proposal):
        approver = actor(Role.MAIN_OPERATOR)
        granted = store.approve(proposal.proposal_id, approver, proposal.version)
        return service.execute(
            approver,
            proposal.proposal_id,
            expected_version=granted.version,
            execution_nonce=granted.execution_nonce,
        )

    def test_an_update_the_provider_only_acknowledged_is_not_verified(self) -> None:
        trello = FakeTrello({"card-1": {"idList": "list-1", "name": "before"}})
        service, store = self.service(trello)
        proposal = service.propose_trello_action(
            actor(), "update", "card-1", fields={"name": "after"}
        )
        # Trello answers the write happily and changes nothing.
        trello.acknowledge_only = True
        settled = self.approved(service, store, proposal)
        self.assertEqual(settled.status.value, "unknown")
        self.assertIs(settled.receipt.get("verified"), False)

    def test_an_update_that_really_landed_is_verified_by_reading_it_back(self) -> None:
        trello = FakeTrello({"card-1": {"idList": "list-1", "name": "before"}})
        service, store = self.service(trello)
        proposal = service.propose_trello_action(
            actor(), "update", "card-1", fields={"name": "after"}
        )
        settled = self.approved(service, store, proposal)
        self.assertEqual(settled.status.value, "verified")
        # Read back after the write, not before it.
        writes = [index for index, (name, _) in enumerate(trello.calls) if name == "update_card"]
        reads = [index for index, (name, _) in enumerate(trello.calls) if name == "get_card"]
        self.assertTrue(any(read > writes[-1] for read in reads))

    def test_a_move_the_provider_only_acknowledged_is_not_verified(self) -> None:
        trello = FakeTrello({"card-1": {"idList": "list-1"}})
        service, store = self.service(trello)
        proposal = service.propose_trello_action(
            actor(), "move", "card-1", destination_list_id="list-2"
        )
        trello.acknowledge_only = True
        settled = self.approved(service, store, proposal)
        self.assertEqual(settled.status.value, "unknown")

    def test_an_archive_the_provider_only_acknowledged_is_not_verified(self) -> None:
        trello = FakeTrello({"card-1": {"idList": "list-1"}})
        service, store = self.service(trello)
        proposal = service.propose_trello_action(actor(), "archive", "card-1")
        trello.acknowledge_only = True
        settled = self.approved(service, store, proposal)
        self.assertEqual(settled.status.value, "unknown")

    def test_a_created_card_is_verified_by_finding_it_afterwards(self) -> None:
        trello = FakeTrello()
        service, store = self.service(trello)
        proposal = service.propose_trello_create(
            actor(), "list-1", {"name": "123 Main St", "normalized_address": "123 main st"}
        )
        settled = self.approved(service, store, proposal)
        self.assertEqual(settled.status.value, "verified")
        self.assertTrue(settled.receipt.get("resulting_card_id"))

    def test_a_create_the_provider_only_acknowledged_is_not_verified(self) -> None:
        trello = FakeTrello()
        service, store = self.service(trello)
        proposal = service.propose_trello_create(actor(), "list-1", {"name": "123 Main St"})
        trello.acknowledge_only = True
        settled = self.approved(service, store, proposal)
        self.assertEqual(settled.status.value, "unknown")
        self.assertIs(settled.receipt.get("verified"), False)


class ReachableOperationTests(unittest.TestCase):
    """Every operation the contract names has a path that runs it."""

    def test_every_declared_routine_operation_is_implemented(self) -> None:
        """A declared operation that falls through to "not permitted" is worse
        than an absent one: it reads as supported everywhere it is listed."""

        from assistant.scotty_business.property_engine import ROUTINE_OPERATIONS

        engine = self.engine(FakeTrello({"card-1": {"idList": "list-1"}}))
        for operation in sorted(ROUTINE_OPERATIONS - {"create", "update", "reformat"}):
            with self.subTest(operation=operation):
                try:
                    engine.routine(actor(), operation, "card-1", self.arguments(operation))
                except ValueError as exc:  # pragma: no cover - the failure is the report
                    self.fail(f"{operation} is declared and unreachable: {exc}")

    def arguments(self, operation: str) -> dict[str, object]:
        return {
            "move": {"list_id": "list-2"},
            "label": {"label_ids": ["label-1"]},
            "unarchive": {},
        }.get(operation, {})

    def engine(self, trello):
        import synthetic

        from assistant.scotty_business.property_engine import EffectLog, PropertyCardEngine

        directory = tempfile.TemporaryDirectory(prefix="scotty-trello-engine-")
        self.addCleanup(directory.cleanup)
        effects = EffectLog(Path(directory.name) / "effects.db")
        effects.initialize()
        return PropertyCardEngine(synthetic.config(), trello, effects)

    def test_a_label_change_is_applied_and_read_back(self) -> None:
        trello = FakeTrello({"card-1": {"idList": "list-1", "idLabels": []}})
        engine = self.engine(trello)
        outcome = engine.routine(actor(), "label", "card-1", {"label_ids": ["label-1"]})
        self.assertEqual(outcome.status.value, "verified")
        self.assertEqual(trello.cards["card-1"]["idLabels"], ["label-1"])

    def test_a_label_the_board_does_not_have_is_refused_before_the_write(self) -> None:
        trello = FakeTrello({"card-1": {"idList": "list-1", "idLabels": []}})
        engine = self.engine(trello)
        with self.assertRaises(ValueError):
            engine.routine(actor(), "label", "card-1", {"label_ids": ["not-on-this-board"]})
        self.assertNotIn("set_labels", [name for name, _ in trello.calls])

    def test_an_archived_card_can_be_brought_back(self) -> None:
        trello = FakeTrello({"card-1": {"idList": "list-1", "closed": True}})
        engine = self.engine(trello)
        outcome = engine.routine(actor(), "unarchive", "card-1", {})
        self.assertEqual(outcome.status.value, "verified")
        self.assertIs(trello.cards["card-1"]["closed"], False)

    def test_a_label_the_provider_only_acknowledged_is_unknown(self) -> None:
        trello = FakeTrello({"card-1": {"idList": "list-1", "idLabels": []}})
        trello.acknowledge_only = True
        engine = self.engine(trello)
        outcome = engine.routine(actor(), "label", "card-1", {"label_ids": ["label-1"]})
        self.assertEqual(outcome.status.value, "unknown")


class RetryAfterFailureTests(unittest.TestCase):
    """A definite failure has to be retryable; only doubt blocks a retry."""

    def engine(self, trello):
        import synthetic

        from assistant.scotty_business.property_engine import EffectLog, PropertyCardEngine

        directory = tempfile.TemporaryDirectory(prefix="scotty-trello-retry-")
        self.addCleanup(directory.cleanup)
        effects = EffectLog(Path(directory.name) / "effects.db")
        effects.initialize()
        return PropertyCardEngine(synthetic.config(), trello, effects)

    def test_a_move_that_definitely_failed_can_be_tried_again(self) -> None:
        """Found by reading this back: a 500 stranded the card forever.

        The claim is written before the call, and only AmbiguousEffectError
        settled it. A plain ProviderError -- a refusal, a bad gateway, anything
        the provider answered definitely -- propagated with the row still
        UNKNOWN. The next attempt then saw an unresolved claim, refused to
        repeat it, and there was no path that ever settled it: the card could
        not be moved again by anybody.
        """

        trello = FakeTrello({"card-1": {"idList": "list-1"}})
        engine = self.engine(trello)

        def refusing(card_id, list_id):
            raise ProviderError("Trello said no")

        trello.move_card = refusing  # type: ignore[method-assign]
        with self.assertRaises(ProviderError):
            engine.routine(actor(), "move", "card-1", {"list_id": "list-2"})

        # The provider answered definitely, so nothing is in doubt and the same
        # move is allowed to run again.
        trello.move_card = FakeTrello.move_card.__get__(trello)  # type: ignore[method-assign]
        outcome = engine.routine(actor(), "move", "card-1", {"list_id": "list-2"})
        self.assertEqual(outcome.status.value, "verified")
        self.assertEqual(trello.cards["card-1"]["idList"], "list-2")

    def test_a_move_whose_outcome_is_unknown_still_blocks_a_blind_retry(self) -> None:
        """The case that must keep refusing: nobody knows what happened."""

        trello = FakeTrello({"card-1": {"idList": "list-1"}})
        trello.ambiguous_on = {"move_card"}
        engine = self.engine(trello)
        first = engine.routine(actor(), "move", "card-1", {"list_id": "list-2"})
        self.assertEqual(first.status.value, "unknown")

        trello.ambiguous_on = set()
        again = engine.routine(actor(), "move", "card-1", {"list_id": "list-2"})
        self.assertEqual(again.status.value, "unknown")
        self.assertIn("reconcile", again.reason)
        # And it really did not write a second time.
        self.assertEqual(trello.cards["card-1"]["idList"], "list-1")

    def test_a_bulk_card_that_definitely_failed_is_not_stranded(self) -> None:
        trello = FakeTrello({"card-1": {"idList": "list-1"}, "card-2": {"idList": "list-1"}})
        engine = self.engine(trello)
        plan = engine.dry_run(actor(), "move", ["card-1", "card-2"], {"list_id": "list-2"})

        real = trello.move_card
        refused: list[str] = []

        def selective(card_id, list_id):
            if card_id == "card-2" and not refused:
                refused.append(card_id)
                raise ProviderError("Trello said no")
            return real(card_id, list_id)

        trello.move_card = selective  # type: ignore[method-assign]
        first = engine.run_bulk(actor(), plan, plan.payload_hash())
        self.assertEqual(first.verified, ("card-1",))
        self.assertEqual(first.failed, ("card-2",))

        # Re-running the approved batch finishes the card that failed, and
        # leaves the one that already landed alone.
        again = engine.run_bulk(actor(), plan, plan.payload_hash())
        self.assertEqual(sorted(again.verified), ["card-1", "card-2"])
        self.assertEqual(trello.cards["card-2"]["idList"], "list-2")


class QueryTests(unittest.TestCase):
    """Filtering, sorting and querying a board, as a typed read."""

    def board(self):
        return FakeTrello(
            {
                "card-1": {"name": "3 Oak", "idList": "list-1", "idLabels": ["label-1"]},
                "card-2": {"name": "1 Elm", "idList": "list-2", "idLabels": []},
                "card-3": {"name": "2 Ash", "idList": "list-1", "idLabels": ["label-1"]},
                "card-4": {"name": "4 Fir", "idList": "list-1", "closed": True},
            }
        )

    def query(self, trello, **options):
        from assistant.scotty_business.property_engine import query_cards

        return query_cards(trello, **options)

    def test_an_unfiltered_query_leaves_archived_cards_out(self) -> None:
        found = self.query(self.board())
        self.assertEqual([card.source_id for card in found], ["card-1", "card-2", "card-3"])

    def test_a_query_filters_by_list_and_by_label(self) -> None:
        board = self.board()
        by_list = self.query(board, list_id="list-1")
        self.assertEqual([card.source_id for card in by_list], ["card-1", "card-3"])
        by_label = self.query(board, label_id="label-1")
        self.assertEqual([card.source_id for card in by_label], ["card-1", "card-3"])

    def test_a_query_sorts_by_a_named_field_in_either_direction(self) -> None:
        board = self.board()
        ascending = self.query(board, sort_by="name")
        self.assertEqual([card.fields["name"] for card in ascending], ["1 Elm", "2 Ash", "3 Oak"])
        descending = self.query(board, sort_by="name", descending=True)
        self.assertEqual([card.fields["name"] for card in descending], ["3 Oak", "2 Ash", "1 Elm"])

    def test_a_query_can_ask_for_the_archived_ones_on_purpose(self) -> None:
        found = self.query(self.board(), archived=True)
        self.assertEqual([card.source_id for card in found], ["card-4"])

    def test_a_query_over_a_board_it_could_not_fully_read_is_refused(self) -> None:
        """Found by reading this back: a partial board answered as if whole.

        Trello pages, and `list_all_cards` says whether it reached the end.
        The duplicate check already respects that -- answering "no match" from
        the first page of a larger board is how one property gets two cards --
        and this query ignored it. "No cards in the offers list" from a board
        that was never fully read is a wrong answer that looks like a right
        one, so it is refused instead.
        """

        board = self.board()
        board.board_complete = False
        with self.assertRaises(ProviderError):
            self.query(board)

    def test_an_unknown_sort_field_is_refused_rather_than_ignored(self) -> None:
        with self.assertRaises(ValueError):
            self.query(self.board(), sort_by="whatever")

    def test_a_query_is_bounded(self) -> None:
        found = self.query(self.board(), limit=2)
        self.assertEqual(len(found), 2)


class BulkTests(unittest.TestCase):
    """A batch that can be previewed and then actually run, once."""

    def engine(self, trello):
        import synthetic

        from assistant.scotty_business.property_engine import EffectLog, PropertyCardEngine

        directory = tempfile.TemporaryDirectory(prefix="scotty-trello-bulk-")
        self.addCleanup(directory.cleanup)
        effects = EffectLog(Path(directory.name) / "effects.db")
        effects.initialize()
        return PropertyCardEngine(synthetic.config(), trello, effects)

    def board(self):
        return FakeTrello(
            {
                "card-1": {"idList": "list-1", "name": "one"},
                "card-2": {"idList": "list-1", "name": "two"},
                "card-3": {"idList": "list-1", "name": "three"},
            }
        )

    def test_a_batch_runs_only_the_plan_it_was_approved_for(self) -> None:
        """The dry run is the contract; the execution has to match it.

        A plan approved for three cards that quietly runs on four is the whole
        reason batches are consequence-gated. So the plan's own hash travels
        with the approval and is checked before anything moves.
        """

        trello = self.board()
        engine = self.engine(trello)
        plan = engine.dry_run(actor(), "move", ["card-1", "card-2"], {"list_id": "list-2"})
        outcome = engine.run_bulk(actor(), plan, plan.payload_hash())
        self.assertEqual(outcome.verified, ("card-1", "card-2"))
        self.assertEqual(trello.cards["card-1"]["idList"], "list-2")
        self.assertEqual(trello.cards["card-3"]["idList"], "list-1")

    def test_a_batch_whose_plan_changed_underneath_it_is_refused(self) -> None:
        trello = self.board()
        engine = self.engine(trello)
        plan = engine.dry_run(actor(), "move", ["card-1"], {"list_id": "list-2"})
        with self.assertRaises(PermissionError):
            engine.run_bulk(actor(), plan, "a-hash-from-some-other-plan")
        self.assertEqual(trello.cards["card-1"]["idList"], "list-1")

    def test_running_the_same_batch_twice_moves_nothing_twice(self) -> None:
        trello = self.board()
        engine = self.engine(trello)
        plan = engine.dry_run(actor(), "move", ["card-1"], {"list_id": "list-2"})
        engine.run_bulk(actor(), plan, plan.payload_hash())
        writes = len([name for name, _ in trello.calls if name == "move_card"])
        again = engine.run_bulk(actor(), plan, plan.payload_hash())
        self.assertEqual(len([name for name, _ in trello.calls if name == "move_card"]), writes)
        # The second run reports the same cards, reconciled rather than redone.
        self.assertEqual(again.verified, ("card-1",))

    def test_one_card_failing_does_not_take_the_others_with_it(self) -> None:
        trello = self.board()
        engine = self.engine(trello)
        plan = engine.dry_run(
            actor(), "move", ["card-1", "card-2", "card-3"], {"list_id": "list-2"}
        )

        real = trello.move_card

        def selective(card_id, list_id):
            if card_id == "card-2":
                raise AmbiguousEffectError("move outcome unknown")
            return real(card_id, list_id)

        trello.move_card = selective  # type: ignore[method-assign]
        outcome = engine.run_bulk(actor(), plan, plan.payload_hash())
        self.assertEqual(outcome.verified, ("card-1", "card-3"))
        self.assertEqual(outcome.unresolved, ("card-2",))

    def test_a_batch_larger_than_the_cap_is_refused_at_the_preview(self) -> None:
        from assistant.scotty_business.property_engine import MAX_BULK_CARDS

        engine = self.engine(self.board())
        with self.assertRaises(ValueError):
            engine.dry_run(actor(), "move", [f"card-{n}" for n in range(MAX_BULK_CARDS + 1)], {})


class BulkApprovalTests(unittest.TestCase):
    """A batch reaches the board only through an approval that named it."""

    def runtime(self):
        from test_provider_connection import runtime

        return runtime(
            DISCORD_BOT_TOKEN="synthetic-discord",
            SCOTTY_TRELLO_API_KEY="shared-key",
            SCOTTY_TRELLO_TOKEN_MAIN_OPERATOR="operator-token",  # noqa: S106 - synthetic
        )

    def test_a_bulk_move_is_proposed_with_its_plan_and_not_run_on_the_spot(self) -> None:
        from test_provider_connection import principal_for

        with self.runtime() as live:
            operator = principal_for(live, Role.MAIN_OPERATOR)
            board = FakeTrello({"card-1": {"idList": "list-1"}, "card-2": {"idList": "list-1"}})
            proposal = self.propose(live, operator, board)
            # Proposed, not performed: the board has not moved.
            self.assertEqual(board.cards["card-1"]["idList"], "list-1")
            self.assertTrue(proposal["proposal_id"])
            self.assertNotIn("move_card", [name for name, _ in board.calls])

    def engine(self, trello):
        import synthetic

        from assistant.scotty_business.property_engine import EffectLog, PropertyCardEngine

        if not hasattr(self, "_effects"):
            directory = tempfile.TemporaryDirectory(prefix="scotty-bulk-approval-")
            self.addCleanup(directory.cleanup)
            self._effects = EffectLog(Path(directory.name) / "effects.db")
            self._effects.initialize()
        return PropertyCardEngine(synthetic.config(), trello, self._effects)

    def propose(self, live, operator, board):
        engine = self.engine(board)
        live._property_engine = lambda _principal: engine  # type: ignore[method-assign]
        # The service holds its own reference, bound when the runtime was
        # built, so replacing the runtime's method alone changes nothing.
        live.service.property_engine = lambda _principal: engine
        return live.handle_propose(
            operator,
            {
                "operation": "trello_bulk",
                "card_operation_target": "move",
                "card_ids": ["card-1", "card-2"],
                "payload": {"list_id": "list-2"},
            },
        )

    def approve(self, live, operator, proposal):
        return live.handle_approval(
            operator,
            {
                "action": "approve",
                "proposal_id": proposal["proposal_id"],
                "expected_version": proposal["version"],
            },
        )

    def spend(self, approved):
        return {
            "expected_version": approved["version"],
            "execution_nonce": approved["execution_nonce"],
        }

    def test_an_unapproved_batch_does_not_run(self) -> None:
        """Found by reading this back: the gate was not wired to the door.

        `execute_bulk` read the proposal for its plan and never asked whether
        anybody had approved it, never claimed it, and never settled it. A
        caller could propose a mass edit and run it in the next call -- which
        is the entire thing bulk operations are consequence-classified to
        prevent. My own test called it "an approved batch" and approved
        nothing, so it passed and told me the opposite.
        """

        from test_provider_connection import principal_for

        with self.runtime() as live:
            operator = principal_for(live, Role.MAIN_OPERATOR)
            board = FakeTrello({"card-1": {"idList": "list-1"}, "card-2": {"idList": "list-1"}})
            proposal = self.propose(live, operator, board)
            # A real version and a nonce that nobody issued, so what refuses
            # this is the approval state machine rather than argument checking.
            with self.assertRaises((ApprovalError, PermissionError)):
                live.execute_bulk(
                    operator,
                    proposal["proposal_id"],
                    {
                        "expected_version": proposal["version"],
                        "execution_nonce": "not-a-nonce-anybody-issued",
                    },
                )
            self.assertEqual(board.cards["card-1"]["idList"], "list-1")
            self.assertEqual(board.cards["card-2"]["idList"], "list-1")

    def test_an_approved_batch_runs_and_settles_the_proposal(self) -> None:
        from test_provider_connection import principal_for

        with self.runtime() as live:
            operator = principal_for(live, Role.MAIN_OPERATOR)
            board = FakeTrello({"card-1": {"idList": "list-1"}, "card-2": {"idList": "list-1"}})
            proposal = self.propose(live, operator, board)
            approved = self.approve(live, operator, proposal)
            outcome = live.execute_bulk(operator, proposal["proposal_id"], self.spend(approved))
            self.assertEqual(outcome["verified"], ["card-1", "card-2"])
            self.assertEqual(board.cards["card-2"]["idList"], "list-2")
            # And the proposal is finished, so the approval is spent.
            settled = live.service.approvals.get(proposal["proposal_id"])
            self.assertEqual(settled.status.value, "verified")

    def test_an_approval_cannot_be_spent_twice(self) -> None:
        from test_provider_connection import principal_for

        with self.runtime() as live:
            operator = principal_for(live, Role.MAIN_OPERATOR)
            board = FakeTrello({"card-1": {"idList": "list-1"}, "card-2": {"idList": "list-1"}})
            proposal = self.propose(live, operator, board)
            approved = self.approve(live, operator, proposal)
            live.execute_bulk(operator, proposal["proposal_id"], self.spend(approved))
            with self.assertRaises((ApprovalError, PermissionError)):
                live.execute_bulk(operator, proposal["proposal_id"], self.spend(approved))

    def test_a_card_that_vanished_since_the_preview_invalidates_the_approval(self) -> None:
        """The approval is for a plan, and the plan describes a board.

        A card removed between the preview and the execution changes what the
        re-preview produces, so its hash no longer matches what was approved.
        Refusing is the point: the batch somebody agreed to is not the batch
        that would run.
        """

        from test_provider_connection import principal_for

        with self.runtime() as live:
            operator = principal_for(live, Role.MAIN_OPERATOR)
            board = FakeTrello({"card-1": {"idList": "list-1"}, "card-2": {"idList": "list-1"}})
            proposal = self.propose(live, operator, board)
            approved = self.approve(live, operator, proposal)
            del board.cards["card-2"]
            outcome = live.execute_bulk(operator, proposal["proposal_id"], self.spend(approved))
            # The approval is spent and recorded as failed rather than left
            # hanging, and nothing on the board moved.
            self.assertEqual(outcome["status"], "failed")
            self.assertEqual(board.cards["card-1"]["idList"], "list-1")


class ConflictMergeTests(unittest.TestCase):
    """A merge whose conflicts somebody actually resolved."""

    def cards(self):
        from assistant.scotty_business.property_cards import parse_card

        left = parse_card(
            {
                "card_id": "card-1",
                "fields": {
                    "address": {"value": "123 Main St", "source": "trello", "authority": 30},
                    "asking_price": {
                        "value": "100000",
                        "source": "trello",
                        "authority": 30,
                    },
                },
            }
        )
        right = parse_card(
            {
                "card_id": "card-2",
                "fields": {
                    "address": {"value": "123 Main St", "source": "trello", "authority": 30},
                    "asking_price": {
                        "value": "125000",
                        "source": "trello",
                        "authority": 30,
                    },
                },
            }
        )
        return left, right

    def test_a_conflict_can_be_resolved_and_the_choice_is_what_commits(self) -> None:
        from assistant.scotty_business.property_cards import merge_preview

        left, right = self.cards()
        preview = merge_preview(left, right)
        self.assertIn("asking_price", preview.unresolved)
        chosen = preview.choose("asking_price", "right")
        merged = chosen.commit()
        self.assertEqual(merged.fields["asking_price"].value, "125000")

    def test_an_unresolved_conflict_cannot_be_committed_by_accident(self) -> None:
        from assistant.scotty_business.property_cards import ConflictError, merge_preview

        left, right = self.cards()
        with self.assertRaises(ConflictError):
            merge_preview(left, right).commit()

    def test_the_merge_proposal_carries_the_resolved_fields(self) -> None:
        """What is approved is the resolution, not "merge these two cards".

        A proposal that only named the two cards would be approved once and
        then merged with whatever resolution was chosen afterwards, which is a
        different change from the one anybody looked at.
        """

        import synthetic

        from assistant.scotty_business.approvals import ApprovalStore
        from assistant.scotty_business.service import ScottyService

        trello = FakeTrello(
            {
                "card-1": {"name": "123 Main St", "normalized_address": "123 main st"},
                "card-2": {"name": "123 Main St", "normalized_address": "123 main st"},
            }
        )
        directory = tempfile.TemporaryDirectory(prefix="scotty-merge-")
        self.addCleanup(directory.cleanup)
        store = ApprovalStore(Path(directory.name) / "approvals.db")
        store.initialize()
        unused = object()
        service = ScottyService(
            synthetic.config(),
            store,
            trello=lambda _actor: trello,
            ghl=unused,
            rentcast=None,
            discord=unused,
        )
        proposal = service.propose_trello_merge(
            actor(), "card-1", "card-2", resolutions={"name": "source"}
        )
        self.assertEqual(proposal.payload.get("resolutions"), {"name": "source"})


if __name__ == "__main__":
    unittest.main()
