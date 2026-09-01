"""Budgets, rates, quiet hours, circuit breakers, and retention.

Reliability is not only about failing safely; it is about not doing too much.
Every limit here is configured rather than implicit, counted against the exact
actor who asked, and denied with a reason the assistant can explain.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import synthetic

from assistant.scotty_business.budgets import (
    DEFAULT_BUDGETS,
    BudgetError,
    BudgetLedger,
    BudgetPolicy,
    Decision,
)
from assistant.scotty_business.policy import Role


def moment(hour: int, day: int = 1) -> datetime:
    return datetime(2026, 9, day, hour, 0, tzinfo=UTC)


class PolicyTests(unittest.TestCase):
    def test_the_defaults_are_bounded_and_complete(self) -> None:
        policy = BudgetPolicy.from_mapping({})
        for action in DEFAULT_BUDGETS:
            self.assertGreater(policy.limit(action).per_day, 0)
            self.assertGreater(policy.limit(action).per_hour, 0)
            self.assertLessEqual(policy.limit(action).per_hour, policy.limit(action).per_day)

    def test_configuration_narrows_a_limit_but_never_removes_it(self) -> None:
        policy = BudgetPolicy.from_mapping({"external_send": {"per_day": 5, "per_hour": 2}})
        self.assertEqual(policy.limit("external_send").per_day, 5)
        for bad in (
            {"external_send": {"per_day": 0, "per_hour": 1}},
            {"external_send": {"per_day": 5, "per_hour": 9}},
            {"external_send": {"per_day": 100_000, "per_hour": 1}},
            {"unknown_action": {"per_day": 1, "per_hour": 1}},
            {"external_send": "unlimited"},
        ):
            with self.subTest(bad=bad), self.assertRaises(BudgetError):
                BudgetPolicy.from_mapping(bad)

    def test_quiet_hours_are_a_window_not_a_guess(self) -> None:
        policy = BudgetPolicy.from_mapping({"quiet_hours": [21, 8]})
        self.assertTrue(policy.in_quiet_hours(moment(22)))
        self.assertTrue(policy.in_quiet_hours(moment(3)))
        self.assertFalse(policy.in_quiet_hours(moment(12)))
        self.assertFalse(BudgetPolicy.from_mapping({}).in_quiet_hours(moment(3)))


class LedgerTests(unittest.TestCase):
    def ledger(self, policy=None) -> BudgetLedger:
        directory = tempfile.TemporaryDirectory(prefix="scotty-budgets-")
        self.addCleanup(directory.cleanup)
        ledger = BudgetLedger(
            Path(directory.name) / "budgets.db",
            policy or BudgetPolicy.from_mapping({"external_send": {"per_day": 3, "per_hour": 2}}),
        )
        ledger.initialize()
        return ledger

    def actor(self, role=Role.MAIN_OPERATOR):
        return synthetic.config().principal_for(role)

    def test_spending_inside_the_budget_is_allowed_and_counted(self) -> None:
        ledger = self.ledger()
        first = ledger.spend(self.actor(), "external_send", at=moment(10))
        self.assertIs(first.allowed, True)
        self.assertEqual(first.remaining_today, 2)
        self.assertEqual(
            ledger.spend(self.actor(), "external_send", at=moment(10)).remaining_today, 1
        )

    def test_the_hourly_limit_denies_before_the_daily_one(self) -> None:
        ledger = self.ledger()
        ledger.spend(self.actor(), "external_send", at=moment(10))
        ledger.spend(self.actor(), "external_send", at=moment(10))
        denied = ledger.spend(self.actor(), "external_send", at=moment(10))
        self.assertFalse(denied.allowed)
        self.assertIn("hour", denied.reason)
        # The next hour is a fresh hourly budget, still inside the day.
        self.assertTrue(ledger.spend(self.actor(), "external_send", at=moment(11)).allowed)

    def test_one_users_spending_never_consumes_the_others_budget(self) -> None:
        ledger = self.ledger()
        for hour in (9, 10, 11):
            self.assertTrue(ledger.spend(self.actor(), "external_send", at=moment(hour)).allowed)
        self.assertFalse(ledger.spend(self.actor(), "external_send", at=moment(12)).allowed)
        other = ledger.spend(self.actor(Role.EMPLOYEE), "external_send", at=moment(12))
        self.assertTrue(other.allowed)

    def test_a_new_day_restores_the_budget(self) -> None:
        ledger = self.ledger()
        for hour in (9, 10, 11):
            ledger.spend(self.actor(), "external_send", at=moment(hour))
        self.assertFalse(ledger.spend(self.actor(), "external_send", at=moment(13)).allowed)
        self.assertTrue(ledger.spend(self.actor(), "external_send", at=moment(9, day=2)).allowed)

    def test_a_quiet_hours_send_is_deferred_not_silently_dropped(self) -> None:
        ledger = self.ledger(
            BudgetPolicy.from_mapping(
                {"quiet_hours": [21, 8], "external_send": {"per_day": 5, "per_hour": 5}}
            )
        )
        decision = ledger.spend(self.actor(), "external_send", at=moment(23))
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.deferred)
        self.assertIn("quiet hours", decision.reason)
        # Nothing was counted against a budget the caller never got to use.
        self.assertTrue(ledger.spend(self.actor(), "external_send", at=moment(9, day=2)).allowed)

    def test_an_urgent_action_still_reaches_the_maintainer_in_quiet_hours(self) -> None:
        ledger = self.ledger(BudgetPolicy.from_mapping({"quiet_hours": [21, 8]}))
        decision = ledger.spend(self.actor(), "incident_alert", at=moment(23))
        self.assertTrue(decision.allowed)


class CircuitBreakerTests(unittest.TestCase):
    def ledger(self) -> BudgetLedger:
        directory = tempfile.TemporaryDirectory(prefix="scotty-breaker-")
        self.addCleanup(directory.cleanup)
        ledger = BudgetLedger(Path(directory.name) / "budgets.db", BudgetPolicy.from_mapping({}))
        ledger.initialize()
        return ledger

    def test_repeated_provider_failures_open_the_breaker(self) -> None:
        ledger = self.ledger()
        for _ in range(BudgetPolicy.from_mapping({}).breaker_threshold):
            ledger.record_failure("trello", at=moment(10))
        state = ledger.breaker("trello", at=moment(10))
        self.assertTrue(state.open)
        self.assertIn("trello", state.reason)

    def test_a_success_closes_the_breaker_again(self) -> None:
        ledger = self.ledger()
        for _ in range(BudgetPolicy.from_mapping({}).breaker_threshold):
            ledger.record_failure("trello", at=moment(10))
        ledger.record_success("trello", at=moment(10))
        self.assertFalse(ledger.breaker("trello", at=moment(10)).open)

    def test_the_breaker_reopens_the_provider_after_its_cooldown(self) -> None:
        policy = BudgetPolicy.from_mapping({})
        ledger = self.ledger()
        for _ in range(policy.breaker_threshold):
            ledger.record_failure("trello", at=moment(10))
        self.assertTrue(ledger.breaker("trello", at=moment(10)).open)
        self.assertFalse(ledger.breaker("trello", at=moment(12)).open)

    def test_one_provider_failing_never_opens_another(self) -> None:
        ledger = self.ledger()
        for _ in range(BudgetPolicy.from_mapping({}).breaker_threshold):
            ledger.record_failure("trello", at=moment(10))
        self.assertFalse(ledger.breaker("google_workspace", at=moment(10)).open)


class RetentionTests(unittest.TestCase):
    def test_spending_records_older_than_the_retention_window_are_pruned(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="scotty-retention-")
        self.addCleanup(directory.cleanup)
        ledger = BudgetLedger(
            Path(directory.name) / "budgets.db",
            BudgetPolicy.from_mapping({"retention_days": 7}),
        )
        ledger.initialize()
        actor = synthetic.config().principal_for(Role.MAIN_OPERATOR)
        ledger.spend(actor, "external_send", at=moment(10, day=1))
        removed = ledger.prune(at=datetime(2026, 10, 1, tzinfo=UTC))
        self.assertEqual(removed, 1)
        self.assertEqual(ledger.prune(at=datetime(2026, 10, 1, tzinfo=UTC)), 0)


class DecisionTests(unittest.TestCase):
    def test_a_decision_explains_itself_without_naming_anything_private(self) -> None:
        decision = Decision(
            allowed=False, reason="the hourly limit for external_send is spent", deferred=False
        )
        rendered = str(decision.as_json())
        self.assertIn("hourly", rendered)
        for private in (synthetic.ROUTE_CHANNEL, synthetic.ROUTE_USER, synthetic.OPERATOR_USER):
            self.assertNotIn(private, rendered)


if __name__ == "__main__":
    unittest.main()
