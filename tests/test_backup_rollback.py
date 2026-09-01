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
    SECRET_NAMES,
    BackupError,
    RollbackPlan,
    backup_state,
    restore_state,
    rollback_plan,
    verify_backup,
)


class StateFixture(unittest.TestCase):
    def state(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="scotty-state-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "workflows.json").write_text('{"workflows": []}', encoding="utf-8")
        (root / "personas.json").write_text('{"employee": "Nova"}', encoding="utf-8")
        (root / "reminders.db").write_bytes(b"SQLite format 3\x00reminders")
        (root / "approvals.db").write_bytes(b"SQLite format 3\x00approvals")
        (root / "property-effects.db").write_bytes(b"SQLite format 3\x00effects")
        (root / "budgets.db").write_bytes(b"SQLite format 3\x00budgets")
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


class RollbackTests(StateFixture):
    def plan(self, releases) -> RollbackPlan:
        directory = tempfile.TemporaryDirectory(prefix="scotty-releases-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for name, accepted, digest in releases:
            release = root / name
            release.mkdir()
            (release / "release.json").write_text(
                json.dumps({"accepted": accepted, "image_digest": digest}), encoding="utf-8"
            )
        return rollback_plan(root, current="r3")

    def test_the_target_is_the_last_independently_accepted_release(self) -> None:
        plan = self.plan(
            [
                ("r1", True, "sha256:aaa"),
                ("r2", True, "sha256:bbb"),
                ("r3", False, "sha256:ccc"),
            ]
        )
        self.assertEqual(plan.target, "r2")
        self.assertEqual(plan.image_digest, "sha256:bbb")
        self.assertTrue(plan.available)

    def test_an_unaccepted_release_is_never_a_rollback_target(self) -> None:
        plan = self.plan([("r1", False, "sha256:aaa"), ("r3", False, "sha256:ccc")])
        self.assertFalse(plan.available)
        self.assertIn("no accepted release", plan.reason)

    def test_the_plan_stops_the_current_container_before_starting_another(self) -> None:
        plan = self.plan([("r2", True, "sha256:bbb"), ("r3", False, "sha256:ccc")])
        steps = plan.steps()
        self.assertTrue(steps[0].startswith("stop"))
        self.assertTrue(any(step.startswith("start") for step in steps))
        self.assertLess(
            next(index for index, step in enumerate(steps) if step.startswith("stop")),
            next(index for index, step in enumerate(steps) if step.startswith("start")),
        )

    def test_the_plan_is_a_proposal_and_executes_nothing(self) -> None:
        plan = self.plan([("r2", True, "sha256:bbb"), ("r3", False, "sha256:ccc")])
        self.assertFalse(hasattr(plan, "execute"))
        self.assertFalse(hasattr(plan, "run"))


class IncludeListTests(unittest.TestCase):
    def test_the_include_list_and_the_secret_list_never_overlap(self) -> None:
        self.assertFalse(set(BACKUP_INCLUDES) & set(SECRET_NAMES))
        for name in SECRET_NAMES:
            self.assertNotIn(name, BACKUP_INCLUDES)


if __name__ == "__main__":
    unittest.main()
