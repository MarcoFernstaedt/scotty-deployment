"""Deterministic supervision: one consumer, bounded restarts, one alert.

The model is not the thing that keeps this running. A supervisor watches the
parts that must hold — exactly one Discord consumer, a process that restarts
without looping, state that is where it should be — and tells Marco once per
incident rather than every time it looks.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from assistant.scotty_business.supervisor import (
    CRASH_LOOP_THRESHOLD,
    ConsumerLease,
    HealthState,
    IncidentLog,
    Supervisor,
)


def moment(minute: int = 0, hour: int = 10) -> datetime:
    return datetime(2026, 9, 1, hour, minute, tzinfo=UTC)


class ConsumerLeaseTests(unittest.TestCase):
    def lease(self) -> ConsumerLease:
        directory = tempfile.TemporaryDirectory(prefix="scotty-lease-")
        self.addCleanup(directory.cleanup)
        return ConsumerLease(Path(directory.name) / "consumer.lease")

    def test_one_process_holds_the_lease_and_a_second_is_refused(self) -> None:
        lease = self.lease()
        self.assertTrue(lease.claim("process-a", at=moment()))
        self.assertFalse(lease.claim("process-b", at=moment(1)))
        self.assertEqual(lease.holder(), "process-a")

    def test_the_holder_renews_its_own_lease_without_contention(self) -> None:
        lease = self.lease()
        lease.claim("process-a", at=moment())
        self.assertTrue(lease.claim("process-a", at=moment(1)))

    def test_an_expired_lease_is_taken_over_rather_than_deadlocking(self) -> None:
        lease = self.lease()
        lease.claim("process-a", at=moment())
        self.assertTrue(lease.claim("process-b", at=moment() + timedelta(minutes=30)))
        self.assertEqual(lease.holder(), "process-b")

    def test_releasing_hands_it_back_only_to_its_own_holder(self) -> None:
        lease = self.lease()
        lease.claim("process-a", at=moment())
        self.assertFalse(lease.release("process-b"))
        self.assertTrue(lease.release("process-a"))
        self.assertTrue(lease.claim("process-b", at=moment(1)))

    def test_a_corrupt_lease_file_is_reclaimed_not_trusted(self) -> None:
        lease = self.lease()
        lease.path.write_text("not json", encoding="utf-8")
        self.assertTrue(lease.claim("process-a", at=moment()))


class CrashLoopTests(unittest.TestCase):
    def supervisor(self) -> Supervisor:
        directory = tempfile.TemporaryDirectory(prefix="scotty-supervisor-")
        self.addCleanup(directory.cleanup)
        return Supervisor(Path(directory.name))

    def test_restarts_inside_the_window_are_a_crash_loop(self) -> None:
        supervisor = self.supervisor()
        for index in range(CRASH_LOOP_THRESHOLD - 1):
            state = supervisor.record_restart(at=moment(index))
            self.assertFalse(state.crash_looping)
        state = supervisor.record_restart(at=moment(CRASH_LOOP_THRESHOLD - 1))
        self.assertTrue(state.crash_looping)
        self.assertIn("restart", state.reason)

    def test_restarts_spread_over_time_are_not_a_crash_loop(self) -> None:
        supervisor = self.supervisor()
        for index in range(CRASH_LOOP_THRESHOLD + 2):
            state = supervisor.record_restart(at=moment(hour=index))
        self.assertFalse(state.crash_looping)

    def test_a_crash_loop_stops_restarting_and_proposes_the_operator_step(self) -> None:
        supervisor = self.supervisor()
        for index in range(CRASH_LOOP_THRESHOLD):
            supervisor.record_restart(at=moment(index))
        decision = supervisor.should_restart(at=moment(CRASH_LOOP_THRESHOLD))
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.proposal)


class IncidentAlertTests(unittest.TestCase):
    def log(self) -> IncidentLog:
        directory = tempfile.TemporaryDirectory(prefix="scotty-incidents-")
        self.addCleanup(directory.cleanup)
        return IncidentLog(Path(directory.name) / "incidents.json")

    def test_one_alert_per_incident_however_often_it_is_seen(self) -> None:
        log = self.log()
        self.assertTrue(log.should_alert("trello_unavailable", at=moment()))
        for minute in range(1, 5):
            self.assertFalse(log.should_alert("trello_unavailable", at=moment(minute)))

    def test_recovery_alerts_once_and_re_arms_the_incident(self) -> None:
        log = self.log()
        log.should_alert("trello_unavailable", at=moment())
        self.assertTrue(log.should_alert_recovery("trello_unavailable", at=moment(5)))
        self.assertFalse(log.should_alert_recovery("trello_unavailable", at=moment(6)))
        # Once recovered, the same failure later is a new incident.
        self.assertTrue(log.should_alert("trello_unavailable", at=moment(7)))

    def test_a_recovery_nobody_had_an_incident_for_is_not_announced(self) -> None:
        log = self.log()
        self.assertFalse(log.should_alert_recovery("ghl_unavailable", at=moment()))

    def test_distinct_incidents_do_not_suppress_each_other(self) -> None:
        log = self.log()
        self.assertTrue(log.should_alert("trello_unavailable", at=moment()))
        self.assertTrue(log.should_alert("google_unavailable", at=moment()))

    def test_an_alert_names_no_credential_and_no_private_identifier(self) -> None:
        log = self.log()
        log.should_alert("trello_unavailable", at=moment())
        rendered = log.path.read_text(encoding="utf-8")
        for forbidden in ("token", "secret", "Authorization"):
            self.assertNotIn(forbidden, rendered)


class HealthStateTests(unittest.TestCase):
    def test_unknown_is_never_reported_as_healthy(self) -> None:
        self.assertNotEqual(HealthState.UNKNOWN, HealthState.HEALTHY)
        for state in HealthState:
            self.assertIsInstance(state.value, str)
        self.assertEqual(
            {state.value for state in HealthState},
            {"healthy", "degraded", "blocked", "unknown", "not configured"},
        )


if __name__ == "__main__":
    unittest.main()
