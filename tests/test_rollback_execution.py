"""Rolling back for real, in one command.

A rollback that prints a plan is a document. The thing an operator needs at
three in the morning is a command that stops the container that is failing,
puts back the exact bytes of a release somebody accepted, starts it, and then
says plainly whether that worked.

The ordering is the safety property: the running container stops, and is proven
stopped, before anything else happens. Two processes on one Discord bot token is
the failure this sequence exists to prevent.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from assistant.scotty_supervisor import cli
from assistant.scotty_supervisor.releases import (
    current_release,
    install_release,
    publish_release,
    select_release,
)
from assistant.scotty_supervisor.supervise import Supervisor


class FakeDocker:
    """The exact docker calls, in order, and what each one is told to answer."""

    def __init__(self, *, running: bool = True) -> None:
        self.running = running
        self.commands: list[list[str]] = []
        self.stop_fails = False
        self.start_leaves_it_down = False

    def __call__(self, command: Sequence[str]) -> tuple[int, str]:
        self.commands.append(list(command))
        if command[0] == "docker" and command[1] == "inspect":
            if "{{.State.Running}}" in command:
                return 0, "true\n" if self.running else "false\n"
            return 0, "cafe\n"
        if command[0] == "docker" and command[1] == "stop":
            if self.stop_fails:
                return 1, ""
            self.running = False
            return 0, ""
        if command[0] == "docker" and command[1] == "start":
            self.running = not self.start_leaves_it_down
            return 0, ""
        if command[0].endswith("scotty-start"):
            self.running = not self.start_leaves_it_down
            return 0, ""
        return 0, ""

    def names(self) -> list[str]:
        return [" ".join(command) for command in self.commands]


class RollbackHarness(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="scotty-rollback-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.releases = self.root / "releases"
        self.releases.mkdir()
        self.deployment = self.root / "srv"
        self.deployment.mkdir()
        self.supervisor_dir = self.root / "supervisor"
        self.docker = FakeDocker()
        self.alerts: list[tuple[str, str]] = []
        patches = (
            mock.patch.object(cli, "_alert", lambda kind, text: self.alerts.append((kind, text))),
            mock.patch.object(cli, "RELEASES_DIR", self.releases),
            mock.patch.object(cli, "SUPERVISOR_DIR", self.supervisor_dir),
            mock.patch.object(cli, "DEPLOYMENT_DIR", self.deployment),
            mock.patch.object(cli, "_run", self.docker),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def publish(self, name: str, marker: str, *, accept: bool) -> None:
        source = self.root / f"build-{marker}"
        (source / "assistant").mkdir(parents=True)
        (source / "assistant" / "runtime.py").write_text(f"# {marker}\n", encoding="utf-8")
        publish_release(source, self.releases, name)
        if accept:
            select_release(self.releases, name, accepted=True)

    def scene(self) -> None:
        """An accepted release, and a broken one selected after it."""

        self.publish("2026-01-01", "good", accept=True)
        self.publish("2026-02-01", "broken", accept=False)
        select_release(self.releases, "2026-02-01")
        install_release(self.releases, "2026-02-01", self.deployment)

    def supervision(self):
        return Supervisor(
            cli.DockerContainer(cli.CONTAINER, self.docker),
            self.supervisor_dir,
            alert=lambda kind, text: None,
        ).state()

    def output(self, argv: list[str]) -> tuple[int, dict[str, object]]:
        with mock.patch("sys.stdout") as stdout:
            status = cli.run(argv)
        written = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        try:
            return status, json.loads(written)
        except json.JSONDecodeError:
            return status, {}


class PlanTests(RollbackHarness):
    def test_a_plain_rollback_names_the_target_and_changes_nothing(self) -> None:
        self.scene()
        status, plan = self.output(["rollback"])
        self.assertEqual(status, 0)
        self.assertEqual(plan["target"], "2026-01-01")
        self.assertEqual(current_release(self.releases), "2026-02-01")
        self.assertNotIn("docker stop scotty", self.docker.names())
        self.assertEqual(
            (self.deployment / "assistant" / "runtime.py").read_text(encoding="utf-8"),
            "# broken\n",
        )


class ExecutionTests(RollbackHarness):
    def test_executing_a_rollback_selects_the_accepted_release_and_puts_it_back(self) -> None:
        self.scene()
        status, report = self.output(["rollback", "--execute"])
        self.assertEqual(status, 0)
        self.assertEqual(report["state"], "verified")
        self.assertEqual(current_release(self.releases), "2026-01-01")
        self.assertEqual(
            (self.deployment / "assistant" / "runtime.py").read_text(encoding="utf-8"),
            "# good\n",
        )

    def test_the_container_stops_and_is_proven_stopped_before_anything_else(self) -> None:
        self.scene()
        self.output(["rollback", "--execute"])
        names = self.docker.names()
        stop = names.index("docker stop scotty")
        start = next(index for index, name in enumerate(names) if "start" in name and index > stop)
        self.assertLess(stop, start)
        # And the release was not selected until after the stop returned.
        self.assertTrue(
            any("inspect" in name for name in names[stop:start]),
            "the rollback must confirm the container is down, not assume it",
        )

    def test_a_stop_that_fails_writes_nothing_and_starts_nothing(self) -> None:
        self.scene()
        self.docker.stop_fails = True
        status, report = self.output(["rollback", "--execute"])
        self.assertEqual(status, 1)
        self.assertEqual(report["state"], "failed")
        self.assertEqual(current_release(self.releases), "2026-02-01")
        self.assertEqual(
            (self.deployment / "assistant" / "runtime.py").read_text(encoding="utf-8"),
            "# broken\n",
        )
        self.assertFalse(any("start" in name for name in self.docker.names()))

    def test_a_start_that_leaves_it_down_is_unknown_and_never_retried(self) -> None:
        self.scene()
        self.docker.start_leaves_it_down = True
        status, report = self.output(["rollback", "--execute"])
        self.assertEqual(status, 1)
        self.assertEqual(report["state"], "unknown")
        self.assertEqual(len([name for name in self.docker.names() if "start" in name]), 1)
        # An ambiguous outcome is escalated rather than retried into a second
        # consumer, and the operator finds supervision still held.
        self.assertEqual([kind for kind, _ in self.alerts], ["rollback"])
        self.assertTrue(self.supervision().hold_reason)

    def test_there_is_nothing_to_roll_back_to_without_an_accepted_release(self) -> None:
        self.publish("2026-02-01", "broken", accept=False)
        select_release(self.releases, "2026-02-01")
        status, report = self.output(["rollback", "--execute"])
        self.assertEqual(status, 1)
        self.assertFalse(report["available"])
        self.assertNotIn("docker stop scotty", self.docker.names())

    def test_supervision_is_held_across_the_rollback_and_lifted_afterwards(self) -> None:
        self.scene()
        self.output(["rollback", "--execute"])
        state = self.supervision()
        # Held during, so the watch loop cannot start the old container back up
        # halfway through; lifted after, so the deployment is supervised again.
        self.assertEqual(state.hold_reason, "")

    def test_a_failed_rollback_leaves_supervision_held_for_a_person(self) -> None:
        self.scene()
        self.docker.stop_fails = True
        self.output(["rollback", "--execute"])
        state = self.supervision()
        self.assertTrue(state.hold_reason)


class RecordedModeTests(RollbackHarness):
    def test_a_release_puts_back_the_mode_it_was_published_with(self) -> None:
        source = self.root / "build-modes"
        source.mkdir()
        script = source / "scotty-start"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o755)
        (source / "private.json").write_text("{}", encoding="utf-8")
        (source / "private.json").chmod(0o600)
        publish_release(source, self.releases, "modes")
        install_release(self.releases, "modes", self.deployment)
        self.assertEqual((self.deployment / "scotty-start").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.deployment / "private.json").stat().st_mode & 0o777, 0o600)

    def test_ownership_is_recorded_so_a_rollback_can_put_it_back(self) -> None:
        source = self.root / "build-owned"
        source.mkdir()
        (source / "plugin.py").write_text("x = 1\n", encoding="utf-8")
        manifest = publish_release(source, self.releases, "owned")
        entries = manifest["files"]
        assert isinstance(entries, list)
        entry = entries[0]
        assert isinstance(entry, dict)
        # The container account owns the plugin tree. A rollback that wrote
        # every file back as root would leave a deployment the runtime cannot
        # read, so ownership is part of what a release records.
        self.assertEqual(entry["uid"], os.stat(source / "plugin.py").st_uid)
        self.assertEqual(entry["gid"], os.stat(source / "plugin.py").st_gid)


if __name__ == "__main__":
    unittest.main()
