"""Runs that finish, and runs that do not get stuck behind other runs.

Two defects, both of which leave work parked forever.

A consequence step raises a proposal and the run waits. Somebody approved it,
the effect happened -- and nothing ever told the run. The step stayed
`awaiting_approval`, the run stayed `waiting_approval`, and the background pass
skipped it because waiting runs were not open runs. The work was done and the
workflow never knew.

And the pass that carries runs forward asked for the oldest fifty. Fifty runs
that cannot move are fifty runs it returns every time, so the fifty-first --
which could have run -- never gets looked at.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from assistant.scotty_business.policy import Principal, Role
from assistant.scotty_business.workflow_runs import (
    RunLedger,
    RunState,
    StepState,
)
from assistant.scotty_business.workflows import parse_workflow

MOMENT = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)


def definition(**overrides) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Seller follow up",
        "purpose": "open a card, then text the seller once somebody approves",
        "trigger": {"kind": "manual"},
        "steps": [
            {"operation": "reminder.create", "arguments": {"text": "call them"}},
            {"operation": "ghl.send_sms", "arguments": {"contact_id": "K-1", "body": "hello"}},
        ],
        "limits": {"cards_per_run": 5, "runs_per_day": 500, "recipients": 1},
        "approval_class": "consequence",
        "retries": {"attempts": 1, "circuit_breaker": 3, "stop_rule": "on_unknown"},
        "idempotency": {"key": "lead_id", "on_duplicate": "skip"},
        "examples": [{"input": {"lead_id": "L-1"}, "expect": "one card, one text"}],
    }
    body.update(overrides)
    return body


class LedgerFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="scotty-completion-")
        self.addCleanup(directory.cleanup)
        self.moment = MOMENT
        self.ledger = RunLedger(
            Path(directory.name) / "workflow-runs.db",
            owner_uid=None,
            clock=lambda: self.moment,
        )
        self.ledger.initialize()
        self.trent = Principal(guild_id="G", channel_id="C", user_id="U", role=Role.MAIN_OPERATOR)
        self.workflow = parse_workflow(definition(), owner=Role.MAIN_OPERATOR)

    def parked(self):
        """One run, carried to the consequence step and waiting on approval."""

        run = self.ledger.start(self.workflow, self.trent, {"lead_id": "L-1"})
        first = self.ledger.claim(run.run_id)
        assert first is not None
        self.ledger.record(run.run_id, first.index, StepState.DONE, detail="reminder set")
        second = self.ledger.claim(run.run_id)
        assert second is not None
        self.ledger.wait_for_approval(run.run_id, second.index, "P-1")
        return self.ledger.get(run.run_id, Role.MAIN_OPERATOR)


class ApprovalCompletionTests(LedgerFixture):
    def test_a_waiting_run_is_found_by_its_approval(self) -> None:
        run = self.parked()
        self.assertEqual(run.state, RunState.WAITING_APPROVAL)
        found = self.ledger.awaiting("P-1")
        assert found is not None
        self.assertEqual(found[0], run.run_id)
        self.assertEqual(found[1], 1)

    def test_an_approved_and_executed_step_completes_and_the_run_resumes(self) -> None:
        run = self.parked()
        resumed = self.ledger.settle_approval("P-1", StepState.DONE, detail="message sent")
        self.assertEqual(resumed, run.run_id)
        stored = self.ledger.get(run.run_id, Role.MAIN_OPERATOR)
        self.assertEqual(stored.steps[1].state, StepState.DONE)
        # Back to pending so the pass picks it up; not silently succeeded.
        self.assertEqual(stored.state, RunState.PENDING)

    def test_a_denied_approval_fails_the_exact_step_and_stops_the_run(self) -> None:
        run = self.parked()
        self.ledger.settle_approval("P-1", StepState.FAILED, detail="denied")
        stored = self.ledger.get(run.run_id, Role.MAIN_OPERATOR)
        self.assertEqual(stored.steps[1].state, StepState.FAILED)
        self.assertEqual(stored.state, RunState.FAILED)

    def test_an_unknown_outcome_stops_the_run_for_a_person(self) -> None:
        run = self.parked()
        self.ledger.settle_approval("P-1", StepState.UNKNOWN, detail="provider unreachable")
        stored = self.ledger.get(run.run_id, Role.MAIN_OPERATOR)
        self.assertEqual(stored.state, RunState.UNKNOWN)
        self.assertIn("reconcile", stored.reason)

    def test_a_different_approval_never_completes_a_waiting_step(self) -> None:
        run = self.parked()
        self.assertIsNone(self.ledger.settle_approval("P-2", StepState.DONE))
        stored = self.ledger.get(run.run_id, Role.MAIN_OPERATOR)
        self.assertEqual(stored.steps[1].state, StepState.AWAITING_APPROVAL)
        self.assertEqual(stored.state, RunState.WAITING_APPROVAL)

    def test_the_same_approval_settles_once(self) -> None:
        self.parked()
        self.assertIsNotNone(self.ledger.settle_approval("P-1", StepState.DONE))
        self.assertIsNone(self.ledger.settle_approval("P-1", StepState.DONE))

    def test_a_waiting_run_survives_a_restart_and_is_still_findable(self) -> None:
        run = self.parked()
        reopened = RunLedger(self.ledger.path, owner_uid=None, clock=lambda: self.moment)
        # Recovery must not turn a run waiting on a person into an unknown one.
        self.assertEqual(reopened.recover_interrupted(), 0)
        self.assertEqual(
            reopened.get(run.run_id, Role.MAIN_OPERATOR).state, RunState.WAITING_APPROVAL
        )
        found = reopened.awaiting("P-1")
        assert found is not None
        self.assertEqual(found[0], run.run_id)


class StarvationTests(LedgerFixture):
    def blocked(self, count: int) -> None:
        """Runs that will never move, more than one page of them."""

        for index in range(count):
            self.moment = MOMENT + timedelta(seconds=index)
            run = self.ledger.start(self.workflow, self.trent, {"lead_id": f"stuck-{index}"})
            claimed = self.ledger.claim(run.run_id)
            assert claimed is not None
            self.ledger.record(run.run_id, claimed.index, StepState.DONE)
            following = self.ledger.claim(run.run_id)
            assert following is not None
            self.ledger.wait_for_approval(run.run_id, following.index, f"P-stuck-{index}")

    def test_later_work_is_reached_past_a_page_of_blocked_runs(self) -> None:
        self.blocked(60)
        self.moment = MOMENT + timedelta(minutes=5)
        runnable = self.ledger.start(self.workflow, self.trent, {"lead_id": "runnable"})

        # Walk the whole ledger the way a supervision pass does, one page at a
        # time. The run that can move must come up, however many cannot.
        seen: list[str] = []
        cursor = ""
        for _ in range(20):
            page = self.ledger.open_runs(Role.MAIN_OPERATOR, after=cursor, limit=10)
            if not page:
                break
            seen.extend(item.run_id for item in page)
            cursor = page[-1].cursor
        self.assertIn(runnable.run_id, seen)

    def test_a_page_never_repeats_a_run_it_already_returned(self) -> None:
        for index in range(30):
            self.moment = MOMENT + timedelta(seconds=index)
            self.ledger.start(self.workflow, self.trent, {"lead_id": f"open-{index}"})
        first = self.ledger.open_runs(Role.MAIN_OPERATOR, limit=10)
        second = self.ledger.open_runs(Role.MAIN_OPERATOR, after=first[-1].cursor, limit=10)
        self.assertEqual(len(first), 10)
        self.assertEqual(len(second), 10)
        self.assertFalse({item.run_id for item in first} & {item.run_id for item in second})

    def test_waiting_runs_are_not_open_work_but_are_still_reachable(self) -> None:
        self.blocked(3)
        # Nothing to carry forward: they are all waiting on people.
        self.assertEqual(self.ledger.open_runs(Role.MAIN_OPERATOR), ())
        # But each is still findable by the approval it is waiting on.
        self.assertIsNotNone(self.ledger.awaiting("P-stuck-1"))


class TriggerSurfaceTests(unittest.TestCase):
    """Every advertised trigger kind is one this deployment can actually fire."""

    def test_no_trigger_kind_is_accepted_that_nothing_can_deliver(self) -> None:
        from assistant.scotty_business.workflow_runs import DELIVERABLE_TRIGGERS
        from assistant.scotty_business.workflows import _TRIGGER_KINDS

        # A definition that names a trigger nobody delivers is a workflow that
        # silently never runs. The schema and the deliverers are the same set.
        self.assertEqual(set(_TRIGGER_KINDS), set(DELIVERABLE_TRIGGERS))

    def test_an_undeliverable_trigger_is_refused_at_definition_time(self) -> None:
        from assistant.scotty_business.workflows import WorkflowError

        with self.assertRaises(WorkflowError):
            parse_workflow(definition(trigger={"kind": "card_moved"}), owner=Role.MAIN_OPERATOR)


class RuntimeCompletionTests(unittest.TestCase):
    """The whole path, through the tool surface a person actually uses."""

    def runtime(self):
        from test_provider_connection import runtime

        return runtime(DISCORD_BOT_TOKEN="synthetic-discord")

    def test_settling_an_approval_carries_the_run_forward(self) -> None:
        from test_provider_connection import principal_for

        with self.runtime() as runtime:
            operator = principal_for(runtime, Role.MAIN_OPERATOR)
            workflow = parse_workflow(definition(), owner=Role.MAIN_OPERATOR)
            saved = runtime.workflows.save(workflow)
            run = runtime.workflow_runs.start(saved, operator, {"lead_id": "L-9"})
            claimed = runtime.workflow_runs.claim(run.run_id)
            assert claimed is not None
            runtime.workflow_runs.record(run.run_id, claimed.index, StepState.DONE)
            following = runtime.workflow_runs.claim(run.run_id)
            assert following is not None
            runtime.workflow_runs.wait_for_approval(run.run_id, following.index, "P-runtime")

            self.assertEqual(
                runtime.workflow_runs.get(run.run_id, Role.MAIN_OPERATOR).state,
                RunState.WAITING_APPROVAL,
            )
            # The runtime's own settle path, the one the approval surface calls.
            self.assertEqual(
                runtime.settle_workflow_approval("P-runtime", StepState.DONE), run.run_id
            )
            stored = runtime.workflow_runs.get(run.run_id, Role.MAIN_OPERATOR)
            self.assertEqual(stored.steps[1].state, StepState.DONE)
            self.assertEqual(stored.state, RunState.PENDING)

    def test_an_approval_that_belongs_to_no_run_is_simply_not_one(self) -> None:
        with self.runtime() as runtime:
            self.assertIsNone(runtime.settle_workflow_approval("P-nobody", StepState.DONE))


if __name__ == "__main__":
    unittest.main()
