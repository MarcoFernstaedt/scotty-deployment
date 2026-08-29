from __future__ import annotations

import ast
import unittest
from pathlib import Path


class InstallerPackageTests(unittest.TestCase):
    def test_installer_stages_complete_plugin_and_local_setup_command(self) -> None:
        installer = Path("install.sh").read_text(encoding="utf-8")
        expected = {
            "__init__.py",
            "plugin.yaml",
            "approvals.py",
            "calculations.py",
            "config.py",
            "identity.py",
            "ingress.py",
            "policy.py",
            "reminders.py",
            "runtime.py",
            "service.py",
            "setup.py",
            "adapters/__init__.py",
            "adapters/discord.py",
            "adapters/ghl.py",
            "adapters/http.py",
            "adapters/records.py",
            "adapters/rentcast.py",
            "adapters/trello.py",
        }
        for relative in expected:
            with self.subTest(path=relative):
                self.assertIn(f'"{relative}"', installer)
        self.assertIn("/srv/Scotty/data/plugins/scotty_business", installer)
        self.assertIn("/srv/Scotty/operator/setup-scotty", installer)
        self.assertNotRegex(installer, r"docker compose .*\b(?:up|start|run)\b")

    def test_setup_wrapper_imports_only_the_installed_package(self) -> None:
        wrapper = Path("setup-scotty").read_text(encoding="utf-8")
        ast.parse(wrapper)
        self.assertIn("/srv/Scotty/data/plugins", wrapper)
        self.assertIn("scotty_business.setup", wrapper)
        self.assertNotIn("subprocess", wrapper)


if __name__ == "__main__":
    unittest.main()
