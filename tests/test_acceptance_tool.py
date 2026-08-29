from __future__ import annotations

import contextlib
import importlib.util
import io
import unittest
from pathlib import Path


def load_tool(name: str):
    path = Path("tools") / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"scotty_tools_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SyntheticAcceptanceTests(unittest.TestCase):
    def test_the_acceptance_run_passes_without_credentials_or_live_calls(self) -> None:
        module = load_tool("synthetic_acceptance")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(module.main(), 0)
        output = buffer.getvalue()
        self.assertIn("synthetic acceptance: PASS", output)
        self.assertGreaterEqual(len(module.CHECKS), 70)

    def test_the_acceptance_run_reads_only_synthetic_fixtures(self) -> None:
        source = Path("tools/synthetic_acceptance.py").read_text(encoding="utf-8")
        for forbidden in ("/srv/Scotty", "https://", "urlopen"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_a_broken_check_fails_the_run_rather_than_reporting_pass(self) -> None:
        module = load_tool("synthetic_acceptance")
        with self.assertRaises(module.AcceptanceFailure):
            module.check("deliberately false", False)


class PinnedSmokeTests(unittest.TestCase):
    def test_the_smoke_checks_the_native_routing_contract_in_the_real_runtime(self) -> None:
        source = Path("tools/pinned_smoke.py").read_text(encoding="utf-8")
        self.assertIn("multiplex_profiles", source)
        self.assertIn("profile_routes", source)
        self.assertIn("exactly three native profile routes", source)
        self.assertIn('["chat_id", "guild_id", "name", "platform", "profile"]', source)
        self.assertIn("the full profile enables only the guard", source)
        self.assertIn("client profile is bounded", source)


class OAuthProbeTests(unittest.TestCase):
    def test_the_probe_targets_the_pinned_digest_and_never_logs_in(self) -> None:
        source = Path("tools/pinned_oauth_probe.py").read_text(encoding="utf-8")
        self.assertIn(
            "sha256:d64f4e9aba92884fff3d5020c02a75676066f237622d0776759ca1437b9b0517", source
        )
        self.assertIn("--network", source)
        self.assertIn("none", source)
        self.assertIn("--help", source)
        self.assertIn("openai-codex", source)
        for forbidden in ("--rm -it", "password"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
