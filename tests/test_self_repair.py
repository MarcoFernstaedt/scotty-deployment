from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import synthetic

from assistant.scotty_business.approvals import ApprovalStore
from assistant.scotty_business.policy import Role
from assistant.scotty_business.reminders import ReminderStore
from assistant.scotty_business.self_repair import SelfRepairError, SelfRepairManager


class SelfRepairTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
