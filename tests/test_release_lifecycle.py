"""Releases you can go back to, and state you can actually put back.

A backup that can only be previewed is a backup nobody has tested. A rollback
that has no manifest is a hope. This is the lifecycle: immutable release
directories with hashes, one atomically-selected current release, a restore that
stages and validates before it cuts over, and a rollback that names a release
somebody actually accepted.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from assistant.scotty_supervisor.releases import (
    ReleaseError,
    current_release,
    install_release,
    publish_release,
    rollback,
    select_release,
    verify_release,
)
from assistant.scotty_supervisor.state import (
    StateError,
    backup_state,
    restore_state,
    verify_backup,
)


class ReleaseHarness(unittest.TestCase):
    def root(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="scotty-releases-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def payload(self, root: Path, marker: str) -> Path:
        source = root / f"payload-{marker}"
        (source / "assistant").mkdir(parents=True)
        (source / "assistant" / "thing.py").write_text(f"# {marker}\n", encoding="utf-8")
        (source / "compose.yaml").write_text(f"# {marker}\n", encoding="utf-8")
        return source


class ReleaseTests(ReleaseHarness):
    def test_a_published_release_is_hash_bound_and_readable_back(self) -> None:
        root = self.root()
        manifest = publish_release(self.payload(root, "a"), root / "releases", "2026.09.01")
        self.assertEqual(manifest["release"], "2026.09.01")
        self.assertTrue(manifest["files"])
        for entry in manifest["files"]:
            self.assertEqual(len(entry["sha256"]), 64)
        self.assertEqual(verify_release(root / "releases" / "2026.09.01"), ())

    def test_a_tampered_release_is_reported_not_selected(self) -> None:
        root = self.root()
        publish_release(self.payload(root, "a"), root / "releases", "2026.09.01")
        target = root / "releases" / "2026.09.01" / "files" / "compose.yaml"
        target.write_text("# tampered\n", encoding="utf-8")
        self.assertEqual(verify_release(root / "releases" / "2026.09.01"), ("compose.yaml",))
        with self.assertRaises(ReleaseError):
            select_release(root / "releases", "2026.09.01")

    def test_a_release_cannot_be_republished_over_itself(self) -> None:
        root = self.root()
        publish_release(self.payload(root, "a"), root / "releases", "2026.09.01")
        with self.assertRaises(ReleaseError):
            publish_release(self.payload(root, "b"), root / "releases", "2026.09.01")

    def test_selection_is_atomic_and_names_exactly_one_release(self) -> None:
        root = self.root()
        releases = root / "releases"
        publish_release(self.payload(root, "a"), releases, "2026.09.01")
        publish_release(self.payload(root, "b"), releases, "2026.09.02")
        select_release(releases, "2026.09.01")
        self.assertEqual(current_release(releases), "2026.09.01")
        select_release(releases, "2026.09.02")
        self.assertEqual(current_release(releases), "2026.09.02")

    def test_accepting_a_release_is_what_makes_it_a_rollback_target(self) -> None:
        root = self.root()
        releases = root / "releases"
        publish_release(self.payload(root, "a"), releases, "2026.09.01")
        publish_release(self.payload(root, "b"), releases, "2026.09.02")
        select_release(releases, "2026.09.02")

        # Nothing has been accepted, so there is nowhere to go back to.
        plan = rollback(releases)
        self.assertFalse(plan.available)

        select_release(releases, "2026.09.01", accepted=True)
        select_release(releases, "2026.09.02")
        plan = rollback(releases)
        self.assertTrue(plan.available)
        self.assertEqual(plan.target, "2026.09.01")

    def test_installing_a_release_puts_the_exact_recorded_bytes_in_place(self) -> None:
        root = self.root()
        releases = root / "releases"
        publish_release(self.payload(root, "a"), releases, "2026.09.01")
        destination = root / "live"
        installed = install_release(releases, "2026.09.01", destination)
        self.assertIn("compose.yaml", installed)
        self.assertEqual((destination / "compose.yaml").read_text(encoding="utf-8"), "# a\n")

    def test_installing_a_tampered_release_is_refused_before_it_writes(self) -> None:
        root = self.root()
        releases = root / "releases"
        publish_release(self.payload(root, "a"), releases, "2026.09.01")
        (releases / "2026.09.01" / "files" / "compose.yaml").write_text("x", encoding="utf-8")
        destination = root / "live"
        with self.assertRaises(ReleaseError):
            install_release(releases, "2026.09.01", destination)
        self.assertFalse((destination / "compose.yaml").exists())


class StateHarness(unittest.TestCase):
    def state(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="scotty-state-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "workflows.json").write_text('{"workflows": []}', encoding="utf-8")
        (root / "personas.json").write_text('{"employee": "Nova"}', encoding="utf-8")
        (root / "reminders.db").write_bytes(b"SQLite format 3\x00reminders")
        (root / "google-oauth.main_operator.json").write_text(
            '{"refresh_token": "synthetic-refresh"}', encoding="utf-8"
        )
        return root

    def destination(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="scotty-backup-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name) / "backup"


class RestoreTests(StateHarness):
    def test_a_restore_actually_puts_the_state_back(self) -> None:
        state, destination = self.state(), self.destination()
        backup_state(state, destination)
        (state / "workflows.json").write_text('{"workflows": ["changed"]}', encoding="utf-8")
        (state / "personas.json").unlink()

        restored = restore_state(destination, state)
        self.assertIn("workflows.json", restored)
        self.assertEqual(
            json.loads((state / "workflows.json").read_text(encoding="utf-8")),
            {"workflows": []},
        )
        # A file that was deleted since the backup comes back too.
        self.assertTrue((state / "personas.json").exists())

    def test_a_restore_stages_and_validates_before_it_touches_anything(self) -> None:
        state, destination = self.state(), self.destination()
        backup_state(state, destination)
        (destination / "files" / "workflows.json").write_text("tampered", encoding="utf-8")
        original = (state / "workflows.json").read_text(encoding="utf-8")

        with self.assertRaises(StateError):
            restore_state(destination, state)
        # Nothing was written, including the files that were still intact.
        self.assertEqual((state / "workflows.json").read_text(encoding="utf-8"), original)
        self.assertEqual(list(state.glob("*.staging")), [])

    def test_a_restore_never_writes_credential_state_back(self) -> None:
        state, destination = self.state(), self.destination()
        backup_state(state, destination)
        token = state / "google-oauth.main_operator.json"
        token.write_text('{"refresh_token": "rotated-since"}', encoding="utf-8")
        restore_state(destination, state)
        # The token was rotated after the backup; a restore must not undo that.
        self.assertIn("rotated-since", token.read_text(encoding="utf-8"))

    def test_an_interrupted_cutover_leaves_no_half_written_file(self) -> None:
        state, destination = self.state(), self.destination()
        backup_state(state, destination)

        calls = {"count": 0}

        def failing_replace(source, target):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("interrupted")
            import os

            os.replace(source, target)

        with self.assertRaises(StateError):
            restore_state(destination, state, replace=failing_replace)
        # Every file is either its old content or its restored content, and
        # nothing is left staged.
        self.assertEqual(list(state.glob("*.staging")), [])
        self.assertTrue((state / "workflows.json").read_text(encoding="utf-8").strip())

    def test_verification_reports_exactly_which_file_disagrees(self) -> None:
        state, destination = self.state(), self.destination()
        backup_state(state, destination)
        (destination / "files" / "reminders.db").write_bytes(b"different")
        self.assertEqual(verify_backup(destination), ("reminders.db",))


class OwnershipTests(ReleaseHarness):
    def test_a_release_built_by_a_person_never_hands_them_the_deployment(self) -> None:
        from unittest import mock

        from assistant.scotty_supervisor.releases import _restore_attributes

        root = self.root()
        target = root / "plugin.py"
        target.write_text("x = 1\n", encoding="utf-8")
        chowned: list[tuple[int, int]] = []

        def record(_path, uid, gid):
            chowned.append((uid, gid))

        with mock.patch("os.geteuid", return_value=0), mock.patch("os.chown", record):
            # A release published from somebody's checkout carries their uid.
            _restore_attributes(target, {"mode": "0o644", "uid": 1000, "gid": 1000})
            self.assertEqual(chowned, [])
            # The accounts the deployment actually uses are restored.
            _restore_attributes(target, {"mode": "0o600", "uid": 10000, "gid": 10000})
            _restore_attributes(target, {"mode": "0o755", "uid": 0, "gid": 0})
            self.assertEqual(chowned, [(10000, 10000), (0, 0)])


if __name__ == "__main__":
    unittest.main()
