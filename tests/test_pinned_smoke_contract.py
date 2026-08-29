"""Exercise everything in the pinned smoke that does not need the Hermes image.

The smoke itself runs inside the pinned container. This module stages the same
synthetic deployment on the host and drives the same enforcement paths, so a
staging or logic mistake fails here rather than only on the deployment host.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


def load_smoke():
    spec = importlib.util.spec_from_file_location("scotty_pinned_smoke", "tools/pinned_smoke.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SMOKE = load_smoke()
IDS = SMOKE.SYNTHETIC_INPUTS
UNKNOWN_USER = "999000000000000001"
WIZARD = "Scotty, send Trent the setup wizard."


def source(
    guild: str,
    chat: str,
    user: str,
    *,
    parent: str | None = None,
    is_bot: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        platform=SimpleNamespace(value="discord"),
        guild_id=guild,
        scope_id=guild,
        chat_id=chat,
        user_id=user,
        parent_chat_id=parent,
        is_bot=is_bot,
    )


def event(text: str, src: SimpleNamespace, message_id: str = "800000000000000001"):
    return SimpleNamespace(text=text, message_id=message_id, source=src)


class ProbeSourceTests(unittest.TestCase):
    def test_the_probe_body_is_valid_python(self) -> None:
        ast.parse(SMOKE.PROBE)

    def test_the_probe_uses_synthetic_identifiers_only(self) -> None:
        for value in (
            IDS.guild_id,
            IDS.operator_channel_id,
            IDS.employee_channel_id,
            IDS.route_guild_id,
            IDS.route_channel_id,
            IDS.route_user_id,
        ):
            with self.subTest(value=value):
                self.assertTrue(value.isdigit())
                self.assertTrue(17 <= len(value) <= 20)
        self.assertEqual(set(IDS.secrets), {"DISCORD_BOT_TOKEN"})

    def test_the_smoke_makes_no_network_call_of_its_own(self) -> None:
        source_text = Path("tools/pinned_smoke.py").read_text(encoding="utf-8")
        self.assertIn('"--network",\n        "none",', source_text)
        self.assertNotIn("urlopen", source_text)
        self.assertNotIn("https://discord.com", source_text)


class SenderAuthorizationContractTests(unittest.TestCase):
    """The probe must drive the real pinned method with a real SessionSource."""

    def test_the_probe_builds_a_real_session_source(self) -> None:
        self.assertIn("gateway.session", SMOKE.PROBE)
        self.assertIn("SessionSource", SMOKE.PROBE)
        self.assertIn("def build_session_source(", SMOKE.PROBE)
        self.assertIn("SessionSource(**kwargs)", SMOKE.PROBE)
        self.assertIn("inspect.signature(SessionSource)", SMOKE.PROBE)

    def test_the_probe_never_passes_a_bare_user_string_to_the_pinned_method(self) -> None:
        """The original defect: `method(instance, user)` with a string user ID."""

        self.assertNotIn("method(instance, user)", SMOKE.PROBE)
        self.assertIn("method(instance, session_source", SMOKE.PROBE)

    def test_the_probe_resolves_the_runtime_platform_rather_than_a_stand_in(self) -> None:
        self.assertIn("def resolve_platform(", SMOKE.PROBE)
        self.assertIn("the runtime Discord platform value could not be resolved", SMOKE.PROBE)

    def test_the_probe_asserts_admission_and_denial_through_the_pinned_method(self) -> None:
        self.assertIn("the pinned runtime admits each configured sender", SMOKE.PROBE)
        self.assertIn("the pinned runtime denies an unknown sender", SMOKE.PROBE)
        self.assertIn("_is_user_authorized", SMOKE.PROBE)

    def test_the_probe_fails_loudly_when_the_pinned_method_cannot_be_driven(self) -> None:
        self.assertIn("the pinned authorization method could not be driven", SMOKE.PROBE)

    def test_the_probe_checks_the_method_takes_a_source_parameter(self) -> None:
        self.assertIn(
            "the pinned authorization method takes a SessionSource, not a user string",
            SMOKE.PROBE,
        )


class LifecycleDispatchContractTests(unittest.TestCase):
    """The hook must be invoked through the pinned lifecycle, with no fallback."""

    def test_the_probe_dispatches_through_the_pinned_lifecycle(self) -> None:
        self.assertIn("def lifecycle_dispatch(", SMOKE.PROBE)
        self.assertIn('"pre_gateway_dispatch"', SMOKE.PROBE)
        self.assertIn("discover_plugins(force=True)", SMOKE.PROBE)
        self.assertIn("get_plugin_manager()", SMOKE.PROBE)

    def test_the_probe_never_constructs_the_hook_object_directly(self) -> None:
        """A direct construction proves the class, not the registration."""

        for forbidden in (
            "IngressGuard(",
            "from scotty_business.ingress import",
            "MaintainerGuard(",
            "_load_private_config",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, SMOKE.PROBE)

    def test_the_probe_has_no_manager_hooks_attribute_assumption(self) -> None:
        self.assertNotIn("manager.hooks", SMOKE.PROBE)
        self.assertNotIn('getattr(manager, "hooks"', SMOKE.PROBE)

    def test_missing_lifecycle_dispatch_is_a_failure_not_a_fallback(self) -> None:
        self.assertIn("no pinned lifecycle dispatch entry point could be driven", SMOKE.PROBE)
        self.assertIn("raise RuntimeError(", SMOKE.PROBE)

    def test_a_dispatch_that_does_not_reach_the_hook_is_a_failure(self) -> None:
        self.assertIn("so the registered hook did not decide", SMOKE.PROBE)
        self.assertIn("def hook_decision(", SMOKE.PROBE)

    def test_asynchronous_dispatch_is_driven_rather_than_bypassed(self) -> None:
        self.assertIn("inspect.isawaitable", SMOKE.PROBE)
        self.assertIn("asyncio.run", SMOKE.PROBE)

    def test_the_registered_hook_is_asserted_for_admit_and_deny(self) -> None:
        self.assertIn("registered pre_gateway_dispatch admits", SMOKE.PROBE)
        self.assertIn("registered pre_gateway_dispatch denies", SMOKE.PROBE)
        self.assertIn("before model execution", SMOKE.PROBE)

    def test_the_wizard_is_proven_through_lifecycle_dispatch_only(self) -> None:
        self.assertIn("the wizard trigger is intercepted before model execution", SMOKE.PROBE)
        self.assertIn("the fixed wizard goes only to the main-operator channel", SMOKE.PROBE)
        self.assertIn("one inbound message delivers the wizard exactly once", SMOKE.PROBE)
        self.assertIn("no wrong sender can trigger the wizard", SMOKE.PROBE)
        # Delivery is observed by replacing the module-level sender, never by
        # constructing the guard, and the decision comes from dispatch.
        self.assertIn("guard_module.send_fixed_message", SMOKE.PROBE)
        self.assertIn("dispatch_decision(", SMOKE.PROBE)

    def test_both_profile_homes_are_probed(self) -> None:
        self.assertEqual((SMOKE.ROLE_ROOT, SMOKE.ROLE_MAINTAINER), ("root", "maintainer"))
        self.assertIn('if ROLE == "root":', SMOKE.PROBE)
        self.assertIn('elif ROLE == "maintainer":', SMOKE.PROBE)
        self.assertIn("the maintainer guard is loaded in its own profile home", SMOKE.PROBE)
        self.assertIn("the full profile exposes no bounded Scotty tool", SMOKE.PROBE)


class EnvironmentHygieneTests(unittest.TestCase):
    def test_the_probe_rejects_an_inherited_allowlist_or_open_policy(self) -> None:
        self.assertIn("no open sender policy is inherited or set", SMOKE.PROBE)
        self.assertIn("the container carries no unexpected Discord environment", SMOKE.PROBE)
        self.assertIn("the probe uses its staged profile home", SMOKE.PROBE)

    def test_the_host_discord_environment_is_stripped_before_docker_runs(self) -> None:
        source_text = Path("tools/pinned_smoke.py").read_text(encoding="utf-8")
        self.assertIn('if name.startswith("DISCORD_"):', source_text)
        self.assertIn("environment.pop(name)", source_text)

    def test_the_container_environment_is_supplied_explicitly(self) -> None:
        command = SMOKE._command(
            Path("/tmp/scotty-smoke"), SMOKE.ROLE_ROOT, "/opt/data", "1,2,3", "{}"
        )
        joined = " ".join(command)
        self.assertIn("DISCORD_ALLOWED_USERS=1,2,3", joined)
        self.assertIn("SCOTTY_SMOKE_ROLE=root", joined)
        self.assertIn("HERMES_HOME=/opt/data", joined)
        self.assertIn("--network none", joined)
        self.assertIn(SMOKE.IMAGE, command)

    def test_both_roles_run_against_their_own_profile_home(self) -> None:
        source_text = Path("tools/pinned_smoke.py").read_text(encoding="utf-8")
        self.assertIn('(ROLE_ROOT, "/opt/data")', source_text)
        self.assertIn("(ROLE_MAINTAINER, maintainer_home)", source_text)

    def test_the_identifier_payload_carries_both_homes(self) -> None:
        identifiers = json.loads(SMOKE._identifiers())
        self.assertEqual(identifiers["home"]["root"], "/opt/data")
        self.assertEqual(identifiers["home"]["maintainer"], "/opt/data/profiles/scotty-maintainer")
        self.assertEqual(identifiers["wizard_command"], WIZARD)
        self.assertEqual(len(identifiers["expected_tools"]), 5)


class StagedDeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="scotty-smoke-stage-")
        self.home = Path(self.tempdir.name)
        SMOKE._stage(self.home)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_each_profile_home_carries_only_its_own_plugin(self) -> None:
        maintainer = self.home / "profiles" / "scotty-maintainer" / "plugins"
        self.assertTrue((maintainer / "scotty_guard" / "plugin.yaml").is_file())
        self.assertFalse((maintainer / "scotty_business").exists())
        for profile in ("scotty-main-operator", "scotty-employee"):
            with self.subTest(profile=profile):
                plugins = self.home / "profiles" / profile / "plugins"
                self.assertTrue((plugins / "scotty_business" / "plugin.yaml").is_file())
                self.assertFalse((plugins / "scotty_guard").exists())

    def test_no_bytecode_is_staged(self) -> None:
        self.assertEqual(list(self.home.rglob("__pycache__")), [])
        self.assertEqual(list(self.home.rglob("*.pyc")), [])

    def test_the_private_configuration_is_staged_for_the_guard(self) -> None:
        private = json.loads((self.home / "scotty" / "private.json").read_text(encoding="utf-8"))
        self.assertEqual(private["maintainer_route"]["user_id"], IDS.route_user_id)
        self.assertEqual(
            private["principals"]["main_operator"]["channel_id"], IDS.operator_channel_id
        )

    def test_every_profile_home_config_repeats_the_selected_model(self) -> None:
        for profile in ("scotty-maintainer", "scotty-main-operator", "scotty-employee"):
            with self.subTest(profile=profile):
                rendered = (self.home / "profiles" / profile / "config.yaml").read_text(
                    encoding="utf-8"
                )
                self.assertIn(f'provider: "{IDS.model_provider}"', rendered)
                self.assertIn(f'default: "{IDS.model_name}"', rendered)


class StagedGuardEnforcementTests(unittest.TestCase):
    """Drive the staged guard exactly as the in-container probe does."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="scotty-smoke-guard-")
        self.home = Path(self.tempdir.name)
        SMOKE._stage(self.home)
        plugins = str(self.home / "profiles" / "scotty-maintainer" / "plugins")
        sys.path.insert(0, plugins)
        self._plugins_path = plugins
        for name in list(sys.modules):
            if name.startswith("scotty_guard"):
                del sys.modules[name]
        import scotty_guard.guard as guard_module

        self.guard_module = guard_module
        self.sent: list[tuple[str, str]] = []
        guard_module.send_fixed_message = lambda channel, text: self.sent.append((channel, text))
        self.guard = guard_module.MaintainerGuard(
            guard_module.load_config((self.home / "scotty" / "private.json",))
        )

    def tearDown(self) -> None:
        if self._plugins_path in sys.path:
            sys.path.remove(self._plugins_path)
        for name in list(sys.modules):
            if name.startswith("scotty_guard"):
                del sys.modules[name]
        self.tempdir.cleanup()

    def test_the_staged_guard_admits_only_the_exact_maintainer_tuple(self) -> None:
        self.assertEqual(
            self.guard(
                event("status", source(IDS.route_guild_id, IDS.route_channel_id, IDS.route_user_id))
            ),
            {"action": "allow"},
        )

    def test_the_staged_guard_denies_every_other_sender_and_tuple(self) -> None:
        denials = {
            "unknown sender": source(IDS.route_guild_id, IDS.route_channel_id, UNKNOWN_USER),
            "operator in the maintainer channel": source(
                IDS.route_guild_id, IDS.route_channel_id, IDS.operator_user_id
            ),
            "employee in the maintainer channel": source(
                IDS.route_guild_id, IDS.route_channel_id, IDS.employee_user_id
            ),
            "maintainer in the operator channel": source(
                IDS.guild_id, IDS.operator_channel_id, IDS.route_user_id
            ),
            "wrong guild": source(IDS.guild_id, IDS.route_channel_id, IDS.route_user_id),
            "wrong parent thread": source(
                IDS.route_guild_id,
                "900000000000000001",
                IDS.route_user_id,
                parent=IDS.operator_channel_id,
            ),
            "bot author": source(
                IDS.route_guild_id, IDS.route_channel_id, IDS.route_user_id, is_bot=True
            ),
        }
        for label, candidate in denials.items():
            with self.subTest(case=label):
                self.assertEqual(
                    self.guard(event("status", candidate)),
                    {"action": "skip", "reason": "unauthorized"},
                )
        self.assertEqual(self.sent, [])

    def test_the_staged_guard_delivers_the_wizard_once_to_the_operator(self) -> None:
        trigger = event(
            WIZARD,
            source(IDS.route_guild_id, IDS.route_channel_id, IDS.route_user_id),
            "800000000000000009",
        )
        self.assertEqual(self.guard(trigger), {"action": "skip", "reason": "fixed-wizard"})
        self.assertEqual(self.sent, [(IDS.operator_channel_id, self.guard_module.SETUP_WIZARD)])
        self.guard(trigger)
        self.assertEqual(len(self.sent), 1)

    def test_no_wrong_sender_can_trigger_the_staged_wizard(self) -> None:
        for user in (IDS.operator_user_id, IDS.employee_user_id, UNKNOWN_USER):
            with self.subTest(user=user):
                self.guard(event(WIZARD, source(IDS.route_guild_id, IDS.route_channel_id, user)))
        self.assertEqual(self.sent, [])


class StagedRootHookEnforcementTests(unittest.TestCase):
    """Drive the staged bounded plugin's pre-dispatch gate over the full matrix."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="scotty-smoke-root-")
        self.home = Path(self.tempdir.name)
        SMOKE._stage(self.home)
        from assistant.scotty_business.config import RuntimeConfig
        from assistant.scotty_business.ingress import IngressGuard

        raw = json.loads((self.home / "scotty" / "private.json").read_text(encoding="utf-8"))
        self.delivered: list[tuple[str, str]] = []
        self.guard = IngressGuard(
            RuntimeConfig.from_mapping(raw),
            lambda channel, text: self.delivered.append((channel, text)),
            self.home / "scotty",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_each_exact_tuple_is_admitted(self) -> None:
        admitted = {
            "operator": source(IDS.guild_id, IDS.operator_channel_id, IDS.operator_user_id),
            "employee": source(IDS.guild_id, IDS.employee_channel_id, IDS.employee_user_id),
            "maintainer": source(IDS.route_guild_id, IDS.route_channel_id, IDS.route_user_id),
        }
        for label, candidate in admitted.items():
            with self.subTest(case=label):
                self.assertEqual(self.guard(event("status", candidate)), {"action": "allow"})

    def test_every_mixed_tuple_is_denied_before_model_execution(self) -> None:
        denied = {
            "unknown sender": source(IDS.guild_id, IDS.operator_channel_id, UNKNOWN_USER),
            "maintainer in the operator channel": source(
                IDS.guild_id, IDS.operator_channel_id, IDS.route_user_id
            ),
            "maintainer in the employee channel": source(
                IDS.guild_id, IDS.employee_channel_id, IDS.route_user_id
            ),
            "operator in the maintainer channel": source(
                IDS.route_guild_id, IDS.route_channel_id, IDS.operator_user_id
            ),
            "employee in the maintainer channel": source(
                IDS.route_guild_id, IDS.route_channel_id, IDS.employee_user_id
            ),
            "operator in the employee channel": source(
                IDS.guild_id, IDS.employee_channel_id, IDS.operator_user_id
            ),
            "employee in the operator channel": source(
                IDS.guild_id, IDS.operator_channel_id, IDS.employee_user_id
            ),
            "wrong guild": source(
                "999000000000000002", IDS.operator_channel_id, IDS.operator_user_id
            ),
            "wrong channel": source(IDS.guild_id, "900000000000000001", IDS.operator_user_id),
            "wrong parent thread": source(
                IDS.guild_id,
                "900000000000000001",
                IDS.operator_user_id,
                parent=IDS.employee_channel_id,
            ),
            "bot author": source(
                IDS.guild_id, IDS.operator_channel_id, IDS.operator_user_id, is_bot=True
            ),
        }
        for label, candidate in denied.items():
            with self.subTest(case=label):
                self.assertEqual(
                    self.guard(event("status", candidate)),
                    {"action": "skip", "reason": "unauthorized"},
                )
        self.assertEqual(self.delivered, [], "a denial never replies")

    def test_valid_senders_need_no_separate_pairing_step(self) -> None:
        """Admission comes from configuration alone; nothing is paired at runtime."""

        for candidate in (
            source(IDS.guild_id, IDS.operator_channel_id, IDS.operator_user_id),
            source(IDS.guild_id, IDS.employee_channel_id, IDS.employee_user_id),
            source(IDS.route_guild_id, IDS.route_channel_id, IDS.route_user_id),
        ):
            with self.subTest(user=candidate.user_id):
                self.assertEqual(self.guard(event("status", candidate)), {"action": "allow"})
        self.assertEqual(list(self.home.rglob("pairing*")), [])

    def test_the_root_hook_also_routes_the_wizard_only_to_the_operator(self) -> None:
        self.guard(
            event(
                WIZARD,
                source(IDS.route_guild_id, IDS.route_channel_id, IDS.route_user_id),
                "800000000000000011",
            )
        )
        self.assertEqual([channel for channel, _ in self.delivered], [IDS.operator_channel_id])


if __name__ == "__main__":
    unittest.main()
