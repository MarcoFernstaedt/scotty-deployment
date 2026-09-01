"""Workflows that actually run, and a record of what they did.

A workflow you can save but never run is a document. This is the part that
executes one: a durable ledger of runs and steps, so that a container that dies
mid-run comes back knowing exactly which step was in flight and refuses to
guess what happened to it.

The rules that matter are about not doing something twice. One trigger produces
one run. A step that succeeded is never replayed. A step whose outcome is
unknown stops the run and waits for a person, because retrying an effect you
cannot see is how one message becomes two.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from assistant.scotty_business.policy import Principal, Role
from assistant.scotty_business.workflow_runs import (
    RunError,
    RunLedger,
    Runner,
    RunState,
    StepOutcome,
    StepState,
    due_trigger,
)
from assistant.scotty_business.workflows import WorkflowError, parse_workflow

MOMENT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def definition(*, steps: list[dict[str, object]], approval: str = "routine") -> dict[str, object]:
    return {
        "name": "New lead intake",
        "purpose": "open a card and remind me when a lead arrives",
        "trigger": {"kind": "manual"},
        "steps": steps,
        "limits": {"cards_per_run": 5, "runs_per_day": 20, "recipients": 0},
        "approval_class": approval,
        "retries": {"attempts": 2, "circuit_breaker": 3, "stop_rule": "on_unknown"},
        "idempotency": {"key": "lead_id", "on_duplicate": "skip"},
        "examples": [{"input": {"lead_id": "L-1"}, "expect": "one card, one reminder"}],
    }


class LedgerFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="scotty-runs-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "workflow-runs.db"
        self.moment = MOMENT
        self.ledger = RunLedger(self.path, owner_uid=None, clock=lambda: self.moment)
        self.ledger.initialize()
        self.trent = Principal(
            guild_id="G", channel_id="C", user_id="U-trent", role=Role.MAIN_OPERATOR
        )
        self.mikey = Principal(guild_id="G", channel_id="C2", user_id="U-mikey", role=Role.EMPLOYEE)

    def workflow(self, **kwargs):
        return parse_workflow(
            definition(
                steps=kwargs.pop(
                    "steps",
                    [
                        {"operation": "property_card.create", "arguments": {"list": "leads"}},
                        {"operation": "reminder.create", "arguments": {"text": "call them"}},
                    ],
                ),
                **kwargs,
            ),
            owner=Role.MAIN_OPERATOR,
        )

    def start(self, workflow=None, trigger=None):
        # One workflow per fixture unless a test brings its own: two calls to
        # parse_workflow are two different workflows, and idempotency is per
        # workflow by design.
        if workflow is None:
            workflow = getattr(self, "_workflow", None) or self.workflow()
            self._workflow = workflow
        return self.ledger.start(workflow, self.trent, trigger or {"lead_id": "L-1"})


class StartTests(LedgerFixture):
    def test_a_run_records_every_step_before_it_does_any_of_them(self) -> None:
        run = self.start()
        self.assertEqual(run.state, RunState.PENDING)
        self.assertEqual(len(run.steps), 2)
        self.assertTrue(all(step.state is StepState.PENDING for step in run.steps))
        # Reopening the ledger reads the same run: it is on disk, not in memory.
        reopened = RunLedger(self.path, owner_uid=None, clock=lambda: self.moment)
        self.assertEqual(reopened.get(run.run_id, Role.MAIN_OPERATOR).run_id, run.run_id)

    def test_one_trigger_makes_one_run_however_many_times_it_arrives(self) -> None:
        first = self.start()
        second = self.start()
        # The same lead arriving twice is the same work, not twice the work.
        self.assertEqual(second.run_id, first.run_id)
        self.assertEqual(len(self.ledger.list(Role.MAIN_OPERATOR)), 1)

    def test_a_different_trigger_is_a_different_run(self) -> None:
        first = self.start()
        second = self.start(trigger={"lead_id": "L-2"})
        self.assertNotEqual(second.run_id, first.run_id)

    def test_a_trigger_missing_its_idempotency_field_is_refused(self) -> None:
        # Without the field that makes a run unique there is no way to tell a
        # repeat from a new one, so starting at all would risk doing it twice.
        with self.assertRaises(RunError):
            self.start(trigger={"name": "someone"})

    def test_each_step_payload_is_frozen_and_hash_bound_at_the_start(self) -> None:
        run = self.start()
        hashes = [step.payload_hash for step in run.steps]
        self.assertEqual(len(set(hashes)), 2)
        for step in run.steps:
            self.assertEqual(len(step.payload_hash), 64)

    def test_a_run_carries_a_deadline_and_the_version_it_started_from(self) -> None:
        workflow = self.workflow()
        run = self.start(workflow)
        self.assertEqual(run.workflow_version, workflow.version)
        self.assertGreater(run.deadline_at, self.moment)

    def test_another_user_cannot_see_or_touch_this_run(self) -> None:
        run = self.start()
        with self.assertRaises(RunError):
            self.ledger.get(run.run_id, Role.EMPLOYEE)
        self.assertEqual(self.ledger.list(Role.EMPLOYEE), ())


class ProgressTests(LedgerFixture):
    def test_a_claimed_step_is_never_claimed_again_by_anyone(self) -> None:
        run = self.start()
        first = self.ledger.claim(run.run_id)
        assert first is not None
        self.assertEqual(first.index, 0)
        self.assertEqual(first.state, StepState.RUNNING)
        # A second claim moves on rather than handing out the same step twice.
        self.assertIsNone(self.ledger.claim(run.run_id))

    def test_a_finished_step_is_not_replayed_when_the_run_continues(self) -> None:
        run = self.start()
        claimed = self.ledger.claim(run.run_id)
        assert claimed is not None
        self.ledger.record(run.run_id, claimed.index, StepState.DONE, detail="card BX")
        following = self.ledger.claim(run.run_id)
        assert following is not None
        self.assertEqual(following.index, 1)
        stored = self.ledger.get(run.run_id, Role.MAIN_OPERATOR)
        self.assertEqual(stored.steps[0].state, StepState.DONE)
        self.assertEqual(stored.steps[0].detail, "card BX")

    def test_attempts_are_counted_and_bounded_by_the_workflow(self) -> None:
        run = self.start()
        for _ in range(2):
            claimed = self.ledger.claim(run.run_id)
            assert claimed is not None
            self.ledger.record(run.run_id, claimed.index, StepState.PENDING, detail="provider 500")
        stored = self.ledger.get(run.run_id, Role.MAIN_OPERATOR)
        self.assertEqual(stored.steps[0].attempts, 2)

    def test_an_interrupted_step_comes_back_unknown_and_is_never_retried(self) -> None:
        run = self.start()
        self.ledger.claim(run.run_id)
        # The container died here. Nothing knows whether the card was created.
        recovered = RunLedger(self.path, owner_uid=None, clock=lambda: self.moment)
        self.assertEqual(recovered.recover_interrupted(), 1)
        stored = recovered.get(run.run_id, Role.MAIN_OPERATOR)
        self.assertEqual(stored.steps[0].state, StepState.UNKNOWN)
        self.assertEqual(stored.state, RunState.UNKNOWN)
        self.assertIsNone(recovered.claim(run.run_id))


class ControlTests(LedgerFixture):
    def test_a_run_can_be_paused_and_resumed_by_its_owner(self) -> None:
        run = self.start()
        self.ledger.pause(run.run_id, Role.MAIN_OPERATOR)
        self.assertEqual(self.ledger.get(run.run_id, Role.MAIN_OPERATOR).state, RunState.PAUSED)
        self.assertIsNone(self.ledger.claim(run.run_id))
        self.ledger.resume(run.run_id, Role.MAIN_OPERATOR)
        self.assertIsNotNone(self.ledger.claim(run.run_id))

    def test_a_cancelled_run_stays_cancelled(self) -> None:
        run = self.start()
        self.ledger.cancel(run.run_id, Role.MAIN_OPERATOR, "not needed after all")
        self.assertEqual(self.ledger.get(run.run_id, Role.MAIN_OPERATOR).state, RunState.CANCELLED)
        self.assertIsNone(self.ledger.claim(run.run_id))
        with self.assertRaises(RunError):
            self.ledger.resume(run.run_id, Role.MAIN_OPERATOR)

    def test_only_the_owner_may_pause_or_cancel(self) -> None:
        run = self.start()
        for action in (self.ledger.pause, self.ledger.resume):
            with self.assertRaises(RunError):
                action(run.run_id, Role.EMPLOYEE)
        with self.assertRaises(RunError):
            self.ledger.cancel(run.run_id, Role.EMPLOYEE, "not mine")

    def test_a_run_past_its_deadline_stops_instead_of_carrying_on(self) -> None:
        run = self.start()
        self.moment = MOMENT + timedelta(days=2)
        self.assertIsNone(self.ledger.claim(run.run_id))
        self.assertEqual(self.ledger.get(run.run_id, Role.MAIN_OPERATOR).state, RunState.FAILED)
        self.assertIn("deadline", self.ledger.get(run.run_id, Role.MAIN_OPERATOR).reason)

    def test_the_daily_run_limit_is_the_workflow_s_own(self) -> None:
        workflow = parse_workflow(
            {
                **definition(
                    steps=[{"operation": "reminder.create", "arguments": {"text": "hello"}}]
                ),
                "limits": {"cards_per_run": 1, "runs_per_day": 2, "recipients": 0},
            },
            owner=Role.MAIN_OPERATOR,
        )
        for index in range(2):
            self.ledger.start(workflow, self.trent, {"lead_id": f"L-{index}"})
        with self.assertRaises(RunError) as caught:
            self.ledger.start(workflow, self.trent, {"lead_id": "L-3"})
        self.assertIn("today", str(caught.exception))

    def test_finishing_records_a_terminal_state_and_a_reason(self) -> None:
        run = self.start()
        self.ledger.finish(run.run_id, RunState.SUCCEEDED, "both steps done")
        stored = self.ledger.get(run.run_id, Role.MAIN_OPERATOR)
        self.assertEqual(stored.state, RunState.SUCCEEDED)
        self.assertEqual(stored.reason, "both steps done")
        self.assertIsNone(self.ledger.claim(run.run_id))


class PrivacyTests(LedgerFixture):
    def test_a_run_summary_carries_no_argument_values(self) -> None:
        run = self.ledger.start(
            self.workflow(),
            self.trent,
            {"lead_id": "L-1", "phone": "+15555550123"},
        )
        rendered = str(run.preview())
        self.assertNotIn("+15555550123", rendered)
        self.assertIn("property_card.create", rendered)


class RunnerFixture(LedgerFixture):
    """A runner over a dispatch that records what it was asked to do."""

    def setUp(self) -> None:
        super().setUp()
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.outcomes: dict[str, StepOutcome] = {}
        self.runner = Runner(self.ledger, self.dispatch)

    def dispatch(self, actor, operation, arguments) -> StepOutcome:
        self.assertIs(actor, self.trent)
        self.calls.append((operation, dict(arguments)))
        return self.outcomes.get(operation, StepOutcome(StepState.DONE, "done"))

    def advance(self, workflow=None):
        workflow = workflow or (getattr(self, "_workflow", None) or self.workflow())
        self._workflow = workflow
        run = self.start(workflow)
        return self.runner.advance(run.run_id, workflow, self.trent)


class RunnerTests(RunnerFixture):
    def test_a_run_executes_its_steps_in_order_and_succeeds(self) -> None:
        run = self.advance()
        self.assertEqual(
            [operation for operation, _ in self.calls],
            ["property_card.create", "reminder.create"],
        )
        self.assertEqual(run.state, RunState.SUCCEEDED)
        self.assertTrue(all(step.state is StepState.DONE for step in run.steps))

    def test_each_step_is_given_exactly_the_arguments_its_author_fixed(self) -> None:
        self.advance()
        self.assertEqual(self.calls[0][1], {"list": "leads"})
        self.assertEqual(self.calls[1][1], {"text": "call them"})

    def test_advancing_a_finished_run_does_nothing_a_second_time(self) -> None:
        workflow = self.workflow()
        run = self.advance(workflow)
        before = len(self.calls)
        again = self.runner.advance(run.run_id, workflow, self.trent)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(again.state, RunState.SUCCEEDED)

    def test_a_failing_step_is_retried_up_to_the_declared_attempts(self) -> None:
        self.outcomes["property_card.create"] = StepOutcome(StepState.FAILED, "Trello said 500")
        run = self.advance()
        # attempts: 2 in the definition, so three tries in total, and then the
        # run stops rather than working on a card that was never created.
        self.assertEqual(len([call for call in self.calls if call[0] == "property_card.create"]), 3)
        self.assertEqual(run.state, RunState.FAILED)
        self.assertEqual(run.steps[0].state, StepState.FAILED)
        self.assertEqual(run.steps[1].state, StepState.PENDING)

    def test_an_unknown_outcome_stops_the_run_and_is_never_retried(self) -> None:
        self.outcomes["property_card.create"] = StepOutcome(StepState.UNKNOWN, "no readback")
        run = self.advance()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(run.state, RunState.UNKNOWN)
        self.assertIn("reconcile", run.reason)

    def test_a_consequence_step_waits_for_an_approval_and_does_nothing_yet(self) -> None:
        workflow = self.workflow(
            steps=[
                {"operation": "property_card.create", "arguments": {"list": "leads"}},
                {"operation": "ghl.send_sms", "arguments": {"contact_id": "K-1", "body": "hi"}},
            ],
            approval="consequence",
        )
        self.outcomes["ghl.send_sms"] = StepOutcome(StepState.AWAITING_APPROVAL, "proposed", "P-1")
        run = self.advance(workflow)
        self.assertEqual(run.state, RunState.WAITING_APPROVAL)
        self.assertEqual(run.steps[1].state, StepState.AWAITING_APPROVAL)
        # The approval is bound to this exact step, so approving it cannot
        # authorize a different message later.
        self.assertEqual(run.steps[1].approval_id, "P-1")

    def test_a_paused_run_stops_where_it_is(self) -> None:
        workflow = self.workflow()
        run = self.start(workflow)
        self.ledger.pause(run.run_id, Role.MAIN_OPERATOR)
        stopped = self.runner.advance(run.run_id, workflow, self.trent)
        self.assertEqual(self.calls, [])
        self.assertEqual(stopped.state, RunState.PAUSED)

    def test_a_run_never_executes_a_step_for_the_wrong_user(self) -> None:
        workflow = self.workflow()
        run = self.start(workflow)
        with self.assertRaises(RunError):
            self.runner.advance(run.run_id, workflow, self.mikey)
        self.assertEqual(self.calls, [])

    def test_a_run_stops_at_its_deadline_mid_flight(self) -> None:
        workflow = self.workflow()
        run = self.start(workflow)

        def slow(actor, operation, arguments):
            self.calls.append((operation, dict(arguments)))
            self.moment = MOMENT + timedelta(days=3)
            return StepOutcome(StepState.DONE, "done")

        stopped = Runner(self.ledger, slow).advance(run.run_id, workflow, self.trent)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(stopped.state, RunState.FAILED)
        self.assertIn("deadline", stopped.reason)


class ScheduleTests(LedgerFixture):
    """A schedule that actually fires, at most once per window."""

    def scheduled(self, minutes: int = 60):
        body = definition(steps=[{"operation": "reminder.create", "arguments": {"text": "hi"}}])
        body["trigger"] = {"kind": "schedule", "every_minutes": minutes}
        body["idempotency"] = {"key": "window", "on_duplicate": "skip"}
        return parse_workflow(body, owner=Role.MAIN_OPERATOR)

    def test_a_schedule_must_say_how_often_and_key_on_the_window(self) -> None:
        body = definition(steps=[{"operation": "reminder.create", "arguments": {"text": "hi"}}])
        body["trigger"] = {"kind": "schedule"}
        with self.assertRaises(WorkflowError):
            parse_workflow(body, owner=Role.MAIN_OPERATOR)
        body["trigger"] = {"kind": "schedule", "every_minutes": 60}
        with self.assertRaises(WorkflowError):
            # Keyed on anything else, two firings of one window would be two
            # runs, which is the duplicate this exists to prevent.
            parse_workflow(body, owner=Role.MAIN_OPERATOR)

    def test_one_window_starts_exactly_one_run(self) -> None:
        workflow = self.scheduled()
        first = self.ledger.start(workflow, self.trent, due_trigger(workflow, self.moment))
        self.moment = MOMENT + timedelta(minutes=30)
        second = self.ledger.start(workflow, self.trent, due_trigger(workflow, self.moment))
        self.assertEqual(second.run_id, first.run_id)

    def test_the_next_window_starts_a_new_run(self) -> None:
        workflow = self.scheduled()
        first = self.ledger.start(workflow, self.trent, due_trigger(workflow, self.moment))
        self.moment = MOMENT + timedelta(minutes=61)
        second = self.ledger.start(workflow, self.trent, due_trigger(workflow, self.moment))
        self.assertNotEqual(second.run_id, first.run_id)

    def test_quiet_hours_are_not_a_window_at_all(self) -> None:
        body = definition(steps=[{"operation": "reminder.create", "arguments": {"text": "hi"}}])
        body["trigger"] = {"kind": "schedule", "every_minutes": 60}
        body["idempotency"] = {"key": "window", "on_duplicate": "skip"}
        body["schedule"] = {"quiet_hours": [21, 8]}
        workflow = parse_workflow(body, owner=Role.MAIN_OPERATOR)
        self.assertIsNone(due_trigger(workflow, datetime(2026, 3, 1, 23, 0, tzinfo=UTC)))
        self.assertIsNotNone(due_trigger(workflow, datetime(2026, 3, 1, 12, 0, tzinfo=UTC)))

    def test_a_manual_workflow_is_never_due_on_its_own(self) -> None:
        self.assertIsNone(due_trigger(self.workflow(), self.moment))


class OpenRunTests(LedgerFixture):
    def test_an_open_run_behind_newer_ones_is_still_carried_forward(self) -> None:
        body = definition(steps=[{"operation": "reminder.create", "arguments": {"text": "hi"}}])
        body["limits"] = {"cards_per_run": 1, "runs_per_day": 100, "recipients": 0}
        workflow = parse_workflow(body, owner=Role.MAIN_OPERATOR)
        stuck = self.ledger.start(workflow, self.trent, {"lead_id": "L-old"})
        for index in range(30):
            self.moment = MOMENT + timedelta(minutes=index + 1)
            run = self.ledger.start(workflow, self.trent, {"lead_id": f"L-{index}"})
            self.ledger.finish(run.run_id, RunState.SUCCEEDED, "done")
        # A recent-activity list would have lost it twenty-five runs ago.
        open_ids = [run.run_id for run in self.ledger.open_runs(Role.MAIN_OPERATOR)]
        self.assertEqual(open_ids, [stuck.run_id])

    def test_open_runs_are_this_user_s_own_and_never_a_finished_one(self) -> None:
        run = self.start()
        self.assertEqual(
            [item.run_id for item in self.ledger.open_runs(Role.MAIN_OPERATOR)], [run.run_id]
        )
        self.assertEqual(self.ledger.open_runs(Role.EMPLOYEE), ())
        self.ledger.finish(run.run_id, RunState.CANCELLED, "never mind")
        self.assertEqual(self.ledger.open_runs(Role.MAIN_OPERATOR), ())


if __name__ == "__main__":
    unittest.main()
