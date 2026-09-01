"""Every provider call is counted, and the counting is not optional.

The limits existed, were configurable, and were almost never applied. The
runtime built its policy from an empty mapping, so the configuration file was
never read; and the only paths that spent anything were chat messages and
incident alerts. Trello reads, card writes, Workspace writes, GHL sends,
announcements, administration, workflow steps and scheduled runs all went out
uncounted, which means a runaway loop had nothing standing in front of it.

So there is one place a provider call passes through, and it counts there.
Anything that reaches a provider goes through the same guard, gets the same
answer, and records the same outcome — including the workflow runner, which
used to be a way around it by construction.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from assistant.scotty_business.adapters.http import AmbiguousEffectError, ProviderError
from assistant.scotty_business.budgets import (
    DEFAULT_BUDGETS,
    BudgetError,
    BudgetLedger,
    BudgetPolicy,
)
from assistant.scotty_business.policy import Principal, Role

MOMENT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def actor(role: Role = Role.MAIN_OPERATOR) -> Principal:
    return Principal(guild_id="G", channel_id="C", user_id=f"U-{role.value}", role=role)


class ConfigurationTests(unittest.TestCase):
    def path(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="scotty-budgets-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name) / "budgets.json"

    def test_an_absent_configuration_uses_the_declared_defaults(self) -> None:
        policy = BudgetPolicy.load(self.path())
        self.assertEqual(policy.limit("external_send"), DEFAULT_BUDGETS["external_send"])

    def test_a_configuration_replaces_only_what_it_names(self) -> None:
        import json

        path = self.path()
        path.write_text(
            json.dumps({"external_send": {"per_hour": 2, "per_day": 5}}), encoding="utf-8"
        )
        policy = BudgetPolicy.load(path)
        self.assertEqual(policy.limit("external_send").per_hour, 2)
        self.assertEqual(policy.limit("provider_read"), DEFAULT_BUDGETS["provider_read"])

    def test_a_malformed_configuration_is_refused_rather_than_ignored(self) -> None:
        path = self.path()
        path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(BudgetError):
            BudgetPolicy.load(path)

    def test_a_configuration_the_runtime_could_write_is_refused(self) -> None:
        import json
        import os

        path = self.path()
        path.write_text(
            json.dumps({"external_send": {"per_hour": 9999, "per_day": 9999}}), encoding="utf-8"
        )
        os.chmod(path, 0o666)  # noqa: S103 - the unsafe mode is the point
        # A limit the model-facing account can raise is not a limit.
        with self.assertRaises(BudgetError):
            BudgetPolicy.load(path, owner_uid=os.geteuid())


class LedgerFixture(unittest.TestCase):
    def ledger(self, **overrides) -> BudgetLedger:
        directory = tempfile.TemporaryDirectory(prefix="scotty-budget-ledger-")
        self.addCleanup(directory.cleanup)
        policy = BudgetPolicy.from_mapping(overrides)
        ledger = BudgetLedger(Path(directory.name) / "budgets.db", policy)
        ledger.initialize()
        return ledger


class GuardTests(LedgerFixture):
    def test_a_provider_read_is_counted_and_eventually_refused(self) -> None:
        ledger = self.ledger(provider_read={"per_hour": 2, "per_day": 10})
        for _ in range(2):
            self.assertTrue(ledger.spend(actor(), "provider_read", at=MOMENT).allowed)
        refused = ledger.spend(actor(), "provider_read", at=MOMENT)
        self.assertFalse(refused.allowed)
        self.assertTrue(refused.reason)

    def test_one_user_s_budget_is_not_the_other_s(self) -> None:
        ledger = self.ledger(provider_read={"per_hour": 1, "per_day": 10})
        self.assertTrue(ledger.spend(actor(Role.MAIN_OPERATOR), "provider_read", at=MOMENT).allowed)
        self.assertTrue(ledger.spend(actor(Role.EMPLOYEE), "provider_read", at=MOMENT).allowed)
        self.assertFalse(
            ledger.spend(actor(Role.MAIN_OPERATOR), "provider_read", at=MOMENT).allowed
        )

    def test_a_refusal_costs_nothing_so_the_next_hour_is_whole(self) -> None:
        ledger = self.ledger(provider_read={"per_hour": 1, "per_day": 10})
        ledger.spend(actor(), "provider_read", at=MOMENT)
        ledger.spend(actor(), "provider_read", at=MOMENT)
        later = MOMENT + timedelta(hours=1)
        self.assertTrue(ledger.spend(actor(), "provider_read", at=later).allowed)

    def test_an_hour_boundary_is_a_boundary_not_a_window(self) -> None:
        ledger = self.ledger(provider_read={"per_hour": 1, "per_day": 10})
        self.assertTrue(ledger.spend(actor(), "provider_read", at=MOMENT).allowed)
        edge = MOMENT.replace(minute=59, second=59)
        self.assertFalse(ledger.spend(actor(), "provider_read", at=edge).allowed)
        self.assertTrue(
            ledger.spend(actor(), "provider_read", at=edge + timedelta(seconds=1)).allowed
        )


class BreakerTests(LedgerFixture):
    def test_repeated_failures_open_the_breaker_and_success_closes_it(self) -> None:
        ledger = self.ledger(breaker_threshold=2)
        self.assertFalse(ledger.breaker("trello", at=MOMENT).open)
        ledger.record_failure("trello", at=MOMENT)
        ledger.record_failure("trello", at=MOMENT)
        self.assertTrue(ledger.breaker("trello", at=MOMENT).open)
        ledger.record_success("trello", at=MOMENT)
        self.assertFalse(ledger.breaker("trello", at=MOMENT).open)

    def test_one_provider_s_breaker_is_not_another_s(self) -> None:
        ledger = self.ledger(breaker_threshold=1)
        ledger.record_failure("trello", at=MOMENT)
        self.assertTrue(ledger.breaker("trello", at=MOMENT).open)
        self.assertFalse(ledger.breaker("ghl", at=MOMENT).open)


class RuntimeChokepointTests(unittest.TestCase):
    """Everything that reaches a provider passes the same guard."""

    def runtime(self, **environment):
        from test_provider_connection import runtime

        return runtime(DISCORD_BOT_TOKEN="synthetic-discord", **environment)

    def connected(self):
        return self.runtime(
            SCOTTY_TRELLO_API_KEY="shared-key",
            SCOTTY_TRELLO_TOKEN_MAIN_OPERATOR="operator-token",  # noqa: S106 - synthetic
            SCOTTY_TRELLO_TOKEN_EMPLOYEE="employee-token",  # noqa: S106 - synthetic
        )

    def test_a_provider_read_spends_from_the_caller_s_budget(self) -> None:
        from test_provider_connection import principal_for

        with self.connected() as runtime:
            operator = principal_for(runtime, Role.MAIN_OPERATOR)
            runtime.budgets.policy = BudgetPolicy.from_mapping(
                {"provider_read": {"per_hour": 1, "per_day": 10}}
            )
            watched: list[str] = []
            runtime.broker_harness._broker.executor = _Watching(watched)

            runtime.handle_read(operator, {"operation": "trello_cards"})
            with self.assertRaises(PermissionError):
                runtime.handle_read(operator, {"operation": "trello_cards"})
            # The second call was refused before it reached the provider.
            self.assertEqual(len(watched), 1)

    def test_a_workspace_write_spends_the_write_budget_not_the_read_one(self) -> None:
        from assistant.scotty_business.runtime import _budget_action

        # The Workspace wrapper names its budget directly, having no broker
        # operation id to map. Turning that back into a read would have counted
        # every Workspace write against the wrong pool.
        self.assertEqual(_budget_action("workspace_write"), "workspace_write")
        self.assertEqual(_budget_action("ghl.send_sms"), "external_send")
        self.assertEqual(_budget_action("trello.list_board_cards"), "provider_read")

    def test_an_open_breaker_refuses_before_the_provider_is_touched(self) -> None:
        from test_provider_connection import principal_for

        with self.connected() as runtime:
            operator = principal_for(runtime, Role.MAIN_OPERATOR)
            runtime.budgets.policy = BudgetPolicy.from_mapping({"breaker_threshold": 1})
            runtime.budgets.record_failure("trello")
            watched: list[str] = []
            runtime.broker_harness._broker.executor = _Watching(watched)
            with self.assertRaises(PermissionError):
                runtime.handle_read(operator, {"operation": "trello_cards"})
            self.assertEqual(watched, [])

    def test_a_provider_failure_is_recorded_without_anybody_remembering_to(self) -> None:
        from test_provider_connection import principal_for

        with self.connected() as runtime:
            operator = principal_for(runtime, Role.MAIN_OPERATOR)
            runtime.broker_harness._broker.executor = _Failing()
            with self.assertRaises((ProviderError, AmbiguousEffectError)):
                runtime.handle_read(operator, {"operation": "trello_cards"})
            self.assertGreaterEqual(runtime.budgets.breaker("trello").failures, 1)

    def test_a_workflow_step_spends_the_same_budget_as_a_direct_call(self) -> None:
        from test_provider_connection import principal_for

        with self.connected() as runtime:
            operator = principal_for(runtime, Role.MAIN_OPERATOR)
            runtime.budgets.policy = BudgetPolicy.from_mapping(
                {"provider_read": {"per_hour": 1, "per_day": 10}}
            )
            watched: list[str] = []
            runtime.broker_harness._broker.executor = _Watching(watched)

            runtime.handle_read(operator, {"operation": "trello_cards"})
            # A workflow is a way to ask, not a way round the limit.
            outcome = runtime._run_workflow_step(operator, "trello.list_cards", {})
            self.assertEqual(outcome.state.value, "failed")
            self.assertEqual(len(watched), 1)


class _Watching:
    def __init__(self, seen: list[str]) -> None:
        self.seen = seen

    def run(self, operation_id, arguments, *, actor, timeout=10.0):
        del arguments, timeout
        self.seen.append(f"{operation_id}:{actor}")

        class Outcome:
            ok = True
            status = 200
            body: list[object] = []

            @staticmethod
            def as_reply():
                return {"ok": True, "status": 200, "body": [], "state": ""}

        return Outcome()


class _Failing:
    def run(self, operation_id, arguments, *, actor, timeout=10.0):
        from assistant.scotty_broker.executor import ExecutionError

        del operation_id, arguments, actor, timeout
        raise ExecutionError("provider outcome unknown")


if __name__ == "__main__":
    unittest.main()
