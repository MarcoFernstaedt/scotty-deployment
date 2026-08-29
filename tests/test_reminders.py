from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from assistant.scotty_business.adapters import AmbiguousEffectError
from assistant.scotty_business.policy import Principal, Role
from assistant.scotty_business.reminders import (
    ReminderError,
    ReminderStatus,
    ReminderStore,
    ReminderWorker,
)


class ReminderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="scotty-reminder-test-")
        self.path = os.path.join(self.tempdir.name, "reminders.db")
        self.now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
        self.operator = Principal("100", "201", "301", Role.MAIN_OPERATOR)
        self.employee = Principal("100", "202", "302", Role.EMPLOYEE)
        self.store = ReminderStore(self.path, clock=lambda: self.now)
        self.store.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_reminders_are_owner_only_and_scoped_to_requester_tuple(self) -> None:
        reminder = self.store.create(
            self.operator,
            "Review synthetic lead",
            self.now + timedelta(minutes=5),
        )
        self.assertEqual(reminder.channel_id, self.operator.channel_id)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(
            [item.reminder_id for item in self.store.list_for(self.operator)],
            [reminder.reminder_id],
        )
        self.assertEqual(self.store.list_for(self.employee), ())
        with self.assertRaises(ReminderError):
            self.store.cancel(self.employee, reminder.reminder_id)

    def test_due_claim_is_atomic_and_never_retries_interrupted_dispatch(self) -> None:
        reminder = self.store.create(
            self.operator, "Review synthetic lead", self.now + timedelta(minutes=1)
        )
        self.now += timedelta(minutes=2)
        claimed = self.store.claim_due(limit=10)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].status, ReminderStatus.DISPATCHING)
        self.assertEqual(self.store.claim_due(limit=10), ())

        restarted = ReminderStore(self.path, clock=lambda: self.now)
        restarted.initialize()
        recovered = restarted.get(reminder.reminder_id)
        self.assertEqual(recovered.status, ReminderStatus.UNKNOWN)
        self.assertEqual(restarted.claim_due(limit=10), ())

    def test_worker_delivers_only_to_bound_channel_and_records_receipt(self) -> None:
        reminder = self.store.create(
            self.operator, "Review synthetic lead", self.now + timedelta(minutes=1)
        )
        self.now += timedelta(minutes=2)
        calls: list[tuple[str, str]] = []

        def send(channel_id: str, text: str):
            calls.append((channel_id, text))
            return {"message_id": "message-1", "channel_id": channel_id}

        worker = ReminderWorker(self.store, send)
        self.assertEqual(worker.run_once(), 1)
        delivered = self.store.get(reminder.reminder_id)
        self.assertEqual(delivered.status, ReminderStatus.DELIVERED)
        self.assertEqual(calls, [(self.operator.channel_id, "Review synthetic lead")])
        self.assertEqual(delivered.receipt, {"message_id": "message-1", "channel_id": "201"})

    def test_ambiguous_delivery_is_unknown_and_not_retried(self) -> None:
        reminder = self.store.create(
            self.operator, "Review synthetic lead", self.now + timedelta(minutes=1)
        )
        self.now += timedelta(minutes=2)
        calls = 0

        def send(channel_id: str, text: str):
            nonlocal calls
            calls += 1
            raise AmbiguousEffectError("unknown")

        worker = ReminderWorker(self.store, send)
        self.assertEqual(worker.run_once(), 1)
        self.assertEqual(self.store.get(reminder.reminder_id).status, ReminderStatus.UNKNOWN)
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
