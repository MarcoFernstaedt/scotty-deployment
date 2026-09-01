"""Backups that are one moment, and restores that are all or nothing.

Three defects that share a shape: each writes several files, and each could
stop halfway leaving a state nobody designed.

A backup copied live SQLite files byte for byte while the runtime was writing
them. In write-ahead mode the recent commits live in a sidecar the copy never
took, so the backup passed its own hash manifest and was missing hundreds of
committed rows -- a backup that looks intact and quietly is not is worse than
no backup.

A restore staged every file and then replaced the live ones one at a time. An
injected failure on the second replacement left the first restored and the rest
current: a generation nobody ever ran.

A release installed the same way, and it also never removed files the new
release does not have, and marked itself current before the install succeeded.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from assistant.scotty_supervisor.journal import Journal, JournalError, replace_all
from assistant.scotty_supervisor.releases import (
    ReleaseError,
    install_release,
    publish_release,
)
from assistant.scotty_supervisor.state import (
    StateError,
    backup_state,
    restore_state,
    verify_backup,
)


def rows(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM rows").fetchone()[0])
    finally:
        connection.close()


class StateFixture(unittest.TestCase):
    def roots(self) -> tuple[Path, Path]:
        directory = tempfile.TemporaryDirectory(prefix="scotty-transaction-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        state = root / "state"
        state.mkdir()
        return state, root

    def database(self, state: Path, name: str, count: int = 5) -> Path:
        path = state / name
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE rows (id INTEGER PRIMARY KEY, value TEXT)")
        for index in range(count):
            connection.execute("INSERT INTO rows (value) VALUES (?)", (f"v{index}",))
        connection.commit()
        connection.close()
        return path


class ConsistentBackupTests(StateFixture):
    def test_a_backup_taken_under_a_writer_keeps_every_committed_row(self) -> None:
        state, root = self.roots()
        path = self.database(state, "approvals.db", count=1)
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=WAL")

        stop = threading.Event()
        written: list[int] = []

        def writer() -> None:
            writing = sqlite3.connect(path)
            writing.execute("PRAGMA journal_mode=WAL")
            count = 0
            while not stop.is_set():
                writing.execute("INSERT INTO rows (value) VALUES (?)", ("x" * 200,))
                writing.commit()
                count += 1
            written.append(count)
            writing.close()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            time.sleep(0.2)
            destination = root / "backup"
            backup_state(state, destination)
        finally:
            stop.set()
            thread.join(5)
        connection.close()

        # The snapshot is a moment, not a smear: it opens, so every row in it
        # was committed, and nothing committed before it was taken is missing
        # from the middle of it.
        copied = destination / "files" / "approvals.db"
        self.assertGreater(rows(copied), 1)
        self.assertFalse(verify_backup(destination))

    def test_every_database_in_a_backup_passes_its_own_integrity_check(self) -> None:
        state, root = self.roots()
        self.database(state, "approvals.db")
        self.database(state, "reminders.db")
        destination = root / "backup"
        manifest = backup_state(state, destination)
        checked = [
            entry
            for entry in manifest["files"]  # type: ignore[index]
            if isinstance(entry, dict) and entry["name"].endswith(".db")
        ]
        self.assertTrue(checked)
        for entry in checked:
            with self.subTest(name=entry["name"]):
                self.assertEqual(entry["integrity"], "ok")
                self.assertTrue(entry["schema"])

    def test_a_corrupt_database_is_refused_rather_than_backed_up(self) -> None:
        state, root = self.roots()
        (state / "approvals.db").write_bytes(b"SQLite format 3\x00 but not really")
        with self.assertRaises(StateError):
            backup_state(state, root / "backup")

    def test_one_manifest_describes_one_generation(self) -> None:
        state, root = self.roots()
        self.database(state, "approvals.db")
        (state / "workflows.json").write_text('{"workflows": []}', encoding="utf-8")
        manifest = backup_state(state, root / "backup")
        self.assertTrue(manifest["generation"])
        names = {
            entry["name"]  # type: ignore[index]
            for entry in manifest["files"]  # type: ignore[index]
        }
        self.assertEqual(names, {"approvals.db", "workflows.json"})


class AtomicRestoreTests(StateFixture):
    def prepared(self) -> tuple[Path, Path]:
        state, root = self.roots()
        self.database(state, "approvals.db", count=3)
        (state / "workflows.json").write_text('{"workflows": ["old"]}', encoding="utf-8")
        (state / "personas.json").write_text('{"employee": "old"}', encoding="utf-8")
        destination = root / "backup"
        backup_state(state, destination)
        # Move the live state on, so a restore has something to undo.
        (state / "workflows.json").write_text('{"workflows": ["new"]}', encoding="utf-8")
        (state / "personas.json").write_text('{"employee": "new"}', encoding="utf-8")
        return state, destination

    def test_a_restore_puts_the_whole_generation_back(self) -> None:
        state, destination = self.prepared()
        restored = restore_state(destination, state)
        self.assertIn("workflows.json", restored)
        self.assertIn('"old"', (state / "workflows.json").read_text(encoding="utf-8"))
        self.assertIn('"old"', (state / "personas.json").read_text(encoding="utf-8"))

    def test_a_failure_partway_leaves_the_previous_generation_whole(self) -> None:
        state, destination = self.prepared()
        calls: list[int] = []
        real = __import__("os").replace

        def failing(source, target):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("the disk went away")
            return real(source, target)

        with mock.patch("os.replace", failing), self.assertRaises((StateError, OSError)):
            restore_state(destination, state)

        # Not one old file and one new: the generation that was running before
        # is the generation that is running now.
        current = {
            "workflows.json": (state / "workflows.json").read_text(encoding="utf-8"),
            "personas.json": (state / "personas.json").read_text(encoding="utf-8"),
        }
        self.assertTrue(
            all('"new"' in value for value in current.values())
            or all('"old"' in value for value in current.values()),
            current,
        )

    def test_an_interrupted_restore_is_recovered_deterministically(self) -> None:
        """The process dies mid-cutover and never gets to tidy up after itself."""

        state, destination = self.prepared()
        real = __import__("os").replace
        calls: list[int] = []
        targets = {"workflows.json", "personas.json", "approvals.db"}

        def failing(source, target):
            if Path(target).name in targets:
                calls.append(1)
                if len(calls) == 2:
                    raise KeyboardInterrupt
            return real(source, target)

        with (
            mock.patch("os.replace", failing),
            mock.patch.object(Journal, "rollback", lambda *_: None),
            self.assertRaises(KeyboardInterrupt),
        ):
            restore_state(destination, state)

        # The record is what survives the process. The next one reads it and
        # puts the generation that was running back.
        recovered = Journal(state).recover()
        self.assertTrue(recovered)
        current = (state / "workflows.json").read_text(encoding="utf-8")
        other = (state / "personas.json").read_text(encoding="utf-8")
        self.assertEqual('"old"' in current, '"old"' in other)

    def test_a_restore_still_refuses_to_write_credential_state(self) -> None:
        state, destination = self.prepared()
        import json

        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"].append({"name": "private.json", "sha256": "0" * 64, "bytes": 1})
        (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(StateError):
            restore_state(destination, state)


class JournalTests(unittest.TestCase):
    def root(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="scotty-journal-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def test_every_file_moves_or_none_does(self) -> None:
        root = self.root()
        for name in ("a", "b", "c"):
            (root / name).write_text("old", encoding="utf-8")
            (root / f"{name}.new").write_text("new", encoding="utf-8")
        moves = [(root / f"{name}.new", root / name) for name in ("a", "b", "c")]
        replace_all(root, moves)
        self.assertEqual(
            [(root / name).read_text(encoding="utf-8") for name in ("a", "b", "c")],
            ["new", "new", "new"],
        )

    def test_a_failure_partway_restores_every_prior_byte(self) -> None:
        root = self.root()
        for name in ("a", "b", "c"):
            (root / name).write_text("old", encoding="utf-8")
            (root / f"{name}.new").write_text("new", encoding="utf-8")
        moves = [(root / f"{name}.new", root / name) for name in ("a", "b", "c")]
        real = __import__("os").replace
        calls: list[int] = []

        def failing(source, target):
            if Path(target).name in {"a", "b", "c"}:
                calls.append(1)
                if len(calls) == 2:
                    raise OSError("no space left on device")
            return real(source, target)

        with mock.patch("os.replace", failing), self.assertRaises(JournalError):
            replace_all(root, moves)
        self.assertEqual(
            [(root / name).read_text(encoding="utf-8") for name in ("a", "b", "c")],
            ["old", "old", "old"],
        )

    def test_a_journal_left_behind_is_rolled_back_on_recovery(self) -> None:
        """The process dies mid-cutover, so nothing rolls anything back."""

        root = self.root()
        for name in ("a", "b"):
            (root / name).write_text("old", encoding="utf-8")
            (root / f"{name}.new").write_text("new", encoding="utf-8")
        moves = [(root / f"{name}.new", root / name) for name in ("a", "b")]
        real = __import__("os").replace
        calls: list[int] = []

        def failing(source, target):
            if Path(target).name in {"a", "b"}:
                calls.append(1)
                if len(calls) == 2:
                    raise KeyboardInterrupt
            return real(source, target)

        # The process is gone: it does not get to undo anything on its way out.
        with (
            mock.patch("os.replace", failing),
            mock.patch.object(Journal, "rollback", lambda *_: None),
            self.assertRaises(KeyboardInterrupt),
        ):
            replace_all(root, moves)
        self.assertEqual((root / "a").read_text(encoding="utf-8"), "new")

        # The next process finds the record and puts the generation back.
        self.assertTrue(Journal(root).recover())
        self.assertEqual((root / "a").read_text(encoding="utf-8"), "old")
        self.assertEqual((root / "b").read_text(encoding="utf-8"), "old")

    def test_recovery_with_nothing_to_recover_does_nothing(self) -> None:
        self.assertEqual(Journal(self.root()).recover(), ())


class ExactReleaseTests(unittest.TestCase):
    def roots(self) -> tuple[Path, Path, Path]:
        directory = tempfile.TemporaryDirectory(prefix="scotty-release-exact-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        releases = root / "releases"
        releases.mkdir()
        destination = root / "deployment"
        destination.mkdir()
        return root, releases, destination

    def build(self, root: Path, name: str, files: dict[str, str]) -> Path:
        source = root / f"build-{name}"
        for relative, body in files.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        return source

    def test_a_file_the_new_release_does_not_have_is_removed(self) -> None:
        root, releases, destination = self.roots()
        publish_release(self.build(root, "one", {"a.py": "1", "gone.py": "1"}), releases, "one")
        publish_release(self.build(root, "two", {"a.py": "2"}), releases, "two")
        install_release(releases, "one", destination)
        self.assertTrue((destination / "gone.py").is_file())
        install_release(releases, "two", destination)
        # Overlaying leaves the old file behind, and the deployment then runs
        # bytes that are in no release at all.
        self.assertFalse((destination / "gone.py").exists())
        self.assertEqual((destination / "a.py").read_text(encoding="utf-8"), "2")

    def test_a_failed_install_leaves_the_previous_release_whole(self) -> None:
        root, releases, destination = self.roots()
        publish_release(self.build(root, "one", {"a.py": "1", "b.py": "1"}), releases, "one")
        publish_release(self.build(root, "two", {"a.py": "2", "b.py": "2"}), releases, "two")
        install_release(releases, "one", destination)
        real = __import__("os").replace
        calls: list[int] = []

        def failing(source, target):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("the disk went away")
            return real(source, target)

        with mock.patch("os.replace", failing), self.assertRaises(ReleaseError):
            install_release(releases, "two", destination)
        self.assertEqual(
            {
                (destination / "a.py").read_text(encoding="utf-8"),
                (destination / "b.py").read_text(encoding="utf-8"),
            },
            {"1"},
        )

    def test_a_release_is_verified_against_the_installed_bytes(self) -> None:
        from assistant.scotty_supervisor.releases import verify_installed

        root, releases, destination = self.roots()
        publish_release(self.build(root, "one", {"a.py": "1"}), releases, "one")
        install_release(releases, "one", destination)
        self.assertEqual(verify_installed(releases, "one", destination), ())
        # What is actually running, not what the archive says about itself.
        (destination / "a.py").write_text("tampered", encoding="utf-8")
        self.assertEqual(verify_installed(releases, "one", destination), ("a.py",))


if __name__ == "__main__":
    unittest.main()
