from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import synthetic
from synthetic import (
    CLIENT_GUILD,
    EMPLOYEE_CHANNEL,
    EMPLOYEE_USER,
    OPERATOR_CHANNEL,
    OPERATOR_USER,
    ROUTE_CHANNEL,
    ROUTE_GUILD,
    ROUTE_USER,
)

from assistant.scotty_guard import register
from assistant.scotty_guard.guard import (
    FIXED_WIZARD_COMMAND,
    SETUP_WIZARD,
    GuardConfig,
    GuardUnavailable,
    MaintainerGuard,
    load_config,
    private_config_paths,
    source_tuple,
)


class RecordingContext:
    def __init__(self) -> None:
        self.hooks: dict[str, object] = {}
        self.calls: list[str] = []

    def register_hook(self, hook_name: str, callback: object) -> None:
        self.calls.append(hook_name)
        self.hooks[hook_name] = callback


def guard(state_dir: Path, sent: list[tuple[str, str]]) -> MaintainerGuard:
    return MaintainerGuard(
        GuardConfig(
            guild_id=ROUTE_GUILD,
            channel_id=ROUTE_CHANNEL,
            user_id=ROUTE_USER,
            operator_channel_id=OPERATOR_CHANNEL,
            state_dir=state_dir,
        ),
        send=lambda channel, text: sent.append((channel, text)),
    )


def event(
    guild: str = ROUTE_GUILD,
    channel: str = ROUTE_CHANNEL,
    user: str = ROUTE_USER,
    text: str = "hello",
    *,
    parent: str | None = None,
    is_bot: bool = False,
    platform: str = "discord",
    message_id: str = "800000000000000001",
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        message_id=message_id,
        source=SimpleNamespace(
            platform=SimpleNamespace(value=platform),
            guild_id=guild,
            scope_id=guild,
            chat_id=channel,
            user_id=user,
            parent_chat_id=parent,
            is_bot=is_bot,
        ),
    )


class GuardRegistrationTests(unittest.TestCase):
    def test_the_guard_registers_one_hook_and_nothing_else(self) -> None:
        context = RecordingContext()
        register(context)
        self.assertEqual(context.calls, ["pre_gateway_dispatch"])
        self.assertEqual(set(context.hooks), {"pre_gateway_dispatch"})

    def test_the_guard_package_exposes_no_tools_or_prompt_sections(self) -> None:
        manifest = Path("assistant/scotty_guard/plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("provides_tools: []", manifest)
        self.assertIn("pre_gateway_dispatch", manifest)
        source = Path("assistant/scotty_guard/__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("register_tool", source)
        self.assertNotIn("register_system_prompt_section", source)

    def test_the_guard_never_imports_the_bounded_client_package(self) -> None:
        for path in sorted(Path("assistant/scotty_guard").rglob("*.py")):
            with self.subTest(path=str(path)):
                self.assertNotIn("scotty_business", path.read_text(encoding="utf-8"))


class GuardAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="scotty-guard-test-")
        self.state = Path(self.tempdir.name)
        self.sent: list[tuple[str, str]] = []
        self.guard = guard(self.state, self.sent)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_the_exact_maintainer_tuple_is_admitted(self) -> None:
        self.assertEqual(self.guard(event()), {"action": "allow"})
        self.assertEqual(self.sent, [])

    def test_a_thread_under_the_configured_parent_is_admitted(self) -> None:
        self.assertEqual(
            self.guard(event(channel="900000000000000001", parent=ROUTE_CHANNEL)),
            {"action": "allow"},
        )

    def test_every_wrong_or_mixed_tuple_is_denied_before_dispatch(self) -> None:
        cases = {
            "unknown sender": event(user="999000000000000001"),
            "operator in the maintainer channel": event(user=OPERATOR_USER),
            "employee in the maintainer channel": event(user=EMPLOYEE_USER),
            "maintainer in the operator channel": event(
                guild=CLIENT_GUILD, channel=OPERATOR_CHANNEL
            ),
            "maintainer in the employee channel": event(
                guild=CLIENT_GUILD, channel=EMPLOYEE_CHANNEL
            ),
            "wrong guild": event(guild=CLIENT_GUILD),
            "wrong channel": event(channel="900000000000000001"),
            "wrong parent thread": event(channel="900000000000000001", parent=OPERATOR_CHANNEL),
            "bot author": event(is_bot=True),
            "wrong platform": event(platform="telegram"),
        }
        for label, candidate in cases.items():
            with self.subTest(case=label):
                self.assertEqual(
                    self.guard(candidate), {"action": "skip", "reason": "unauthorized"}
                )
        self.assertEqual(self.sent, [], "a denial never replies and never discloses")

    def test_a_disagreeing_scope_and_guild_pair_is_denied(self) -> None:
        candidate = event()
        candidate.source.scope_id = CLIENT_GUILD
        self.assertEqual(self.guard(candidate), {"action": "skip", "reason": "unauthorized"})

    def test_unavailable_private_configuration_denies_everything(self) -> None:
        def unavailable() -> GuardConfig:
            raise GuardUnavailable("unavailable")

        blind = MaintainerGuard()
        blind.config = unavailable  # type: ignore[method-assign]
        self.assertEqual(blind(event()), {"action": "skip", "reason": "unauthorized"})
        self.assertEqual(
            blind(event(text=FIXED_WIZARD_COMMAND)),
            {"action": "skip", "reason": "unauthorized"},
        )

    def test_malformed_text_is_skipped(self) -> None:
        candidate = event()
        candidate.text = None
        self.assertEqual(self.guard(candidate), {"action": "skip", "reason": "malformed"})

    def test_source_tuple_uses_the_parent_channel_for_threads_only(self) -> None:
        self.assertEqual(
            source_tuple(event(channel="900", parent=ROUTE_CHANNEL).source),
            (ROUTE_GUILD, ROUTE_CHANNEL, ROUTE_USER),
        )
        self.assertEqual(source_tuple(event().source), (ROUTE_GUILD, ROUTE_CHANNEL, ROUTE_USER))


class GuardWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="scotty-guard-wizard-")
        self.state = Path(self.tempdir.name)
        self.sent: list[tuple[str, str]] = []
        self.guard = guard(self.state, self.sent)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_the_exact_trigger_sends_the_fixed_wizard_only_to_the_operator(self) -> None:
        result = self.guard(event(text=FIXED_WIZARD_COMMAND))
        self.assertEqual(result, {"action": "skip", "reason": "fixed-wizard"})
        self.assertEqual(self.sent, [(OPERATOR_CHANNEL, SETUP_WIZARD)])

    def test_the_trigger_is_handled_before_model_execution(self) -> None:
        self.assertEqual(self.guard(event(text=FIXED_WIZARD_COMMAND))["action"], "skip")

    def test_one_inbound_message_delivers_exactly_once(self) -> None:
        candidate = event(text=FIXED_WIZARD_COMMAND)
        self.guard(candidate)
        self.guard(candidate)
        self.assertEqual(len(self.sent), 1, "two hooks on one message deliver once")

    def test_an_explicit_repeat_delivers_again(self) -> None:
        self.guard(event(text=FIXED_WIZARD_COMMAND, message_id="800000000000000001"))
        self.guard(event(text=FIXED_WIZARD_COMMAND, message_id="800000000000000002"))
        self.assertEqual(len(self.sent), 2)

    def test_wrong_users_and_mixed_tuples_produce_no_wizard(self) -> None:
        for candidate in (
            event(text=FIXED_WIZARD_COMMAND, user=OPERATOR_USER),
            event(text=FIXED_WIZARD_COMMAND, user=EMPLOYEE_USER),
            event(text=FIXED_WIZARD_COMMAND, guild=CLIENT_GUILD, channel=OPERATOR_CHANNEL),
            event(text=FIXED_WIZARD_COMMAND, user="999000000000000001"),
        ):
            with self.subTest(source=candidate.source):
                self.assertEqual(
                    self.guard(candidate), {"action": "skip", "reason": "unauthorized"}
                )
        self.assertEqual(self.sent, [])

    def test_a_near_miss_trigger_is_not_the_trigger(self) -> None:
        for text in (
            "scotty, send trent the setup wizard.",
            "Scotty, send Trent the setup wizard",
            "Please: Scotty, send Trent the setup wizard.",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.guard(event(text=text)), {"action": "allow"})
        self.assertEqual(self.sent, [])

    def test_the_wizard_text_never_requests_a_credential(self) -> None:
        self.assertIn("Never paste credentials here", SETUP_WIZARD)
        self.assertNotIn("token", SETUP_WIZARD.lower())

    def test_nothing_is_delivered_without_an_explicit_trigger(self) -> None:
        self.guard(event(text="good morning"))
        self.assertEqual(self.sent, [])


class GuardConfigTests(unittest.TestCase):
    def test_the_shared_private_configuration_is_read_from_a_profile_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-guard-config-") as directory:
            root = Path(directory)
            scotty = root / "scotty"
            scotty.mkdir()
            (scotty / "private.json").write_text(
                json.dumps(synthetic.private_mapping()), encoding="utf-8"
            )
            config = load_config((scotty / "private.json",))
            self.assertEqual(config.guild_id, ROUTE_GUILD)
            self.assertEqual(config.channel_id, ROUTE_CHANNEL)
            self.assertEqual(config.user_id, ROUTE_USER)
            self.assertEqual(config.operator_channel_id, OPERATOR_CHANNEL)
            self.assertEqual(config.state_dir, scotty)

    def test_an_absent_configuration_fails_closed(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(GuardUnavailable),
        ):
            load_config((Path(directory) / "missing.json",))

    def test_a_symlinked_configuration_is_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "real.json"
            target.write_text(json.dumps(synthetic.private_mapping()), encoding="utf-8")
            link = root / "private.json"
            link.symlink_to(target)
            with self.assertRaises(GuardUnavailable):
                load_config((link,))

    def test_a_configuration_without_a_route_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.json"
            path.write_text(
                json.dumps(synthetic.private_mapping(maintainer_route=None)), encoding="utf-8"
            )
            with self.assertRaises(GuardUnavailable):
                load_config((path,))

    def test_candidate_paths_cover_the_profile_home_and_the_data_mount(self) -> None:
        import os

        saved = {name: os.environ.get(name) for name in ("HERMES_HOME", "SCOTTY_PRIVATE_CONFIG")}
        try:
            os.environ.pop("SCOTTY_PRIVATE_CONFIG", None)
            os.environ["HERMES_HOME"] = "/opt/data/profiles/scotty-maintainer"
            paths = [str(item) for item in private_config_paths()]
            self.assertIn("/opt/data/scotty/private.json", paths)
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
