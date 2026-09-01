"""Something outside the container has to notice when it dies.

The in-process supervisor cannot restart a process it is part of, and Compose
is deliberately set to `restart: "no"` because Docker's own policy has no idea
what a crash loop is, when restarting has stopped helping, or when the right
answer is to roll back instead. So the deployment has an external supervisor,
and these are the decisions it has to get right.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from assistant.scotty_supervisor.supervise import (
    MAX_RESTARTS,
    RESTART_WINDOW,
    Decision,
    Supervisor,
    SupervisorState,
)


class FakeContainer:
    """A container that behaves like Docker: it can die, and it can refuse."""

    def __init__(self, *, running: bool = True, starts_ok: bool = True) -> None:
        self.running = running
        self.starts_ok = starts_ok
        self.starts = 0
        self.stops = 0
        self.exists = True

    def is_running(self) -> bool:
        return self.running

    def present(self) -> bool:
        return self.exists

    def start(self) -> bool:
        self.starts += 1
        if not self.starts_ok:
            return False
        self.running = True
        return True

    def stop(self) -> bool:
        self.stops += 1
        self.running = False
        return True


class Harness(unittest.TestCase):
    def supervisor(self, container: FakeContainer, **kwargs) -> Supervisor:
        directory = tempfile.TemporaryDirectory(prefix="scotty-supervisor-")
        self.addCleanup(directory.cleanup)
        self.state_dir = Path(directory.name)
        self.alerts: list[tuple[str, str]] = []
        return Supervisor(
            container,
            self.state_dir,
            alert=lambda kind, text: self.alerts.append((kind, text)),
            **kwargs,
        )

    def moment(self, minute: int = 0) -> datetime:
        return datetime(2026, 9, 1, 12, minute, tzinfo=UTC)


class HealthyTests(Harness):
    def test_a_running_container_is_left_alone(self) -> None:
        container = FakeContainer(running=True)
        supervisor = self.supervisor(container)
        decision = supervisor.tick(at=self.moment())
        self.assertEqual(decision.action, "none")
        self.assertEqual(container.starts, 0)
        self.assertEqual(self.alerts, [])

    def test_a_dead_container_is_started_once(self) -> None:
        container = FakeContainer(running=False)
        supervisor = self.supervisor(container)
        decision = supervisor.tick(at=self.moment())
        self.assertEqual(decision.action, "started")
        self.assertEqual(container.starts, 1)
        self.assertTrue(container.running)

    def test_a_recovered_container_reports_recovery_exactly_once(self) -> None:
        container = FakeContainer(running=False)
        supervisor = self.supervisor(container)
        supervisor.tick(at=self.moment())
        supervisor.tick(at=self.moment(1))
        supervisor.tick(at=self.moment(2))
        recoveries = [kind for kind, _ in self.alerts if kind == "recovery"]
        self.assertEqual(len(recoveries), 1)


class CrashLoopTests(Harness):
    def restart_repeatedly(self, supervisor: Supervisor, container: FakeContainer, times: int):
        decisions = []
        for index in range(times):
            container.running = False
            decisions.append(supervisor.tick(at=self.moment(index)))
        return decisions

    def test_restarts_are_bounded_and_the_loop_is_named(self) -> None:
        container = FakeContainer(running=False)
        supervisor = self.supervisor(container)
        decisions = self.restart_repeatedly(supervisor, container, MAX_RESTARTS + 2)
        self.assertEqual(decisions[-1].action, "gave_up")
        self.assertLessEqual(container.starts, MAX_RESTARTS)
        self.assertTrue(any(kind == "incident" for kind, _ in self.alerts))

    def test_the_incident_is_reported_once_not_every_tick(self) -> None:
        container = FakeContainer(running=False)
        supervisor = self.supervisor(container)
        self.restart_repeatedly(supervisor, container, MAX_RESTARTS + 5)
        incidents = [kind for kind, _ in self.alerts if kind == "incident"]
        self.assertEqual(len(incidents), 1)

    def test_restarts_spread_beyond_the_window_are_not_a_loop(self) -> None:
        container = FakeContainer(running=False)
        supervisor = self.supervisor(container)
        for index in range(MAX_RESTARTS + 3):
            container.running = False
            at = self.moment() + timedelta(seconds=int(RESTART_WINDOW.total_seconds()) * index)
            decision = supervisor.tick(at=at)
        self.assertEqual(decision.action, "started")

    def test_backoff_holds_off_before_the_next_attempt(self) -> None:
        container = FakeContainer(running=False)
        supervisor = self.supervisor(container)
        first = supervisor.tick(at=self.moment())
        self.assertEqual(first.action, "started")
        container.running = False
        # Immediately afterwards, the supervisor waits rather than hammering.
        held = supervisor.tick(at=self.moment())
        self.assertEqual(held.action, "waiting")
        self.assertEqual(container.starts, 1)

    def test_a_container_that_will_not_start_is_not_retried_forever(self) -> None:
        container = FakeContainer(running=False, starts_ok=False)
        supervisor = self.supervisor(container)
        for index in range(MAX_RESTARTS + 3):
            supervisor.tick(at=self.moment(index))
        self.assertLessEqual(container.starts, MAX_RESTARTS)


class SafetyTests(Harness):
    def test_an_integrity_failure_is_never_restarted_into(self) -> None:
        container = FakeContainer(running=False)
        supervisor = self.supervisor(container, integrity=lambda: False)
        decision = supervisor.tick(at=self.moment())
        self.assertEqual(decision.action, "blocked")
        self.assertEqual(container.starts, 0)
        self.assertTrue(any(kind == "incident" for kind, _ in self.alerts))

    def test_an_absent_container_is_reported_not_conjured(self) -> None:
        container = FakeContainer(running=False)
        container.exists = False
        supervisor = self.supervisor(container)
        decision = supervisor.tick(at=self.moment())
        self.assertEqual(decision.action, "absent")
        self.assertEqual(container.starts, 0)

    def test_an_operator_stop_is_honoured_until_it_is_lifted(self) -> None:
        container = FakeContainer(running=False)
        supervisor = self.supervisor(container)
        supervisor.hold("operator stopped it")
        self.assertEqual(supervisor.tick(at=self.moment()).action, "held")
        self.assertEqual(container.starts, 0)
        supervisor.release()
        self.assertEqual(supervisor.tick(at=self.moment(1)).action, "started")

    def test_the_supervisor_never_starts_a_second_consumer(self) -> None:
        container = FakeContainer(running=True)
        supervisor = self.supervisor(container)
        for index in range(5):
            supervisor.tick(at=self.moment(index))
        self.assertEqual(container.starts, 0)


class StateTests(Harness):
    def test_state_survives_a_supervisor_restart(self) -> None:
        container = FakeContainer(running=False)
        supervisor = self.supervisor(container)
        for index in range(MAX_RESTARTS):
            container.running = False
            supervisor.tick(at=self.moment(index))

        # A fresh supervisor over the same state directory knows what happened.
        revived = Supervisor(container, self.state_dir, alert=lambda *_: None)
        container.running = False
        self.assertEqual(revived.tick(at=self.moment(MAX_RESTARTS)).action, "gave_up")

    def test_a_corrupt_state_file_does_not_unbound_the_restarts(self) -> None:
        container = FakeContainer(running=False)
        supervisor = self.supervisor(container)
        supervisor.tick(at=self.moment())
        (self.state_dir / "supervisor.json").write_text("not json", encoding="utf-8")
        revived = Supervisor(container, self.state_dir, alert=lambda *_: None)
        state = revived.state()
        self.assertIsInstance(state, SupervisorState)
        self.assertEqual(state.restarts, ())

    def test_a_decision_says_what_it_did_and_why(self) -> None:
        decision = Decision(action="gave_up", reason="restarting is not recovering this")
        self.assertIn("recovering", decision.as_json()["reason"])
        self.assertEqual(decision.as_json()["action"], "gave_up")


if __name__ == "__main__":
    unittest.main()
