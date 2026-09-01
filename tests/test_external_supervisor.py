"""Something outside the container has to notice when it dies.

The in-process supervisor cannot restart a process it is part of, and Compose
is deliberately set to `restart: "no"` because Docker's own policy has no idea
what a crash loop is, when restarting has stopped helping, or when the right
answer is to roll back instead. So the deployment has an external supervisor,
and these are the decisions it has to get right.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from assistant.scotty_supervisor.supervise import (
    MAX_RESTARTS,
    RESTART_WINDOW,
    Decision,
    Supervisor,
    SupervisorState,
)


class FakeContainer:
    """A container that behaves like Docker: it can die, and it can refuse."""

    def __init__(
        self, *, running: bool = True, starts_ok: bool = True, present: bool | None = None
    ) -> None:
        self.running = running
        self.starts_ok = starts_ok
        self.starts = 0
        self.stops = 0
        self.exists = True if present is None else present

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


class SupervisionGateTests(unittest.TestCase):
    """What has to be true before supervision starts anything at all.

    Four separate ways the old pass could act when it should not have: it
    started any container it found stopped, including one a reboot surfaced
    before setup had ever been accepted; it treated "the process is running" as
    "the deployment is healthy"; it read a corrupted state file as an empty one
    and handed back a fresh restart budget; and it recorded the attempt after
    making it, so a crash in between lost the fact that it had tried.
    """

    def supervisor(self, container, **kwargs):
        directory = tempfile.TemporaryDirectory(prefix="scotty-gate-")
        self.addCleanup(directory.cleanup)
        self.state_dir = Path(directory.name)
        self.alerts: list[tuple[str, str]] = []
        return Supervisor(
            container,
            self.state_dir,
            alert=lambda kind, text: self.alerts.append((kind, text)),
            **kwargs,
        )

    def test_nothing_starts_until_activation_has_been_accepted(self) -> None:
        container = FakeContainer(present=True, running=False)
        supervisor = self.supervisor(container, activated=lambda: False)
        decision = supervisor.tick()
        self.assertEqual(decision.action, "blocked")
        self.assertIn("accepted", decision.reason)
        self.assertEqual(container.starts, 0)

    def test_an_accepted_deployment_starts_normally(self) -> None:
        container = FakeContainer(present=True, running=False)
        supervisor = self.supervisor(container, activated=lambda: True)
        self.assertEqual(supervisor.tick().action, "started")
        self.assertEqual(container.starts, 1)

    def test_a_running_container_is_not_called_healthy_on_its_own(self) -> None:
        container = FakeContainer(present=True, running=True)
        supervisor = self.supervisor(
            container, health=lambda: ("degraded", "the gateway is not connected")
        )
        decision = supervisor.tick()
        self.assertEqual(decision.action, "degraded")
        self.assertIn("gateway", decision.reason)
        # Degraded is reported once rather than every pass.
        supervisor.tick()
        self.assertEqual([kind for kind, _ in self.alerts], ["incident"])

    def test_a_healthy_running_container_is_quiet(self) -> None:
        container = FakeContainer(present=True, running=True)
        supervisor = self.supervisor(container, health=lambda: ("healthy", ""))
        self.assertEqual(supervisor.tick().action, "none")
        self.assertEqual(self.alerts, [])

    def test_a_corrupt_state_file_blocks_rather_than_granting_a_fresh_budget(self) -> None:
        container = FakeContainer(present=True, running=False)
        supervisor = self.supervisor(container, activated=lambda: True)
        (self.state_dir / "supervisor.json").write_text("{ not json", encoding="utf-8")
        decision = supervisor.tick()
        self.assertEqual(decision.action, "blocked")
        self.assertIn("state", decision.reason)
        # Nothing started, and nothing was silently reset.
        self.assertEqual(container.starts, 0)

    def test_the_attempt_is_recorded_before_it_is_made(self) -> None:
        """A crash between deciding and starting must not lose the decision."""

        recorded: list[int] = []

        class Watching(FakeContainer):
            def start(inner) -> bool:  # noqa: N805 - inner is the container
                recorded.append(len(self.supervisor_state().restarts))
                return super().start()

        container = Watching(present=True, running=False)
        supervisor = self.supervisor(container, activated=lambda: True)
        self.supervisor_state = supervisor.state
        supervisor.tick()
        # The restart was already on record when the start was attempted.
        self.assertEqual(recorded, [1])


class UninstallTests(unittest.TestCase):
    """Uninstall leaves nothing consuming Discord and nothing on the network.

    The defect was quiet and complete: supervision, the broker and the egress
    guard were disabled, and the container carried on running with the bridge
    up. An "uninstalled" deployment was still connected to Discord and still
    answering people.
    """

    def cli(self, *, running: bool = True, owned: bool = True, stops: bool = True):
        from assistant.scotty_supervisor import cli

        calls: list[list[str]] = []
        state = {"running": running}

        def run(command):
            calls.append(list(command))
            if command[:2] == ["docker", "inspect"]:
                if "{{.State.Running}}" in command:
                    return 0, "true\n" if state["running"] else "false\n"
                return (0, "managed\n") if owned else (0, "somebody-elses\n")
            if command[:2] == ["docker", "stop"]:
                if stops:
                    state["running"] = False
                return (0, "") if stops else (1, "")
            if command[:3] == ["docker", "network", "inspect"]:
                return 0, "managed\n"
            return 0, ""

        directory = tempfile.TemporaryDirectory(prefix="scotty-uninstall-")
        self.addCleanup(directory.cleanup)
        patches = [
            mock.patch.object(cli, "_run", run),
            mock.patch.object(cli, "SUPERVISOR_DIR", Path(directory.name)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return cli, calls

    def output(self, cli):
        with mock.patch("sys.stdout") as stdout:
            status = cli.run(["uninstall"])
        written = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        try:
            return status, json.loads(written)
        except json.JSONDecodeError:
            return status, {}

    def test_the_container_is_stopped_and_the_network_removed(self) -> None:
        cli, calls = self.cli()
        status, report = self.output(cli)
        self.assertEqual(status, 0)
        rendered = [" ".join(item) for item in calls]
        self.assertIn("docker stop scotty", rendered)
        self.assertIn("docker network rm scotty-egress", rendered)
        self.assertTrue(any("stopped" in str(item) for item in report.get("removed", [])))

    def test_supervision_is_held_first_so_nothing_restarts_it(self) -> None:
        cli, _ = self.cli()
        self.output(cli)
        from assistant.scotty_supervisor.supervise import DockerContainer, Supervisor

        held = Supervisor(
            DockerContainer("scotty", lambda command: (0, "")),
            cli.SUPERVISOR_DIR,
            alert=lambda kind, text: None,
        ).state()
        self.assertTrue(held.hold_reason)

    def test_a_container_this_product_does_not_own_is_refused(self) -> None:
        cli, calls = self.cli(owned=False)
        status, _ = self.output(cli)
        self.assertEqual(status, 1)
        self.assertNotIn("docker stop scotty", [" ".join(item) for item in calls])

    def test_a_container_that_will_not_stop_removes_nothing(self) -> None:
        cli, calls = self.cli(stops=False)
        status, _ = self.output(cli)
        self.assertEqual(status, 1)
        rendered = [" ".join(item) for item in calls]
        self.assertNotIn("docker container rm scotty", rendered)
        self.assertNotIn("docker network rm scotty-egress", rendered)

    def test_the_deployment_s_own_data_is_left_alone(self) -> None:
        cli, _ = self.cli()
        _, report = self.output(cli)
        left = " ".join(str(item) for item in report.get("left_in_place", []))
        self.assertIn("/srv/Scotty", left)
        self.assertIn("/var/lib/scotty", left)


if __name__ == "__main__":
    unittest.main()
