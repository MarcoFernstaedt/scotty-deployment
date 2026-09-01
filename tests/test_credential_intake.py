from __future__ import annotations

import contextlib
import unittest
from types import SimpleNamespace

import synthetic

from assistant.scotty_business.credential_intake import (
    INTAKE_COMMANDS,
    CredentialIntake,
    IntakeStatus,
)
from assistant.scotty_business.policy import Role
from assistant.scotty_business.routing import resolve_route

SECRET = "synthetic-provider-key-000000"  # noqa: S105 - synthetic fixture, not a credential


class FakeBroker:
    """Privilege-separated broker stand-in with fixed operations only."""

    def __init__(self, *, valid: bool = True, commits: bool = True, boundary: bool = True) -> None:
        self.valid = valid
        self.boundary = boundary
        self.commits = commits
        self.validated: list[tuple[str, str]] = []
        self.committed: list[tuple[str, str]] = []
        self.seen_lengths: list[int] = []

    def available(self) -> bool:
        return self.boundary

    def validate(self, provider: str, credential_class: str, material: str) -> bool:
        self.validated.append((provider, credential_class))
        self.seen_lengths.append(len(material))
        return self.valid

    def commit(self, provider: str, credential_class: str, material: str) -> bool:
        self.committed.append((provider, credential_class))
        return self.commits


class FakeDeleter:
    def __init__(self, *, confirms: bool = True, raises: bool = False) -> None:
        self.confirms = confirms
        self.raises = raises
        self.deleted: list[tuple[str, str]] = []

    def delete_message(self, channel_id: str, message_id: str) -> bool:
        if self.raises:
            raise RuntimeError("platform delete is unavailable")
        self.deleted.append((channel_id, message_id))
        return self.confirms


def event(
    text: str,
    *,
    guild: str = synthetic.CLIENT_GUILD,
    channel: str = synthetic.OPERATOR_CHANNEL,
    user: str = synthetic.OPERATOR_USER,
    message_id: str | None = "900000000000000001",
) -> SimpleNamespace:
    source = synthetic.source(guild, channel, user)
    return SimpleNamespace(text=text, source=source, message_id=message_id)


@contextlib.contextmanager
def attested_boundary():
    """Exercise the retained mechanism as if a boundary had been attested.

    The shipped default is off. These tests keep the mechanism honest so that
    enabling it later is a one-line change backed by evidence, not a rewrite.
    """

    from assistant.scotty_business import credential_intake

    original = credential_intake.DISCORD_INTAKE_ENABLED
    credential_intake.DISCORD_INTAKE_ENABLED = True
    try:
        yield
    finally:
        credential_intake.DISCORD_INTAKE_ENABLED = original


class IntakeHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._boundary = attested_boundary()
        self._boundary.__enter__()
        self.addCleanup(lambda: self._boundary.__exit__(None, None, None))

    def build(self, **kwargs):
        config = synthetic.config()
        sent: list[tuple[str, str]] = []
        broker = kwargs.pop("broker", None) or FakeBroker()
        deleter = kwargs.pop("deleter", None) or FakeDeleter()
        clock = kwargs.pop("clock", None) or (lambda: 1_000)
        intake = CredentialIntake(
            config,
            lambda channel, text: sent.append((channel, text)),
            broker=broker,
            deleter=deleter,
            clock=clock,
        )
        return intake, config, sent, broker, deleter

    def open_window(self, intake, config, phrase="Scotty, accept my Trello API key."):
        opening = event(phrase)
        route = resolve_route(config, opening.source)
        assert route is not None
        return intake.open_window(route, phrase), route


class IntakeWindowTests(IntakeHarness):
    def test_only_the_exact_operator_tuple_and_phrase_opens_a_window(self) -> None:
        intake, config, sent, _, _ = self.build()
        opened, _ = self.open_window(intake, config)
        self.assertTrue(opened)
        self.assertEqual(len(sent), 1)
        self.assertIn("next message", sent[0][1].lower())
        self.assertNotIn(SECRET, sent[0][1])

    def test_an_employee_or_unknown_phrase_never_opens_a_window(self) -> None:
        intake, config, _, _, _ = self.build()
        employee = event(
            "Scotty, accept my Trello API key.",
            channel=synthetic.EMPLOYEE_CHANNEL,
            user=synthetic.EMPLOYEE_USER,
        )
        route = resolve_route(config, employee.source)
        assert route is not None
        self.assertEqual(route.principal.role, Role.EMPLOYEE)
        self.assertFalse(intake.open_window(route, "Scotty, accept my Trello API key."))

        operator = event("x")
        operator_route = resolve_route(config, operator.source)
        assert operator_route is not None
        for phrase in (
            "Scotty, accept my key.",
            "scotty, accept my trello api key.",
            "Scotty, accept my Google Workspace password.",
            "",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(intake.open_window(operator_route, phrase))

    def test_a_second_window_never_replaces_an_open_one(self) -> None:
        intake, config, _, _, _ = self.build()
        self.open_window(intake, config)
        opened, _ = self.open_window(intake, config, "Scotty, accept my RentCast API key.")
        self.assertFalse(opened)

    def test_every_intake_phrase_names_a_provider_and_credential_class(self) -> None:
        for phrase, (provider, credential_class) in INTAKE_COMMANDS.items():
            with self.subTest(phrase=phrase):
                self.assertTrue(phrase.startswith("Scotty, accept my "))
                self.assertIn(provider, ("trello", "ghl", "rentcast"))
                self.assertTrue(credential_class)
        self.assertNotIn("google_workspace", {provider for provider, _ in INTAKE_COMMANDS.values()})


class PrivilegeBoundaryTests(IntakeHarness):
    def test_no_window_opens_when_the_privilege_boundary_is_not_installed(self) -> None:
        intake, config, sent, broker, _ = self.build(broker=FakeBroker(boundary=False))
        opened, route = self.open_window(intake, config)
        self.assertFalse(opened)
        self.assertIn("local", sent[0][1].lower())
        self.assertIn("never paste", sent[0][1].lower())
        self.assertIsNone(intake.intercept(event(SECRET), route))
        self.assertEqual(broker.validated, [])

    def test_a_missing_broker_socket_reports_unavailable_and_calls_nothing(self) -> None:
        import tempfile
        from pathlib import Path

        from assistant.scotty_business.credential_intake import BROKER_SOCKET, UnixSocketBroker

        self.assertTrue(BROKER_SOCKET.startswith("/run/"))
        with tempfile.TemporaryDirectory(prefix="scotty-broker-") as directory:
            missing = UnixSocketBroker(Path(directory) / "absent.sock")
            self.assertFalse(missing.available())
            self.assertFalse(missing.validate("trello", "api_key", SECRET))
            self.assertFalse(missing.commit("trello", "api_key", SECRET))

            not_a_socket = Path(directory) / "regular"
            not_a_socket.write_text("", encoding="utf-8")
            self.assertFalse(UnixSocketBroker(not_a_socket).available())

            link = Path(directory) / "link.sock"
            link.symlink_to(not_a_socket)
            self.assertFalse(UnixSocketBroker(link).available())


class ConfirmedDeletionTests(unittest.TestCase):
    """Deletion counts only when the platform reports the message really gone."""

    def adapter(self, statuses):
        from assistant.scotty_business.adapters.discord import DiscordAdapter
        from assistant.scotty_business.adapters.http import HttpResponse

        calls: list[tuple[str, str]] = []

        class Transport:
            def request(self, method, url, *, headers=None, query=None, json_body=None):
                calls.append((method, url))
                return HttpResponse(statuses[(method, len(calls))], {}, None)

        return (
            DiscordAdapter(Transport(), "synthetic-bot-token", (synthetic.OPERATOR_CHANNEL,)),
            calls,
        )

    def test_deletion_is_confirmed_only_by_an_absent_readback(self) -> None:
        adapter, calls = self.adapter({("DELETE", 1): 204, ("GET", 2): 404})
        self.assertTrue(adapter.delete_message(synthetic.OPERATOR_CHANNEL, "900000000000000001"))
        self.assertEqual([method for method, _ in calls], ["DELETE", "GET"])

    def test_an_unconfirmed_or_refused_deletion_is_false(self) -> None:
        for statuses in (
            {("DELETE", 1): 204, ("GET", 2): 200},
            {("DELETE", 1): 403},
            {("DELETE", 1): 500},
            # An already-absent message is not evidence that the operator's
            # message was the one removed.
            {("DELETE", 1): 404},
        ):
            with self.subTest(statuses=statuses):
                adapter, _ = self.adapter(statuses)
                self.assertFalse(
                    adapter.delete_message(synthetic.OPERATOR_CHANNEL, "900000000000000001")
                )

    def test_deletion_is_refused_outside_the_configured_destinations(self) -> None:
        from assistant.scotty_business.adapters.http import ProviderError

        adapter, _ = self.adapter({("DELETE", 1): 204})
        with self.assertRaises(ProviderError):
            adapter.delete_message(synthetic.ROUTE_CHANNEL, "900000000000000001")


class IntakeInterceptTests(IntakeHarness):
    def test_a_credential_is_deleted_then_committed_and_never_returned(self) -> None:
        intake, config, sent, broker, deleter = self.build()
        _, route = self.open_window(intake, config)

        outcome = intake.intercept(event(SECRET), route)

        assert outcome is not None
        self.assertEqual(outcome.status, IntakeStatus.STORED)
        self.assertEqual(outcome.provider, "trello")
        self.assertEqual(broker.validated, [("trello", "api_key")])
        self.assertEqual(deleter.deleted, [(synthetic.OPERATOR_CHANNEL, "900000000000000001")])
        self.assertEqual(broker.committed, [("trello", "api_key")])
        rendered = repr(outcome) + "".join(text for _, text in sent)
        self.assertNotIn(SECRET, rendered)
        self.assertIn("credential present", rendered.lower())

    def test_deletion_precedes_the_commit(self) -> None:
        order: list[str] = []

        class OrderedBroker(FakeBroker):
            def commit(self, provider: str, credential_class: str, material: str) -> bool:
                order.append("commit")
                return super().commit(provider, credential_class, material)

        class OrderedDeleter(FakeDeleter):
            def delete_message(self, channel_id: str, message_id: str) -> bool:
                order.append("delete")
                return super().delete_message(channel_id, message_id)

        intake, config, _, _, _ = self.build(broker=OrderedBroker(), deleter=OrderedDeleter())
        _, route = self.open_window(intake, config)
        intake.intercept(event(SECRET), route)
        self.assertEqual(order, ["delete", "commit"])

    def test_unconfirmed_deletion_fails_closed_without_committing(self) -> None:
        for deleter in (FakeDeleter(confirms=False), FakeDeleter(raises=True)):
            with self.subTest(deleter=type(deleter).__name__):
                intake, config, sent, broker, _ = self.build(deleter=deleter)
                _, route = self.open_window(intake, config)
                outcome = intake.intercept(event(SECRET), route)
                assert outcome is not None
                self.assertEqual(outcome.status, IntakeStatus.DELETE_UNAVAILABLE)
                self.assertEqual(broker.committed, [])
                guidance = "".join(text for _, text in sent).lower()
                self.assertIn("local", guidance)
                self.assertIn("rotate", guidance)

    def test_a_failed_validation_still_deletes_but_never_commits(self) -> None:
        intake, config, _, broker, deleter = self.build(broker=FakeBroker(valid=False))
        _, route = self.open_window(intake, config)
        outcome = intake.intercept(event(SECRET), route)
        assert outcome is not None
        self.assertEqual(outcome.status, IntakeStatus.VALIDATION_FAILED)
        self.assertEqual(len(deleter.deleted), 1)
        self.assertEqual(broker.committed, [])

    def test_a_failed_commit_reports_failure_without_partial_persistence(self) -> None:
        intake, config, _, broker, deleter = self.build(broker=FakeBroker(commits=False))
        _, route = self.open_window(intake, config)
        outcome = intake.intercept(event(SECRET), route)
        assert outcome is not None
        self.assertEqual(outcome.status, IntakeStatus.COMMIT_FAILED)
        self.assertEqual(len(deleter.deleted), 1)

    def test_a_message_without_an_exact_source_identity_fails_closed(self) -> None:
        intake, config, _, broker, deleter = self.build()
        _, route = self.open_window(intake, config)
        outcome = intake.intercept(event(SECRET, message_id=None), route)
        assert outcome is not None
        self.assertEqual(outcome.status, IntakeStatus.DELETE_UNAVAILABLE)
        self.assertEqual(deleter.deleted, [])
        self.assertEqual(broker.committed, [])

    def test_a_generic_event_id_is_never_mistaken_for_a_message_id(self) -> None:
        intake, config, _, broker, deleter = self.build()
        _, route = self.open_window(intake, config)
        carrier = event(SECRET, message_id=None)
        # A session or event identifier is not a message identifier; deleting by
        # it would leave the credential in the channel.
        carrier.id = "700000000000000009"
        carrier.source.id = "700000000000000009"
        outcome = intake.intercept(carrier, route)
        assert outcome is not None
        self.assertEqual(outcome.status, IntakeStatus.DELETE_UNAVAILABLE)
        self.assertEqual(deleter.deleted, [])
        self.assertEqual(broker.committed, [])

    def test_a_window_is_single_use_so_a_replay_is_never_accepted(self) -> None:
        intake, config, _, broker, _ = self.build()
        _, route = self.open_window(intake, config)
        self.assertIsNotNone(intake.intercept(event(SECRET), route))
        self.assertIsNone(intake.intercept(event(SECRET), route))
        self.assertEqual(len(broker.committed), 1)

    def test_an_expired_window_is_consumed_without_any_broker_call(self) -> None:
        now = [1_000]
        intake, config, sent, broker, deleter = self.build(clock=lambda: now[0])
        _, route = self.open_window(intake, config)
        now[0] = 1_000_000
        outcome = intake.intercept(event(SECRET), route)
        assert outcome is not None
        self.assertEqual(outcome.status, IntakeStatus.EXPIRED)
        self.assertEqual(broker.validated, [])
        self.assertEqual(broker.committed, [])
        self.assertEqual(deleter.deleted, [(synthetic.OPERATOR_CHANNEL, "900000000000000001")])
        self.assertIsNone(intake.intercept(event(SECRET), route))

    def test_another_tuple_never_consumes_or_satisfies_an_open_window(self) -> None:
        intake, config, _, broker, _ = self.build()
        _, route = self.open_window(intake, config)
        for guild, channel, user in (
            (synthetic.CLIENT_GUILD, synthetic.EMPLOYEE_CHANNEL, synthetic.EMPLOYEE_USER),
            (synthetic.ROUTE_GUILD, synthetic.ROUTE_CHANNEL, synthetic.ROUTE_USER),
        ):
            other = event(SECRET, guild=guild, channel=channel, user=user)
            other_route = resolve_route(config, other.source)
            with self.subTest(channel=channel):
                self.assertIsNone(intake.intercept(other, other_route))
        self.assertEqual(broker.validated, [])
        self.assertIsNotNone(intake.intercept(event(SECRET), route))

    def test_a_malformed_credential_is_refused_and_the_window_consumed(self) -> None:
        for text in ("", "   ", "x", "a b c", "y" * 5_000, "line\nline"):
            with self.subTest(text=text):
                intake, config, _, broker, deleter = self.build()
                _, route = self.open_window(intake, config)
                outcome = intake.intercept(event(text), route)
                assert outcome is not None
                self.assertEqual(outcome.status, IntakeStatus.MALFORMED)
                self.assertEqual(broker.validated, [])
                self.assertEqual(broker.committed, [])
                self.assertEqual(len(deleter.deleted), 1)
                self.assertIsNone(intake.intercept(event(SECRET), route))

    def test_no_intercept_happens_when_no_window_is_open(self) -> None:
        intake, config, _, broker, deleter = self.build()
        ordinary = event("Show me today's calendar")
        route = resolve_route(config, ordinary.source)
        self.assertIsNone(intake.intercept(ordinary, route))
        self.assertEqual(broker.validated, [])
        self.assertEqual(deleter.deleted, [])

    def test_the_module_never_logs_or_renders_credential_material(self) -> None:
        from pathlib import Path

        source = Path("assistant/scotty_business/credential_intake.py").read_text(encoding="utf-8")
        for forbidden in ("print(", "logger", "logging", "repr(material", "str(material"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        # Credential material may only ever be passed to the broker's two fixed
        # operations; it must never reach an outbound message or a format string.
        for line in source.splitlines():
            if "material" not in line or line.lstrip().startswith("#"):
                continue
            with self.subTest(line=line.strip()):
                self.assertNotIn("enqueue(", line)
                self.assertNotIn('f"', line)
                self.assertNotIn("%", line)

    def test_no_outbound_text_can_ever_carry_the_credential(self) -> None:
        intake, config, sent, _, _ = self.build()
        _, route = self.open_window(intake, config)
        intake.intercept(event(SECRET), route)
        for _, text in sent:
            self.assertNotIn(SECRET, text)
            self.assertNotIn(SECRET[:12], text)


class IngressWiringTests(IntakeHarness):
    """The intake must run inside the pre-dispatch hook, ahead of everything."""

    def guard(self, intake, config, sent):
        from assistant.scotty_business.ingress import IngressGuard

        return IngressGuard(
            config, lambda channel, text: sent.append((channel, text)), intake=intake
        )

    def test_the_opening_phrase_is_handled_before_the_model_ever_sees_it(self) -> None:
        intake, config, sent, _, _ = self.build()
        guard = self.guard(intake, config, sent)
        decision = guard(event("Scotty, accept my Trello API key."))
        self.assertEqual(decision, {"action": "skip", "reason": "credential-intake-open"})
        self.assertTrue(intake.has_open_window())

    def test_the_expected_credential_is_consumed_before_the_leak_scan(self) -> None:
        intake, config, sent, broker, deleter = self.build()
        guard = self.guard(intake, config, sent)
        guard(event("Scotty, accept my Trello API key."))

        # Text that the ordinary leak scan would also match must be resolved by
        # the intake, not treated as an accidental paste.
        decision = guard(event("token=synthetic-provider-key-000000"))

        self.assertEqual(decision, {"action": "skip", "reason": "credential-intake"})
        self.assertEqual(len(deleter.deleted), 1)
        for _, text in sent:
            self.assertNotIn("synthetic-provider-key-000000", text)

    def test_an_ordinary_message_still_reaches_the_model_when_no_window_is_open(self) -> None:
        intake, config, sent, broker, deleter = self.build()
        guard = self.guard(intake, config, sent)
        self.assertEqual(guard(event("Show me today's calendar")), {"action": "allow"})
        self.assertEqual(broker.validated, [])
        self.assertEqual(deleter.deleted, [])

    def test_an_unexpected_credential_paste_is_still_refused_and_rotated(self) -> None:
        intake, config, sent, broker, deleter = self.build()
        guard = self.guard(intake, config, sent)
        decision = guard(event("my api key is synthetic-provider-key-000000"))
        self.assertEqual(decision, {"action": "skip", "reason": "credential-redacted"})
        self.assertIn("rotate", sent[-1][1].lower())
        self.assertEqual(broker.committed, [])

    def test_an_employee_intake_phrase_opens_nothing_and_reaches_no_model(self) -> None:
        intake, config, sent, broker, _ = self.build()
        guard = self.guard(intake, config, sent)
        decision = guard(
            event(
                "Scotty, accept my Trello API key.",
                channel=synthetic.EMPLOYEE_CHANNEL,
                user=synthetic.EMPLOYEE_USER,
            )
        )
        self.assertEqual(decision, {"action": "skip", "reason": "credential-intake-open"})
        self.assertFalse(intake.has_open_window())
        self.assertEqual(broker.validated, [])


class DiscordIntakeIsDisabledTests(unittest.TestCase):
    """The shipped default: Discord never accepts a credential at all."""

    def guard(self, sent):
        from assistant.scotty_business.credential_intake import (
            BROKER_SOCKET,
            CredentialIntake,
            UnixSocketBroker,
        )
        from assistant.scotty_business.ingress import IngressGuard

        config = synthetic.config()
        intake = CredentialIntake(
            config,
            lambda channel, text: sent.append((channel, text)),
            broker=UnixSocketBroker(BROKER_SOCKET),
            deleter=FakeDeleter(),
            clock=lambda: 1_000,
        )
        return (
            IngressGuard(config, lambda channel, text: sent.append((channel, text)), intake=intake),
            intake,
        )

    def test_the_shipped_default_is_off(self) -> None:
        from assistant.scotty_business import credential_intake

        self.assertFalse(credential_intake.DISCORD_INTAKE_ENABLED)

    def test_an_intake_phrase_opens_nothing_and_names_the_local_path(self) -> None:
        sent: list[tuple[str, str]] = []
        guard, intake = self.guard(sent)
        decision = guard(event("Scotty, accept my Trello API key."))
        self.assertEqual(decision, {"action": "skip", "reason": "credential-intake-open"})
        self.assertFalse(intake.has_open_window())
        message = sent[-1][1].lower()
        self.assertIn("local hidden-input setup command", message)
        self.assertIn("rotate", message)

    def test_a_credential_shaped_message_never_reaches_model_dispatch(self) -> None:
        sent: list[tuple[str, str]] = []
        guard, _ = self.guard(sent)
        for text in (
            "my api key is synthetic-provider-key-000000",
            "token=synthetic-provider-key-000000",
            "the secret: synthetic-provider-key-000000",
        ):
            with self.subTest(text=text[:20]):
                decision = guard(event(text))
                self.assertEqual(decision, {"action": "skip", "reason": "credential-redacted"})
        rotation = "".join(text for _, text in sent).lower()
        self.assertIn("rotate", rotation)

    def test_an_opaque_token_is_never_captured_by_a_window(self) -> None:
        sent: list[tuple[str, str]] = []
        guard, intake = self.guard(sent)
        guard(event("Scotty, accept my Trello API key."))
        # With intake off there is no window, so the next message is ordinary
        # traffic rather than a captured credential.
        decision = guard(event(SECRET))
        self.assertNotEqual(decision.get("reason"), "credential-intake")
        self.assertFalse(intake.has_open_window())

    def test_no_window_can_be_opened_on_any_route(self) -> None:
        from assistant.scotty_business.routing import resolve_route

        sent: list[tuple[str, str]] = []
        _, intake = self.guard(sent)
        config = synthetic.config()
        for phrase in INTAKE_COMMANDS:
            opening = event(phrase)
            route = resolve_route(config, opening.source)
            with self.subTest(phrase=phrase):
                self.assertFalse(intake.open_window(route, phrase))
                self.assertIsNone(intake.intercept(opening, route))

    def test_the_switch_is_one_literal_constant_nothing_can_compute(self) -> None:
        import ast
        from pathlib import Path

        tree = ast.parse(
            Path("assistant/scotty_business/credential_intake.py").read_text(encoding="utf-8")
        )
        assignments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "DISCORD_INTAKE_ENABLED"
                for target in node.targets
            )
        ]
        self.assertEqual(len(assignments), 1)
        value = assignments[0].value
        self.assertIsInstance(value, ast.Constant)
        self.assertIs(value.value, False)


if __name__ == "__main__":
    unittest.main()
