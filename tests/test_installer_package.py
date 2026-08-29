from __future__ import annotations

import ast
import unittest
from pathlib import Path


class InstallerPackageTests(unittest.TestCase):
    def package_files(self) -> set[str]:
        root = Path("assistant/scotty_business")
        return {
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        }

    def test_installer_stages_every_plugin_source_file(self) -> None:
        """The staged list must never drift behind the package it installs."""

        installer = Path("install.sh").read_text(encoding="utf-8")
        start = installer.index("readonly -a PLUGIN_FILES=(")
        staged = {
            line.strip().strip('"')
            for line in installer[start : installer.index(")", start)].splitlines()[1:]
            if line.strip()
        }
        self.assertEqual(staged, self.package_files())

    def test_installer_stages_complete_plugin_and_local_setup_command(self) -> None:
        installer = Path("install.sh").read_text(encoding="utf-8")
        for relative in self.package_files():
            with self.subTest(path=relative):
                self.assertIn(f'"{relative}"', installer)
        self.assertIn("/srv/Scotty/data/plugins/scotty_business", installer)
        self.assertIn("/srv/Scotty/operator/setup-scotty", installer)
        self.assertNotRegex(installer, r"docker compose .*\b(?:up|start|run)\b")

    def test_installer_creates_one_home_per_served_profile(self) -> None:
        installer = Path("install.sh").read_text(encoding="utf-8")
        self.assertIn(
            "readonly -a SERVED_PROFILES=(scotty-maintainer scotty-main-operator scotty-employee)",
            installer,
        )
        self.assertIn(
            "readonly -a CLIENT_PROFILES=(scotty-main-operator scotty-employee)", installer
        )
        self.assertIn('install_profile_dir "${PROFILES_DIR}/${served_profile}"', installer)

    def test_installer_stages_the_bounded_plugin_only_in_client_profile_homes(self) -> None:
        installer = Path("install.sh").read_text(encoding="utf-8")
        self.assertIn(
            'install_plugin_file "$plugin_file" '
            '"${PROFILES_DIR}/${client_profile}/plugins/scotty_business"',
            installer,
        )
        self.assertIn(
            "[[ ! -e ${PROFILES_DIR}/scotty-maintainer/plugins ]]",
            installer,
        )
        self.assertNotIn("${PROFILES_DIR}/scotty-maintainer/plugins/scotty_business", installer)

    def test_setup_wrapper_imports_only_the_installed_package(self) -> None:
        wrapper = Path("setup-scotty").read_text(encoding="utf-8")
        ast.parse(wrapper)
        self.assertIn("/srv/Scotty/data/plugins", wrapper)
        self.assertIn("scotty_business.setup", wrapper)
        self.assertNotIn("subprocess", wrapper)


if __name__ == "__main__":
    unittest.main()
