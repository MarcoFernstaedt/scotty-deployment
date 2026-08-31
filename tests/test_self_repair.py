from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import synthetic

from assistant.scotty_business.approvals import ApprovalStore
from assistant.scotty_business.policy import Principal, Role
from assistant.scotty_business.reminders import ReminderStore
from assistant.scotty_business.self_repair import (
    OPERATOR_RECOVERY_COMMAND,
    SelfRepairError,
    SelfRepairManager,
)


class ManagerHarness(unittest.TestCase):
    def manager(self, root: Path) -> SelfRepairManager:
        private = root / "private.json"
        private.write_text(json.dumps(synthetic.private_mapping()), encoding="utf-8")
        private.chmod(0o600)
        approvals = ApprovalStore(root / "approvals.db")
        reminders = ReminderStore(root / "reminders.db")
        approvals.initialize()
        reminders.initialize()
        return SelfRepairManager(
            root,
            private,
            approvals,
            reminders,
            provider_status=lambda: {
                "discord": True,
                "trello": False,
                "ghl": False,
                "rentcast": False,
                "google_workspace": False,
            },
        )


class SelfRepairTests(ManagerHarness):
    def test_health_reports_only_scotty_owned_redacted_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-repair-") as directory:
            manager = self.manager(Path(directory))
            health = manager.health()
            self.assertTrue(health["configuration_valid"])
            self.assertEqual(health["approvals_integrity"], "ok")
            self.assertEqual(health["reminders_integrity"], "ok")
            self.assertEqual(health["providers"]["google_workspace"], "not connected")
            rendered = json.dumps(health)
            self.assertNotIn(directory, rendered)
            self.assertNotIn("token", rendered.lower())

    def test_operator_can_recover_owned_workflows_and_rebuild_only_owned_cache(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-repair-") as directory:
            root = Path(directory)
            manager = self.manager(root)
            cache = root / "cache"
            cache.mkdir(mode=0o700)
            (cache / "stale.json").write_text("stale", encoding="utf-8")
            outside = root.parent / f"{root.name}-outside"
            outside.write_text("preserve", encoding="utf-8")
            operator = synthetic.config().principal_for(Role.MAIN_OPERATOR)

            recovered = manager.repair(operator, "recover_workflows")
            rebuilt = manager.repair(operator, "rebuild_cache")

            self.assertEqual(recovered["status"], "repaired")
            self.assertEqual(rebuilt, {"status": "repaired", "component": "cache"})
            self.assertEqual(list(cache.iterdir()), [])
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")
            outside.unlink()

    def test_employee_and_unknown_or_privileged_repair_actions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-repair-") as directory:
            manager = self.manager(Path(directory))
            employee = synthetic.config().principal_for(Role.EMPLOYEE)
            for action in (
                "recover_workflows",
                "run_shell",
                "restart_service",
                "install_package",
                "read_secrets",
                "repair_imperator",
                "repair_vaultwarden",
            ):
                with self.subTest(action=action), self.assertRaises(SelfRepairError):
                    manager.repair(employee, action)

    def test_cache_symlink_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-repair-") as directory:
            root = Path(directory)
            manager = self.manager(root)
            target = root / "target"
            target.mkdir()
            preserved = target / "preserved"
            preserved.write_text("safe", encoding="utf-8")
            (root / "cache").symlink_to(target, target_is_directory=True)
            operator = synthetic.config().principal_for(Role.MAIN_OPERATOR)
            with self.assertRaises(SelfRepairError):
                manager.repair(operator, "rebuild_cache")
            self.assertEqual(preserved.read_text(encoding="utf-8"), "safe")


class AdversarialSelfRepairTests(ManagerHarness):
    """Adversarial cases: containment, idempotency, redaction, fail-closed."""

    def operator(self) -> Principal:
        return synthetic.config().principal_for(Role.MAIN_OPERATOR)

    def test_a_privileged_repair_names_only_the_fixed_root_recovery_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-repair-") as directory:
            manager = self.manager(Path(directory))
            for action in ("restart_service", "repair_docker", "repair_firewall", "rotate_secrets"):
                with self.subTest(action=action):
                    with self.assertRaises(SelfRepairError) as caught:
                        manager.repair(self.operator(), action)
                    message = str(caught.exception)
                    self.assertIn(OPERATOR_RECOVERY_COMMAND, message)
                    self.assertNotIn(directory, message)

    def test_a_malformed_or_non_principal_caller_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-repair-") as directory:
            manager = self.manager(Path(directory))
            operator = self.operator()
            for action in (None, "", 7, ["rebuild_cache"], "rebuild_cache\n"):
                with self.subTest(action=action), self.assertRaises(SelfRepairError):
                    manager.repair(operator, action)
            for caller in (None, "main_operator", object()):
                with self.subTest(caller=caller), self.assertRaises(SelfRepairError):
                    manager.repair(caller, "rebuild_cache")  # type: ignore[arg-type]

    def test_repairs_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-repair-") as directory:
            manager = self.manager(Path(directory))
            operator = self.operator()
            first = manager.repair(operator, "recover_workflows")
            second = manager.repair(operator, "recover_workflows")
            self.assertEqual(first, second)
            self.assertEqual(second["recovered_approvals"], 0)
            self.assertEqual(second["recovered_reminders"], 0)
            self.assertEqual(
                manager.repair(operator, "rebuild_cache"),
                manager.repair(operator, "rebuild_cache"),
            )

    def test_interrupted_work_becomes_unknown_and_is_never_retried(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-repair-") as directory:
            root = Path(directory)
            manager = self.manager(root)
            with sqlite3.connect(root / "reminders.db") as connection:
                connection.execute(
                    """INSERT INTO reminders (reminder_id, principal_json, channel_id, text,
                       due_at, status, version, attempt_nonce, receipt_json, created_at, updated_at)
                       VALUES ('r-1', ?, ?, 'synthetic', ?, 'dispatching', 1, 'nonce', NULL, ?, ?)""",
                    (
                        json.dumps(
                            list(synthetic.config().principal_for(Role.MAIN_OPERATOR).as_tuple())
                        ),
                        synthetic.OPERATOR_CHANNEL,
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
            self.assertEqual(manager.health()["interrupted_workflows"], 1)
            result = manager.repair(self.operator(), "recover_workflows")
            self.assertEqual(result["recovered_reminders"], 1)
            with sqlite3.connect(root / "reminders.db") as connection:
                status = connection.execute(
                    "SELECT status FROM reminders WHERE reminder_id='r-1'"
                ).fetchone()[0]
            self.assertEqual(status, "unknown")
            self.assertEqual(manager.health()["interrupted_workflows"], 0)

    def test_rebuild_cache_clears_nested_state_and_keeps_owner_only_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-repair-") as directory:
            root = Path(directory)
            manager = self.manager(root)
            cache = root / "cache"
            nested = cache / "provider" / "trello"
            nested.mkdir(mode=0o700, parents=True)
            (nested / "page-1.json").write_text("stale", encoding="utf-8")
            outside = root / "keep.json"
            outside.write_text("preserve", encoding="utf-8")
            (cache / "escape").symlink_to(outside)

            self.assertEqual(
                manager.repair(self.operator(), "rebuild_cache"),
                {"status": "repaired", "component": "cache"},
            )
            self.assertEqual(list(cache.iterdir()), [])
            self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    def test_rebuild_cache_refuses_a_cache_path_that_is_a_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-repair-") as directory:
            root = Path(directory)
            manager = self.manager(root)
            (root / "cache").write_text("not a directory", encoding="utf-8")
            with self.assertRaises(SelfRepairError):
                manager.repair(self.operator(), "rebuild_cache")
            self.assertEqual((root / "cache").read_text(encoding="utf-8"), "not a directory")

    def test_state_permission_repair_restores_owner_only_modes_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-repair-") as directory:
            root = Path(directory)
            manager = self.manager(root)
            (root / "private.json").chmod(0o644)
            (root / "approvals.db").chmod(0o666)
            neighbour = root / "neighbour.txt"
            neighbour.write_text("untouched", encoding="utf-8")
            neighbour.chmod(0o644)

            result = manager.repair(self.operator(), "repair_state_permissions")

            self.assertEqual(result["status"], "repaired")
            self.assertEqual(result["corrected"], 2)
            self.assertEqual((root / "private.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "approvals.db").stat().st_mode & 0o777, 0o600)
            self.assertEqual(neighbour.stat().st_mode & 0o777, 0o644)
            self.assertEqual(
                manager.repair(self.operator(), "repair_state_permissions")["corrected"], 0
            )

    def test_health_reports_invalid_configuration_without_echoing_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-repair-") as directory:
            root = Path(directory)
            manager = self.manager(root)
            mapping = synthetic.private_mapping()
            mapping["ghl"] = {"location_id": "synthetic-location-should-not-leak"}
            del mapping["principals"]["employee"]
            (root / "private.json").write_text(json.dumps(mapping), encoding="utf-8")

            health = manager.health()

            self.assertFalse(health["configuration_valid"])
            rendered = json.dumps(health)
            self.assertNotIn("synthetic-location-should-not-leak", rendered)
            self.assertNotIn(directory, rendered)

    def test_health_reports_a_damaged_owned_database_without_repairing_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-repair-") as directory:
            root = Path(directory)
            manager = self.manager(root)
            (root / "approvals.db").write_bytes(b"not a database")
            (root / "reminders.db").unlink()

            health = manager.health()

            self.assertIn(health["approvals_integrity"], {"failed", "unreadable"})
            self.assertEqual(health["reminders_integrity"], "missing")
            self.assertNotIn(directory, json.dumps(health))

    def test_the_maintainer_may_repair_and_no_repair_reads_a_credential(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-repair-") as directory:
            manager = self.manager(Path(directory))
            maintainer = Principal(
                "110000000000000001", "220000000000000001", "320000000000000001", Role.MAINTAINER
            )
            self.assertEqual(manager.repair(maintainer, "rebuild_cache")["status"], "repaired")
        source = Path("assistant/scotty_business/self_repair.py").read_text(encoding="utf-8")
        for forbidden in ("subprocess", "os.system", "shutil.rmtree", "eval(", "exec(", "environ"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
