"""Backup, restore, and rollback of non-secret state.

What must survive a bad release is the work: workflows, personas, reminders,
approvals, effect records, and the property cards' provenance. What must never
be copied into a backup is a credential, a token, or an OAuth record — a backup
is not a place for secrets, and a restore is not a way to resurrect one.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from assistant.scotty_business.backup import (
    BACKUP_INCLUDES,
    ROLLBACK_COMMAND,
    SECRET_NAMES,
    BackupError,
    backup_state,
    restore_state,
    rollback_guidance,
    verify_backup,
)
from assistant.scotty_supervisor import state as host_state


def make_database(path, label: str) -> None:
    """A real SQLite database, because a backup now proves it can open one.

    The fixtures used to write the file's magic bytes and nothing else. That
    was enough while a backup copied bytes; it is not enough now that a backup
    snapshots a database through SQLite itself and refuses one it cannot read.
    """

    import sqlite3

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE IF NOT EXISTS rows (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO rows (value) VALUES (?)", (label,))
        connection.commit()
    finally:
        connection.close()


class StateFixture(unittest.TestCase):
    def state(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="scotty-state-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "workflows.json").write_text('{"workflows": []}', encoding="utf-8")
        (root / "personas.json").write_text('{"employee": "Nova"}', encoding="utf-8")
        make_database(root / "reminders.db", "reminders")
        make_database(root / "approvals.db", "approvals")
        make_database(root / "property-effects.db", "property-effects")
        make_database(root / "budgets.db", "budgets")
        # These must never be copied.
        (root / "google-oauth.main_operator.json").write_text(
            '{"refresh_token": "synthetic-refresh"}', encoding="utf-8"
        )
        (root / "google-consent.main_operator.json").write_text("{}", encoding="utf-8")
        (root / "private.json").write_text('{"secrets": {}}', encoding="utf-8")
        return root

    def destination(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="scotty-backup-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name) / "backup"


class BackupTests(StateFixture):
    def test_a_backup_carries_the_work_and_never_a_credential(self) -> None:
        state, destination = self.state(), self.destination()
        manifest = backup_state(state, destination)
        copied = {item["name"] for item in manifest["files"]}
        self.assertIn("workflows.json", copied)
        self.assertIn("reminders.db", copied)
        for secret in SECRET_NAMES:
            self.assertNotIn(secret, copied)
        self.assertFalse(any("oauth" in name for name in copied))
        rendered = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in destination.rglob("*")
            if path.is_file()
        )
        self.assertNotIn("synthetic-refresh", rendered)

    def test_every_backed_up_file_is_hash_bound_in_the_manifest(self) -> None:
        state, destination = self.state(), self.destination()
        manifest = backup_state(state, destination)
        self.assertTrue(manifest["files"])
        for entry in manifest["files"]:
            self.assertEqual(len(entry["sha256"]), 64)
        self.assertEqual(verify_backup(destination), ())

    def test_a_tampered_backup_is_reported_rather_than_restored(self) -> None:
        state, destination = self.state(), self.destination()
        backup_state(state, destination)
        (destination / "files" / "workflows.json").write_text("tampered", encoding="utf-8")
        self.assertEqual(verify_backup(destination), ("workflows.json",))
        with self.assertRaises(BackupError):
            restore_state(destination, state)

    def test_a_backup_of_an_absent_file_is_simply_absent_not_an_error(self) -> None:
        state, destination = self.state(), self.destination()
        (state / "workflows.json").unlink()
        manifest = backup_state(state, destination)
        self.assertNotIn("workflows.json", {item["name"] for item in manifest["files"]})


class RestoreTests(StateFixture):
    def test_a_restore_puts_the_work_back_and_leaves_secrets_untouched(self) -> None:
        state, destination = self.state(), self.destination()
        backup_state(state, destination)
        (state / "workflows.json").write_text('{"workflows": ["changed"]}', encoding="utf-8")
        token_before = (state / "google-oauth.main_operator.json").read_text(encoding="utf-8")

        restored = restore_state(destination, state)
        self.assertIn("workflows.json", restored)
        self.assertEqual(
            json.loads((state / "workflows.json").read_text(encoding="utf-8")),
            {"workflows": []},
        )
        # A restore never rewrites, removes, or resurrects credential state.
        self.assertEqual(
            (state / "google-oauth.main_operator.json").read_text(encoding="utf-8"),
            token_before,
        )

    def test_a_restore_refuses_to_write_outside_the_state_directory(self) -> None:
        state, destination = self.state(), self.destination()
        backup_state(state, destination)
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"][0]["name"] = "../escaped.json"
        (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(BackupError):
            restore_state(destination, state)
        self.assertFalse((state.parent / "escaped.json").exists())

    def test_restoring_never_starts_a_consumer_or_replays_a_schedule(self) -> None:
        state, destination = self.state(), self.destination()
        backup_state(state, destination)
        restore_state(destination, state)
        # A restore writes files and nothing else: no marker that would make a
        # second Discord consumer, and no due reminder replayed on the way in.
        self.assertFalse((state / "consumer.lock").exists())
        self.assertFalse((state / "replay.marker").exists())


class RollbackTests(unittest.TestCase):
    """Rollback is a host operation, and the runtime says so rather than guessing.

    Releases are root-owned under /var/lib/scotty, which is outside every mount
    the container has. Code in here that walked a release directory would find
    nothing and report "no accepted release" — which reads like a fact about
    the deployment and is really a fact about what this process can see.
    """

    def test_the_guidance_names_the_host_command_and_never_claims_a_target(self) -> None:
        guidance = rollback_guidance()
        self.assertFalse(guidance["available"])
        self.assertEqual(guidance["operator_command"], ROLLBACK_COMMAND)
        self.assertIn("scotty-supervisor", ROLLBACK_COMMAND)
        self.assertIn("not visible from the runtime", str(guidance["reason"]))

    def test_the_steps_are_the_command_an_operator_actually_has(self) -> None:
        steps = [str(step) for step in guidance_steps()]
        # Not prose about what someone might do: the exact command, including
        # the flag that carries it out, and what its answer means.
        self.assertTrue(any(ROLLBACK_COMMAND in step for step in steps))
        self.assertTrue(any("--execute" in step for step in steps))
        self.assertTrue(any("unknown" in step for step in steps))

    def test_nothing_here_reads_a_release_directory(self) -> None:
        # No argument, so there is no path for a caller to point at and no way
        # for this to report on a directory that happens to exist in a test root.
        import inspect

        self.assertEqual(list(inspect.signature(rollback_guidance).parameters), [])


def guidance_steps() -> list[object]:
    steps = rollback_guidance()["steps"]
    assert isinstance(steps, list)
    return steps


class IncludeListTests(unittest.TestCase):
    def test_the_include_list_and_the_secret_list_never_overlap(self) -> None:
        self.assertFalse(set(BACKUP_INCLUDES) & set(SECRET_NAMES))
        for name in SECRET_NAMES:
            self.assertNotIn(name, BACKUP_INCLUDES)

    def test_the_runtime_and_the_host_supervisor_back_up_the_same_files(self) -> None:
        # Two copies of this list is two chances to drift, and the drift only
        # shows up as a file that quietly stopped being backed up. The host
        # supervisor takes the backups; the runtime describes them.
        self.assertEqual(BACKUP_INCLUDES, host_state.BACKUP_INCLUDES)
        self.assertEqual(SECRET_NAMES, host_state.SECRET_NAMES)

    def test_both_sides_agree_on_what_counts_as_a_secret(self) -> None:
        from assistant.scotty_business.backup import _is_secret

        for name in (*SECRET_NAMES, *BACKUP_INCLUDES, "google-oauth.mikey.json", "session.db"):
            self.assertEqual(_is_secret(name), host_state._is_secret(name), name)


if __name__ == "__main__":
    unittest.main()
