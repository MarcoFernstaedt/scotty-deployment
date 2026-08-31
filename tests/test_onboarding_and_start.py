from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from assistant.scotty_business.policy import SETUP_WIZARD
from assistant.scotty_business.setup import SetupError, collect_inputs_from_prefill, load_prefill


class TrentWizardTests(unittest.TestCase):
    def test_fixed_wizard_covers_nonsecret_choices_for_every_release_provider(self) -> None:
        for phrase in ("Discord", "Trello", "Google Workspace", "GoHighLevel", "RentCast"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, SETUP_WIZARD)
        self.assertIn("preferences", SETUP_WIZARD.lower())
        self.assertIn("local", SETUP_WIZARD.lower())
        self.assertIn("browser consent", SETUP_WIZARD.lower())

    def test_wizard_never_solicits_or_accepts_credentials_in_discord(self) -> None:
        lowered = SETUP_WIZARD.lower()
        for forbidden in ("paste your token", "send your key", "reply with your password"):
            self.assertNotIn(forbidden, lowered)
        self.assertIn("never paste", lowered)
        self.assertIn("cannot accept", lowered)


class PrivatePrefillTests(unittest.TestCase):
    def test_complete_prefill_avoids_reentering_known_ids_and_uses_hidden_secrets(self) -> None:
        prefill = {
            "model_provider": "openai-codex",
            "model_name": "synthetic/codex",
            "guild_id": "100000000000000001",
            "operator_channel_id": "201000000000000001",
            "operator_user_id": "301000000000000001",
            "employee_channel_id": "202000000000000001",
            "employee_user_id": "302000000000000001",
            "announcement_channel_ids": [],
            "route_guild_id": "110000000000000001",
            "route_channel_id": "220000000000000001",
            "route_user_id": "320000000000000001",
        }
        prompts: list[str] = []
        result = collect_inputs_from_prefill(
            prefill,
            hidden_fn=lambda prompt: prompts.append(prompt)
            or ("synthetic-discord-secret" if "Discord" in prompt else ""),
            environ={},
        )
        self.assertEqual(result.guild_id, prefill["guild_id"])
        self.assertEqual(set(result.secrets), {"DISCORD_BOT_TOKEN"})
        self.assertTrue(all("ID" not in prompt for prompt in prompts))

    def test_owner_only_prefill_loads_nonsecret_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-prefill-") as directory:
            path = Path(directory) / "prefill.json"
            path.write_text(
                json.dumps(
                    {
                        "guild_id": "100000000000000001",
                        "operator_user_id": "301000000000000001",
                        "google_workspace": {
                            "account_email": "scotty.synthetic@example.invalid",
                            "calendar_ids": ["calendar.synthetic@example.invalid"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            path.chmod(0o600)
            loaded = load_prefill(path, owner_uid=os.getuid())
            self.assertEqual(loaded["guild_id"], "100000000000000001")

    def test_prefill_rejects_group_readable_files_and_any_secret_shaped_key(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-prefill-") as directory:
            path = Path(directory) / "prefill.json"
            path.write_text('{"guild_id":"1"}', encoding="utf-8")
            path.chmod(0o640)
            with self.assertRaises(SetupError):
                load_prefill(path, owner_uid=os.getuid())
            path.write_text('{"discord_bot_token":"forbidden"}', encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaises(SetupError):
                load_prefill(path, owner_uid=os.getuid())

    def test_prefill_rejects_unknown_fields_instead_of_silently_widening_setup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-prefill-") as directory:
            path = Path(directory) / "prefill.json"
            path.write_text('{"arbitrary_provider":"forbidden"}', encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaises(SetupError):
                load_prefill(path, owner_uid=os.getuid())


class StartCommandContractTests(unittest.TestCase):
    def run_start(self, root: Path, docker_script: str, *, setup_rc: int = 0):
        """Run scotty-start against a fake docker and setup command."""

        log = root / "calls"
        fake = root / "docker"
        fake.write_text(docker_script, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        setup = root / "setup"
        setup.write_text(f"#!/bin/sh\nexit {setup_rc}\n", encoding="utf-8")
        setup.chmod(setup.stat().st_mode | stat.S_IXUSR)
        result = subprocess.run(
            ["/bin/bash", "scotty-start"],
            cwd=Path.cwd(),
            env={
                "PATH": f"{root}:/usr/bin:/bin",
                "SCOTTY_TEST_LOG": str(log),
                "SCOTTY_SETUP_COMMAND": str(setup),
                "SCOTTY_TEST_EUID": "0",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        return result, log

    def test_an_absent_container_is_diagnosed_not_only_recovered(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-start-test-") as directory:
            root = Path(directory)
            result, log = self.run_start(
                root,
                "#!/bin/sh\n"
                'printf \'%s\\n\' "$*" >>"$SCOTTY_TEST_LOG"\n'
                'case "$1 $2" in\n'
                "  'inspect --format') printf 'No such object\\n' >&2; exit 1 ;;\n"
                "esac\n"
                "exit 0\n",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("scotty-start:", result.stderr)
            self.assertIn("stopped", result.stderr)
            self.assertNotIn("start scotty", log.read_text(encoding="utf-8"))
            recovery = [line for line in result.stderr.splitlines() if line.startswith("Recovery:")]
            self.assertEqual(len(recovery), 1)

    def test_a_failing_local_setup_never_starts_the_container(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-start-test-") as directory:
            root = Path(directory)
            result, log = self.run_start(
                root,
                "#!/bin/sh\n"
                'printf \'%s\\n\' "$*" >>"$SCOTTY_TEST_LOG"\n'
                'case "$1 $2" in\n'
                "  'inspect --format') printf 'false\\n' ;;\n"
                "esac\n"
                "exit 0\n",
                setup_rc=17,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("start scotty", log.read_text(encoding="utf-8"))

    def test_root_operator_command_has_fail_closed_stopped_recovery(self) -> None:
        source = Path("scotty-start").read_text(encoding="utf-8")
        self.assertIn("EUID", source)
        self.assertIn("/srv/Scotty/operator/setup-scotty", source)
        self.assertIn("hermes auth add openai-codex", source)
        self.assertIn("google-oauth.json", source)
        self.assertIn("docker stop", source)
        self.assertIn("docker start", source)
        self.assertNotIn("docker run", source)
        self.assertNotIn("docker compose up", source)
        self.assertNotIn("DISCORD_BOT_TOKEN", source)
        # Consent is validated inside the prepared container against the exact
        # configured account and scope set, with no Google call and no send.
        self.assertIn("store.ready(scope.oauth_scopes, scope.account_email)", source)
        # The consent check runs inside the container, so it must use the
        # container's own mount path rather than the host path.
        check = source.split("GOOGLE_CONSENT_CHECK='", 1)[1].split("'", 1)[0]
        self.assertIn("/opt/data/plugins", check)
        self.assertNotIn("/srv/Scotty", check)
        self.assertNotIn("send_message", source)
        self.assertNotIn("SETUP_WIZARD", source)

    def test_partial_failure_stops_the_prepared_container_and_prints_one_recovery_line(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-start-test-") as directory:
            root = Path(directory)
            log = root / "calls"
            fake = root / "docker"
            fake.write_text(
                "#!/bin/sh\n"
                'printf \'%s\\n\' "$*" >>"$SCOTTY_TEST_LOG"\n'
                'case "$1 $2" in\n'
                "  'inspect --format') printf 'false\\n' ;;\n"
                "  'start scotty') exit 0 ;;\n"
                "  'exec -it') exit 23 ;;\n"
                "  'stop scotty') exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            setup = root / "setup"
            setup.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            setup.chmod(setup.stat().st_mode | stat.S_IXUSR)
            result = subprocess.run(
                ["/bin/bash", "scotty-start"],
                cwd=Path.cwd(),
                env={
                    "PATH": f"{root}:/usr/bin:/bin",
                    "SCOTTY_TEST_LOG": str(log),
                    "SCOTTY_SETUP_COMMAND": str(setup),
                    "SCOTTY_TEST_EUID": "0",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stop scotty", log.read_text(encoding="utf-8"))
            recovery = [line for line in result.stderr.splitlines() if line.startswith("Recovery:")]
            self.assertEqual(len(recovery), 1)


if __name__ == "__main__":
    unittest.main()
