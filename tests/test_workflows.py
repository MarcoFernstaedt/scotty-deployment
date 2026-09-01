"""Workflows a client can build, from operations that already exist.

A workflow is a declaration, not code. It names installed operations, its own
limits, and its own approval class, and it is validated whole before it can be
activated. Nothing a user writes here can add an integration, a credential, a
tool, a destination, or any authority the deployment did not already grant.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import synthetic

from assistant.scotty_business.policy import Role
from assistant.scotty_business.workflows import (
    INSTALLED_OPERATIONS,
    WorkflowError,
    WorkflowState,
    WorkflowStore,
    parse_workflow,
)


def definition(**overrides) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "New lead intake",
        "purpose": "Create a property card when a new lead arrives, then remind me.",
        "trigger": {"kind": "manual"},
        "steps": [
            {"operation": "property_card.create", "arguments": {"list_id": "list-1"}},
            {"operation": "reminder.create", "arguments": {"text": "call the seller"}},
        ],
        "limits": {"cards_per_run": 5, "runs_per_day": 20, "recipients": 0},
        "approval_class": "routine",
        "schedule": {"timezone": "America/Los_Angeles", "quiet_hours": [21, 8]},
        "retries": {"attempts": 2, "circuit_breaker": 3, "stop_rule": "on_unknown"},
        "idempotency": {"key": "property_address", "on_duplicate": "skip"},
        "retention_days": 90,
        "client_wording": "I'll open a card and remind you to call.",
        "examples": [{"input": "new lead at 88 Maple Ave", "expect": "one card, one reminder"}],
    }
    body.update(overrides)
    return body


class DefinitionTests(unittest.TestCase):
    def test_a_complete_definition_parses_and_keeps_its_owner(self) -> None:
        workflow = parse_workflow(definition(), owner=Role.MAIN_OPERATOR)
        self.assertEqual(workflow.owner, Role.MAIN_OPERATOR)
        self.assertEqual(workflow.state, WorkflowState.DRAFT)
        self.assertEqual(len(workflow.steps), 2)

    def test_every_required_section_is_actually_required(self) -> None:
        for missing in (
            "name",
            "purpose",
            "trigger",
            "steps",
            "limits",
            "approval_class",
            "retries",
            "idempotency",
            "examples",
        ):
            body = definition()
            body.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(WorkflowError):
                parse_workflow(body, owner=Role.MAIN_OPERATOR)

    def test_a_step_naming_an_operation_that_is_not_installed_is_refused(self) -> None:
        for operation in (
            "shell.run",
            "http.request",
            "credential.read",
            "plugin.install",
            "property_card.delete_board",
            "",
        ):
            body = definition(steps=[{"operation": operation, "arguments": {}}])
            with self.subTest(operation=operation), self.assertRaises(WorkflowError):
                parse_workflow(body, owner=Role.MAIN_OPERATOR)

    def test_every_installed_operation_belongs_to_a_provider_already_configured(self) -> None:
        for operation in INSTALLED_OPERATIONS:
            self.assertIn(".", operation)
            family = operation.split(".", 1)[0]
            self.assertIn(
                family,
                {"property_card", "reminder", "discord", "google", "trello", "ghl", "rentcast"},
            )

    def test_a_workflow_cannot_grant_itself_a_new_destination_or_credential(self) -> None:
        for extra in (
            {"credentials": {"trello": "t"}},
            {"destinations": ["999000000000000009"]},
            {"tools": ["shell"]},
            {"network": ["https://example.invalid"]},
            {"mcp": ["server"]},
        ):
            with self.subTest(extra=extra), self.assertRaises(WorkflowError):
                parse_workflow(definition(**extra), owner=Role.MAIN_OPERATOR)

    def test_a_consequence_step_forces_the_consequence_approval_class(self) -> None:
        body = definition(
            steps=[{"operation": "property_card.archive", "arguments": {}}],
            approval_class="routine",
        )
        with self.assertRaises(WorkflowError):
            parse_workflow(body, owner=Role.MAIN_OPERATOR)
        workflow = parse_workflow(
            definition(
                steps=[{"operation": "property_card.archive", "arguments": {}}],
                approval_class="consequence",
            ),
            owner=Role.MAIN_OPERATOR,
        )
        self.assertEqual(workflow.approval_class, "consequence")

    def test_limits_must_be_bounded_numbers(self) -> None:
        for limits in (
            {"cards_per_run": 0, "runs_per_day": 20, "recipients": 0},
            {"cards_per_run": 5000, "runs_per_day": 20, "recipients": 0},
            {"cards_per_run": 5, "runs_per_day": -1, "recipients": 0},
            {"cards_per_run": 5, "runs_per_day": 20},
            "unbounded",
        ):
            with self.subTest(limits=limits), self.assertRaises(WorkflowError):
                parse_workflow(definition(limits=limits), owner=Role.MAIN_OPERATOR)

    def test_a_definition_that_is_too_large_or_too_deep_is_refused(self) -> None:
        with self.assertRaises(WorkflowError):
            parse_workflow(definition(purpose="x" * 5000), owner=Role.MAIN_OPERATOR)
        nested: dict[str, object] = {"kind": "manual"}
        for _ in range(20):
            nested = {"kind": "manual", "inner": nested}
        with self.assertRaises(WorkflowError):
            parse_workflow(definition(trigger=nested), owner=Role.MAIN_OPERATOR)


class LifecycleTests(unittest.TestCase):
    def store(self) -> WorkflowStore:
        directory = tempfile.TemporaryDirectory(prefix="scotty-workflows-")
        self.addCleanup(directory.cleanup)
        store = WorkflowStore(Path(directory.name) / "workflows.json")
        return store

    def test_a_workflow_moves_draft_to_active_to_paused_to_retired(self) -> None:
        store = self.store()
        saved = store.save(parse_workflow(definition(), owner=Role.MAIN_OPERATOR))
        self.assertEqual(saved.state, WorkflowState.DRAFT)
        self.assertEqual(
            store.transition(saved.workflow_id, Role.MAIN_OPERATOR, WorkflowState.ACTIVE).state,
            WorkflowState.ACTIVE,
        )
        self.assertEqual(
            store.transition(saved.workflow_id, Role.MAIN_OPERATOR, WorkflowState.PAUSED).state,
            WorkflowState.PAUSED,
        )
        self.assertEqual(
            store.transition(saved.workflow_id, Role.MAIN_OPERATOR, WorkflowState.RETIRED).state,
            WorkflowState.RETIRED,
        )

    def test_a_retired_workflow_never_comes_back(self) -> None:
        store = self.store()
        saved = store.save(parse_workflow(definition(), owner=Role.MAIN_OPERATOR))
        store.transition(saved.workflow_id, Role.MAIN_OPERATOR, WorkflowState.ACTIVE)
        store.transition(saved.workflow_id, Role.MAIN_OPERATOR, WorkflowState.RETIRED)
        with self.assertRaises(WorkflowError):
            store.transition(saved.workflow_id, Role.MAIN_OPERATOR, WorkflowState.ACTIVE)

    def test_one_user_can_never_read_or_change_the_others_workflow(self) -> None:
        store = self.store()
        saved = store.save(parse_workflow(definition(), owner=Role.MAIN_OPERATOR))
        self.assertEqual(store.list(Role.EMPLOYEE), ())
        self.assertEqual(len(store.list(Role.MAIN_OPERATOR)), 1)
        with self.assertRaises(WorkflowError):
            store.transition(saved.workflow_id, Role.EMPLOYEE, WorkflowState.PAUSED)
        with self.assertRaises(WorkflowError):
            store.get(saved.workflow_id, Role.EMPLOYEE)

    def test_a_revision_replaces_the_definition_and_returns_it_to_draft(self) -> None:
        store = self.store()
        saved = store.save(parse_workflow(definition(), owner=Role.MAIN_OPERATOR))
        store.transition(saved.workflow_id, Role.MAIN_OPERATOR, WorkflowState.ACTIVE)
        revised = store.revise(
            saved.workflow_id,
            Role.MAIN_OPERATOR,
            parse_workflow(definition(name="Renamed intake"), owner=Role.MAIN_OPERATOR),
        )
        self.assertEqual(revised.name, "Renamed intake")
        self.assertEqual(revised.state, WorkflowState.DRAFT)
        self.assertEqual(revised.version, 2)

    def test_stored_workflows_survive_a_reopen_and_ignore_a_tampered_file(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="scotty-workflows-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "workflows.json"
        WorkflowStore(path).save(parse_workflow(definition(), owner=Role.MAIN_OPERATOR))
        self.assertEqual(len(WorkflowStore(path).list(Role.MAIN_OPERATOR)), 1)
        path.write_text('{"workflows": [{"operation": "shell.run"}]}', encoding="utf-8")
        self.assertEqual(WorkflowStore(path).list(Role.MAIN_OPERATOR), ())


class PreviewTests(unittest.TestCase):
    def test_a_preview_explains_each_step_in_the_client_s_own_words(self) -> None:
        workflow = parse_workflow(definition(), owner=Role.MAIN_OPERATOR)
        preview = workflow.preview()
        self.assertEqual(preview["approval_class"], "routine")
        self.assertEqual(len(preview["steps"]), 2)
        self.assertTrue(all(step["explanation"] for step in preview["steps"]))
        self.assertIn("quiet_hours", preview["schedule"])

    def test_a_preview_names_no_credential_and_no_private_identifier(self) -> None:
        workflow = parse_workflow(definition(), owner=Role.MAIN_OPERATOR)
        rendered = str(workflow.preview())
        for forbidden in (synthetic.ROUTE_CHANNEL, synthetic.ROUTE_USER, "token", "api_key"):
            self.assertNotIn(forbidden, rendered)


class RuntimeWorkflowTests(unittest.TestCase):
    """The workflow surface a client reaches, private to each of them."""

    def runtime(self):
        from test_provider_connection import runtime

        return runtime(DISCORD_BOT_TOKEN="synthetic-discord")

    def actor(self, runtime, role=Role.MAIN_OPERATOR):
        return runtime.config.principal_for(role)

    def test_a_user_builds_previews_activates_and_retires_their_workflow(self) -> None:
        with self.runtime() as runtime:
            operator = self.actor(runtime)
            saved = runtime.handle_read(
                operator,
                {
                    "operation": "workflow",
                    "workflow_action": "save",
                    "definition": definition(),
                },
            )
            workflow_id = saved["workflow_id"]
            self.assertEqual(saved["state"], "draft")
            active = runtime.handle_read(
                operator,
                {
                    "operation": "workflow",
                    "workflow_action": "activate",
                    "workflow_id": workflow_id,
                },
            )
            self.assertEqual(active["state"], "active")
            retired = runtime.handle_read(
                operator,
                {
                    "operation": "workflow",
                    "workflow_action": "retire",
                    "workflow_id": workflow_id,
                },
            )
            self.assertEqual(retired["state"], "retired")

    def test_one_user_never_sees_or_touches_the_others_workflow(self) -> None:
        with self.runtime() as runtime:
            saved = runtime.handle_read(
                self.actor(runtime),
                {
                    "operation": "workflow",
                    "workflow_action": "save",
                    "definition": definition(),
                },
            )
            employee = self.actor(runtime, Role.EMPLOYEE)
            self.assertEqual(
                runtime.handle_read(employee, {"operation": "workflow"}),
                [],
            )
            with self.assertRaises(WorkflowError):
                runtime.handle_read(
                    employee,
                    {
                        "operation": "workflow",
                        "workflow_action": "get",
                        "workflow_id": saved["workflow_id"],
                    },
                )

    def test_a_definition_reaching_for_new_authority_is_refused_at_the_tool(self) -> None:
        with self.runtime() as runtime, self.assertRaises(WorkflowError):
            runtime.handle_read(
                self.actor(runtime),
                {
                    "operation": "workflow",
                    "workflow_action": "save",
                    "definition": definition(steps=[{"operation": "shell.run", "arguments": {}}]),
                },
            )


class RuntimeRunTests(RuntimeWorkflowTests):
    """A workflow that actually runs, through the runtime a client reaches."""

    def active(self, runtime, operator, **overrides):
        saved = runtime.handle_read(
            operator,
            {
                "operation": "workflow",
                "workflow_action": "save",
                "definition": definition(**overrides),
            },
        )
        runtime.handle_read(
            operator,
            {
                "operation": "workflow",
                "workflow_action": "activate",
                "workflow_id": saved["workflow_id"],
            },
        )
        return saved["workflow_id"]

    def start(self, runtime, operator, workflow_id, trigger=None):
        return runtime.handle_read(
            operator,
            {
                "operation": "workflow",
                "workflow_action": "run",
                "workflow_id": workflow_id,
                "trigger": trigger or {"property_address": "44 Maple St"},
            },
        )

    def test_running_a_workflow_carries_out_its_steps_and_records_each_one(self) -> None:
        with self.runtime() as runtime:
            operator = self.actor(runtime)
            workflow_id = self.active(
                runtime,
                operator,
                steps=[
                    {
                        "operation": "reminder.create",
                        "arguments": {
                            "text": "call the seller",
                            "due_at": "2027-01-01T09:00:00+00:00",
                        },
                    }
                ],
            )
            run = self.start(runtime, operator, workflow_id)
            self.assertEqual(run["state"], "succeeded")
            self.assertEqual([step["state"] for step in run["steps"]], ["done"])
            # The reminder is really there: a run that reported success without
            # doing anything would be the exact thing this replaces.
            reminders = runtime.handle_reminder(operator, {"action": "list"})
            self.assertEqual([item["text"] for item in reminders], ["call the seller"])

    def test_the_same_trigger_never_runs_the_work_twice(self) -> None:
        with self.runtime() as runtime:
            operator = self.actor(runtime)
            workflow_id = self.active(
                runtime,
                operator,
                steps=[
                    {
                        "operation": "reminder.create",
                        "arguments": {
                            "text": "call the seller",
                            "due_at": "2027-01-01T09:00:00+00:00",
                        },
                    }
                ],
            )
            first = self.start(runtime, operator, workflow_id)
            second = self.start(runtime, operator, workflow_id)
            self.assertEqual(second["run_id"], first["run_id"])
            self.assertEqual(len(runtime.handle_reminder(operator, {"action": "list"})), 1)

    def test_only_an_active_workflow_runs(self) -> None:
        with self.runtime() as runtime:
            operator = self.actor(runtime)
            saved = runtime.handle_read(
                operator,
                {
                    "operation": "workflow",
                    "workflow_action": "save",
                    "definition": definition(),
                },
            )
            with self.assertRaises(ValueError):
                self.start(runtime, operator, saved["workflow_id"])

    def test_one_user_can_never_run_or_read_the_other_s_run(self) -> None:
        with self.runtime() as runtime:
            operator = self.actor(runtime)
            employee = self.actor(runtime, Role.EMPLOYEE)
            workflow_id = self.active(
                runtime,
                operator,
                steps=[
                    {
                        "operation": "reminder.create",
                        "arguments": {
                            "text": "call the seller",
                            "due_at": "2027-01-01T09:00:00+00:00",
                        },
                    }
                ],
            )
            run = self.start(runtime, operator, workflow_id)
            with self.assertRaises((ValueError, WorkflowError)):
                runtime.handle_read(
                    employee,
                    {
                        "operation": "workflow",
                        "workflow_action": "run_status",
                        "run_id": run["run_id"],
                    },
                )
            self.assertEqual(
                runtime.handle_read(employee, {"operation": "workflow", "workflow_action": "runs"}),
                [],
            )

    def test_a_finished_run_cannot_be_cancelled_back_into_something_else(self) -> None:
        with self.runtime() as runtime:
            operator = self.actor(runtime)
            workflow_id = self.active(
                runtime,
                operator,
                steps=[
                    {
                        "operation": "reminder.create",
                        "arguments": {
                            "text": "call the seller",
                            "due_at": "2027-01-01T09:00:00+00:00",
                        },
                    }
                ],
            )
            run = self.start(runtime, operator, workflow_id)
            self.assertEqual(run["state"], "succeeded")
            with self.assertRaises(ValueError):
                runtime.handle_read(
                    operator,
                    {
                        "operation": "workflow",
                        "workflow_action": "cancel_run",
                        "run_id": run["run_id"],
                        "reason": "not needed",
                    },
                )
            # The record of what was done stands: a run is history, not a draft.
            after = runtime.handle_read(
                operator,
                {
                    "operation": "workflow",
                    "workflow_action": "run_status",
                    "run_id": run["run_id"],
                },
            )
            self.assertEqual(after["state"], "succeeded")

    def test_a_scheduled_workflow_fires_once_a_window_from_the_supervision_pass(self) -> None:
        with self.runtime() as runtime:
            operator = self.actor(runtime)
            self.active(
                runtime,
                operator,
                trigger={"kind": "schedule", "every_minutes": 60},
                idempotency={"key": "window", "on_duplicate": "skip"},
                steps=[
                    {
                        "operation": "reminder.create",
                        "arguments": {
                            "text": "check the board",
                            "due_at": "2027-01-01T09:00:00+00:00",
                        },
                    }
                ],
            )
            # Midday, explicitly: this workflow declares quiet hours, and a
            # pass that did not say when it was running would fire or not
            # depending on the time of day the suite happened to run.
            noon = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
            first = runtime.advance_workflow_runs(at=noon)
            self.assertEqual(first["started"], 1)
            # The loop runs every second. A schedule that fired every pass
            # would be one reminder a second, which is the bug the window
            # exists to prevent.
            again = runtime.advance_workflow_runs(at=noon)
            self.assertEqual(again["started"], 0)
            self.assertEqual(len(runtime.handle_reminder(operator, {"action": "list"})), 1)

    def test_a_workflow_that_is_not_active_never_fires_on_a_schedule(self) -> None:
        with self.runtime() as runtime:
            operator = self.actor(runtime)
            runtime.handle_read(
                operator,
                {
                    "operation": "workflow",
                    "workflow_action": "save",
                    "definition": definition(
                        trigger={"kind": "schedule", "every_minutes": 60},
                        idempotency={"key": "window", "on_duplicate": "skip"},
                    ),
                },
            )
            self.assertEqual(
                runtime.advance_workflow_runs(at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC))[
                    "started"
                ],
                0,
            )

    def test_a_step_the_owner_may_not_do_fails_the_run_rather_than_widening_it(self) -> None:
        with self.runtime() as runtime:
            operator = self.actor(runtime)
            # Trello is not connected in this fixture, so a card step cannot
            # happen. The run records that and stops; it does not find another
            # way to do it.
            workflow_id = self.active(
                runtime,
                operator,
                steps=[{"operation": "property_card.create", "arguments": {"card": {}}}],
                retries={"attempts": 0, "circuit_breaker": 3, "stop_rule": "on_failure"},
            )
            run = self.start(runtime, operator, workflow_id)
            self.assertEqual(run["state"], "failed")
            self.assertEqual(run["steps"][0]["state"], "failed")


if __name__ == "__main__":
    unittest.main()


class StoreIsolationTests(unittest.TestCase):
    def test_a_stored_entry_can_never_claim_the_maintainer_as_its_owner(self) -> None:
        import json

        with tempfile.TemporaryDirectory(prefix="scotty-workflows-") as directory:
            path = Path(directory) / "workflows.json"
            body = definition()
            body.update({"workflow_id": "w-1", "owner": "maintainer", "state": "active"})
            path.write_text(json.dumps({"workflows": [body]}), encoding="utf-8")
            store = WorkflowStore(path, owner_uid=None)
            # The entry is dropped on read rather than becoming a workflow the
            # maintainer route owns.
            for role in (Role.MAINTAINER, Role.MAIN_OPERATOR, Role.EMPLOYEE):
                self.assertEqual(store.list(role), ())
        with self.assertRaises(WorkflowError):
            parse_workflow(definition(), owner=Role.MAINTAINER)

    def test_saving_one_workflow_never_drops_an_entry_it_cannot_parse(self) -> None:
        """Including the other user's, if a later version tightens validation."""

        import json

        with tempfile.TemporaryDirectory(prefix="scotty-workflows-") as directory:
            path = Path(directory) / "workflows.json"
            store = WorkflowStore(path, owner_uid=None)
            store.save(parse_workflow(definition(), owner=Role.MAIN_OPERATOR))

            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["workflows"].append(
                {"workflow_id": "future-1", "owner": "employee", "name": "From a later version"}
            )
            path.write_text(json.dumps(stored), encoding="utf-8")

            # This version cannot parse the other entry, and must not lose it.
            store.save(parse_workflow(definition(name="Second"), owner=Role.MAIN_OPERATOR))
            after = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("future-1", {entry.get("workflow_id") for entry in after["workflows"]})
