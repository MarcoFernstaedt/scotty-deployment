"""Who the broker thinks you are, and what that lets you do.

The defect this replaces was simple to state and hard to see: the broker read
the actor out of the request. Any process running as the runtime account could
say `"actor": "employee"` and act as Mikey — including a compromised plugin, a
maintainer shell, or anything else that happened to share that uid. A boundary
whose authority comes from the message it is protecting is not a boundary.

Authority now comes from the kernel twice over. Each actor has its own socket,
owned by that actor's group and reachable by nobody else, and the actor is
whichever socket the connection arrived on. A request that names an actor is
refused outright rather than ignored, because a caller that can ask and be
quietly overruled will eventually be trusted by somebody.

The rest is what the privileged side must prove before it will spend a
credential: an explicit grant for a shared identity, and for anything with a
consequence, an approval bound to this exact actor, operation, payload and
deadline, usable once.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from assistant.scotty_broker.broker import (
    ACTOR_SOCKETS,
    ACTORS,
    Broker,
    BrokerError,
    CredentialStore,
    Peer,
)
from assistant.scotty_broker.effects import EffectLedger
from assistant.scotty_broker.executor import ExecutionError, Executor
from assistant.scotty_broker.grants import Grant, GrantStore

MOMENT = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)


class Recorder:
    """Stands in for the network. Records what would have gone out."""

    def __init__(self, status: int = 200, body: object | None = None) -> None:
        self.status = status
        self.body = {"id": "provider-1"} if body is None else body
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.fail_with: Exception | None = None

    def send(self, method, url, *, headers, body, timeout):
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append((method, url, dict(body or {})))
        return self.status, self.body


class BrokerFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="scotty-authority-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.moment = MOMENT
        self.store = CredentialStore(self.root / "credentials.json")
        self.grants = GrantStore(self.root / "grants.json", clock=lambda: self.moment)
        self.effects = EffectLedger(self.root / "effects.db", clock=lambda: self.moment)
        self.effects.initialize()
        self.recorder = Recorder()
        self.executor = Executor(self.store, self.grants, send=self.recorder.send)
        self.broker = Broker(
            self.store,
            executor=self.executor,
            grants=self.grants,
            effects=self.effects,
            clock=lambda: self.moment,
        )

    def peer(self, actor: str) -> Peer:
        """The kernel's view of a process running as that actor's account."""

        from assistant.scotty_broker.broker import ACTOR_UIDS

        uid = ACTOR_UIDS[actor]
        return Peer(pid=4321, uid=uid, gid=uid)

    def root_peer(self) -> Peer:
        return Peer(pid=1, uid=0, gid=0)

    def hold(self, provider: str, credential_class: str, actor: str) -> None:
        self.store.put(provider, credential_class, "synthetic-material-value", actor)

    def send_sms(self, **overrides) -> dict[str, object]:
        request: dict[str, object] = {
            "op": "execute",
            "operation": "ghl.send_sms",
            "arguments": {
                "type": "SMS",
                "contactId": "contact-1",
                "message": "synthetic",
                "toNumber": "+15555550123",
            },
        }
        request.update(overrides)
        return request


class ActorBindingTests(BrokerFixture):
    def test_the_actor_is_the_socket_not_the_request(self) -> None:
        # Every actor has exactly one socket, and every socket exactly one
        # actor. That mapping is the whole authority model.
        self.assertEqual(set(ACTOR_SOCKETS), set(ACTORS) - {"shared"})
        self.assertEqual(len(set(ACTOR_SOCKETS.values())), len(ACTOR_SOCKETS))

    def test_a_request_that_names_an_actor_is_refused_outright(self) -> None:
        self.hold("trello", "api_key", "main_operator")
        self.hold("trello", "token", "main_operator")
        for named in ("employee", "main_operator", "shared"):
            with self.subTest(named=named), self.assertRaises(BrokerError) as caught:
                self.broker.handle(
                    self.peer("main_operator"),
                    {
                        "op": "status",
                        "provider": "trello",
                        "credential_class": "api_key",
                        "actor": named,
                    },
                    actor="main_operator",
                )
            # Refused, not ignored: a caller that can ask and be silently
            # overruled is a caller somebody will eventually trust.
            self.assertIn("actor", str(caught.exception))

    def test_a_peer_from_the_wrong_account_cannot_use_an_actor_socket(self) -> None:
        self.hold("trello", "api_key", "employee")
        wrong = Peer(pid=99, uid=10001, gid=10001)  # the main operator's account
        with self.assertRaises(BrokerError):
            self.broker.handle(
                wrong,
                {"op": "status", "provider": "trello", "credential_class": "token"},
                actor="employee",
            )

    def test_the_old_shared_runtime_account_is_nobody(self) -> None:
        # 10000 was the single account every profile ran as. Nothing may reach
        # an actor socket as that uid any more.
        stale = Peer(pid=7, uid=10000, gid=10000)
        for actor in sorted(set(ACTORS) - {"shared"}):
            with self.subTest(actor=actor), self.assertRaises(BrokerError):
                self.broker.handle(
                    stale,
                    {"op": "status", "provider": "trello", "credential_class": "token"},
                    actor=actor,
                )

    def test_one_actor_never_sees_another_s_credential_state(self) -> None:
        self.hold("trello", "api_key", "employee")
        reply = self.broker.handle(
            self.peer("main_operator"),
            {"op": "status", "provider": "trello", "credential_class": "token"},
            actor="main_operator",
        )
        self.assertFalse(reply["ok"])

    def test_the_maintainer_cannot_act_as_a_client(self) -> None:
        self.hold("ghl", "private_token", "main_operator")
        with self.assertRaises((BrokerError, ExecutionError)):
            self.broker.handle(
                self.peer("maintainer"),
                self.send_sms(),
                actor="maintainer",
            )


class SharedGrantTests(BrokerFixture):
    def test_a_shared_credential_is_not_usable_without_a_grant(self) -> None:
        self.hold("trello", "api_key", "shared")
        self.hold("trello", "token", "shared")
        reply = self.broker.handle(
            self.peer("employee"),
            {"op": "status", "provider": "trello", "credential_class": "token"},
            actor="employee",
        )
        # A shared credential existing is not permission to use it.
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["state"], "not authorized")

    def test_a_grant_makes_exactly_what_it_names_usable(self) -> None:
        self.hold("trello", "api_key", "shared")
        self.hold("trello", "token", "shared")
        self.grants.put(
            Grant(
                actor="employee",
                provider="trello",
                operations=("trello.list_board_cards",),
                resources=("board-1",),
                expires_at=MOMENT + timedelta(days=30),
            )
        )
        reply = self.broker.handle(
            self.peer("employee"),
            {"op": "status", "provider": "trello", "credential_class": "token"},
            actor="employee",
        )
        self.assertTrue(reply["ok"])

    def test_a_grant_for_another_actor_authorizes_nothing(self) -> None:
        self.hold("trello", "api_key", "shared")
        self.hold("trello", "token", "shared")
        self.grants.put(
            Grant(
                actor="main_operator",
                provider="trello",
                operations=("trello.list_board_cards",),
                resources=("board-1",),
                expires_at=MOMENT + timedelta(days=30),
            )
        )
        reply = self.broker.handle(
            self.peer("employee"),
            {"op": "status", "provider": "trello", "credential_class": "token"},
            actor="employee",
        )
        self.assertFalse(reply["ok"])

    def test_an_expired_or_revoked_grant_authorizes_nothing(self) -> None:
        self.hold("trello", "api_key", "shared")
        self.hold("trello", "token", "shared")
        self.grants.put(
            Grant(
                actor="employee",
                provider="trello",
                operations=("trello.list_board_cards",),
                resources=("board-1",),
                expires_at=MOMENT + timedelta(minutes=5),
            )
        )
        self.moment = MOMENT + timedelta(hours=1)
        self.assertIsNone(self.grants.find("employee", "trello", "trello.list_board_cards"))
        self.moment = MOMENT
        self.grants.revoke("employee", "trello")
        self.assertIsNone(self.grants.find("employee", "trello", "trello.list_board_cards"))

    def test_a_grant_does_not_cover_an_operation_it_never_named(self) -> None:
        self.hold("ghl", "private_token", "shared")
        self.grants.put(
            Grant(
                actor="employee",
                provider="ghl",
                operations=("ghl.read_contact",),
                resources=("contact-1",),
                expires_at=MOMENT + timedelta(days=30),
            )
        )
        self.assertIsNone(self.grants.find("employee", "ghl", "ghl.send_sms"))

    def test_grants_are_root_owned_and_never_written_by_a_request(self) -> None:
        # There is no wire operation that writes a grant from an actor socket.
        from assistant.scotty_broker.broker import ACTOR_OPERATIONS, ROOT_OPERATIONS

        self.assertIn("grant", ROOT_OPERATIONS)
        self.assertNotIn("grant", ACTOR_OPERATIONS)
        with self.assertRaises(BrokerError):
            self.broker.handle(
                self.peer("employee"),
                {"op": "grant", "provider": "trello", "operations": ["trello.list_board_cards"]},
                actor="employee",
            )


class ConsequenceApprovalTests(BrokerFixture):
    def setUp(self) -> None:
        super().setUp()
        self.hold("ghl", "private_token", "main_operator")

    def approved(self, **overrides) -> str:
        """One approval, minted by root, for the exact request below."""

        request = self.send_sms()
        arguments = request["arguments"]
        assert isinstance(arguments, dict)
        fields: dict[str, object] = {
            "actor": "main_operator",
            "operation": "ghl.send_sms",
            "payload_hash": self.effects.payload_hash(arguments),
            "resource": "contact-1",
            "expires_at": (self.moment + timedelta(minutes=10)).isoformat(),
        }
        fields.update(overrides)
        reply = self.broker.handle(self.root_peer(), {"op": "approve", **fields})
        return str(reply["approval_id"])

    def execute(self, approval_id: str, **overrides) -> dict[str, object]:
        request = self.send_sms(
            approval_id=approval_id,
            idempotency_key="idem-1",
            deadline=(self.moment + timedelta(minutes=5)).isoformat(),
        )
        request.update(overrides)
        return self.broker.handle(self.peer("main_operator"), request, actor="main_operator")

    def test_a_consequence_without_an_approval_is_refused_before_any_call(self) -> None:
        with self.assertRaises((BrokerError, ExecutionError)):
            self.broker.handle(self.peer("main_operator"), self.send_sms(), actor="main_operator")
        self.assertEqual(self.recorder.calls, [])

    def test_an_approved_consequence_runs_once_and_is_recorded_verified(self) -> None:
        reply = self.execute(self.approved())
        self.assertTrue(reply["ok"])
        self.assertEqual(len(self.recorder.calls), 1)
        effect = self.effects.get(str(reply["effect_id"]))
        self.assertEqual(effect.state, "verified")
        self.assertEqual(effect.actor, "main_operator")

    def test_the_same_approval_cannot_be_replayed(self) -> None:
        approval = self.approved()
        self.execute(approval)
        with self.assertRaises(BrokerError):
            self.execute(approval, idempotency_key="idem-2")
        self.assertEqual(len(self.recorder.calls), 1)

    def test_the_same_idempotency_key_never_sends_twice(self) -> None:
        first = self.execute(self.approved())
        second = self.execute(self.approved())
        # The second is answered from the record rather than sent again.
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertEqual(second["effect_id"], first["effect_id"])

    def test_an_approval_for_another_actor_is_refused(self) -> None:
        approval = self.approved(actor="employee")
        with self.assertRaises(BrokerError):
            self.execute(approval)
        self.assertEqual(self.recorder.calls, [])

    def test_an_approval_for_another_operation_is_refused(self) -> None:
        approval = self.approved(operation="ghl.read_contact")
        with self.assertRaises(BrokerError):
            self.execute(approval)
        self.assertEqual(self.recorder.calls, [])

    def test_an_approval_for_a_different_payload_is_refused(self) -> None:
        approval = self.approved(payload_hash="0" * 64)
        with self.assertRaises(BrokerError):
            self.execute(approval)
        self.assertEqual(self.recorder.calls, [])

    def test_an_expired_approval_is_refused(self) -> None:
        approval = self.approved()
        self.moment = MOMENT + timedelta(hours=1)
        with self.assertRaises(BrokerError):
            self.execute(approval)
        self.assertEqual(self.recorder.calls, [])

    def test_a_passed_deadline_is_refused_before_the_call(self) -> None:
        approval = self.approved()
        with self.assertRaises(BrokerError):
            self.execute(approval, deadline=(self.moment - timedelta(minutes=1)).isoformat())
        self.assertEqual(self.recorder.calls, [])

    def test_only_root_may_mint_an_approval(self) -> None:
        with self.assertRaises(BrokerError):
            self.broker.handle(
                self.peer("main_operator"),
                {
                    "op": "approve",
                    "actor": "main_operator",
                    "operation": "ghl.send_sms",
                    "payload_hash": "a" * 64,
                    "resource": "contact-1",
                    "expires_at": (self.moment + timedelta(minutes=10)).isoformat(),
                },
                actor="main_operator",
            )

    def test_a_transport_failure_is_unknown_and_never_retried_silently(self) -> None:
        self.recorder.fail_with = ExecutionError("provider outcome unknown")
        approval = self.approved()
        with self.assertRaises(ExecutionError):
            self.execute(approval)
        recorded = self.effects.by_idempotency("main_operator", "idem-1")
        assert recorded is not None
        self.assertEqual(recorded.state, "unknown")
        # A second attempt on an unknown effect is refused, not repeated.
        with self.assertRaises(BrokerError):
            self.execute(self.approved())
        self.assertEqual(self.recorder.calls, [])


class RoutineTests(BrokerFixture):
    def test_a_routine_read_needs_no_approval(self) -> None:
        self.hold("trello", "api_key", "main_operator")
        self.hold("trello", "token", "main_operator")
        reply = self.broker.handle(
            self.peer("main_operator"),
            {
                "op": "execute",
                "operation": "trello.list_board_cards",
                "arguments": {"board_id": "board-1", "limit": 100},
            },
            actor="main_operator",
        )
        self.assertTrue(reply["ok"])
        self.assertEqual(len(self.recorder.calls), 1)

    def test_no_reply_ever_carries_credential_material(self) -> None:
        self.store.put("trello", "api_key", "material-abcdefgh", "main_operator")
        self.store.put("trello", "token", "material-ijklmnop", "main_operator")
        reply = self.broker.handle(
            self.peer("main_operator"),
            {
                "op": "execute",
                "operation": "trello.list_board_cards",
                "arguments": {"board_id": "board-1"},
            },
            actor="main_operator",
        )
        # The broker puts the credential on the outbound request -- that is its
        # whole job. What must never carry it is the answer that goes back
        # across the socket to the runtime.
        rendered = repr(reply)
        self.assertNotIn("material-abcdefgh", rendered)
        self.assertNotIn("material-ijklmnop", rendered)
        self.assertNotIn("key=", rendered)

    def test_an_actor_without_its_own_credential_is_not_connected(self) -> None:
        reply = self.broker.handle(
            self.peer("employee"),
            {"op": "status", "provider": "trello", "credential_class": "token"},
            actor="employee",
        )
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["state"], "credential absent")


class InstalledShapeTests(unittest.TestCase):
    """What the installed deployment actually keeps out of the container.

    The per-actor sockets above are real and enforced, and on a topology that
    gives each actor its own account they are the whole answer. The pinned
    runtime is not that topology: it is one Discord gateway process serving
    three profiles, and one process cannot be three accounts. Asserting three
    per-actor containers here would assert an architecture this deployment must
    not have -- three gateways on one bot token is duplicate Discord
    consumption, which is a failure this repository already refuses elsewhere.

    So what is asserted is what is true and enforced: the material the runtime
    must never reach is not reachable from it, and the actor on a provider call
    is established from Discord rather than from the caller.
    """

    def test_no_credential_store_or_ledger_is_mounted_into_the_runtime(self) -> None:
        compose = Path("compose.yaml").read_text(encoding="utf-8")
        for path in ("/var/lib/scotty", "credentials.json", "grants.json", "effects.db"):
            with self.subTest(path=path):
                self.assertNotIn(path, compose)

    def test_the_container_reaches_one_socket_and_not_the_store(self) -> None:
        compose = Path("compose.yaml").read_text(encoding="utf-8")
        # The broker's runtime directory, and nothing else on the host.
        self.assertIn("source: /run/scotty", compose)
        self.assertNotIn("source: /var/lib/scotty", compose)

    def test_the_broker_runs_as_root_outside_every_mount(self) -> None:
        unit = Path("broker/scotty-credential-broker.service").read_text(encoding="utf-8")
        installer = Path("install.sh").read_text(encoding="utf-8")
        self.assertIn("User=root", unit)
        self.assertIn("readonly BROKER_DIR=/usr/local/lib/scotty/scotty_broker", installer)

    def test_a_provider_call_must_cite_the_message_it_is_acting_on(self) -> None:
        executable = Path("scotty-credential-broker").read_text(encoding="utf-8")
        # The installed service builds the resolver. Without one, `execute`
        # would fall back to the socket's actor, which on this topology is the
        # runtime rather than a person.
        self.assertIn("ProvenanceResolver", executable)


class AttestedActorTests(BrokerFixture):
    """The actor comes from Discord, and disagreement is refused."""

    def setUp(self) -> None:
        super().setUp()
        from assistant.scotty_broker.provenance import ProvenanceResolver, Route

        self.messages: dict[str, dict[str, object]] = {
            "900000000000000001": {
                "channel_id": "800000000000000001",
                "author": {"id": "700000000000000001"},
            },
            "900000000000000002": {
                "channel_id": "800000000000000002",
                "author": {"id": "700000000000000002"},
            },
        }
        self.fetched: list[str] = []

        def fetch(url, headers):
            self.fetched.append(url)
            message_id = url.rsplit("/", 1)[-1]
            body = self.messages.get(message_id)
            return (200, body) if body is not None else (404, None)

        self.resolver = ProvenanceResolver(
            (
                Route("800000000000000001", "700000000000000001", "main_operator"),
                Route("800000000000000002", "700000000000000002", "employee"),
            ),
            lambda: "synthetic-bot-token",
            fetch=fetch,
            clock=lambda: 1000.0,
        )
        self.broker.provenance = self.resolver

    def cite(self, message_id: str, channel_id: str) -> dict[str, object]:
        return {"channel_id": channel_id, "message_id": message_id}

    def read(self, provenance: dict[str, object]) -> dict[str, object]:
        return self.broker.handle(
            self.peer("main_operator"),
            {
                "op": "execute",
                "operation": "trello.list_board_cards",
                "arguments": {"board_id": "board-1"},
                "provenance": provenance,
            },
            actor="main_operator",
        )

    def test_the_actor_is_whoever_discord_says_wrote_the_message(self) -> None:
        self.hold("trello", "api_key", "main_operator")
        self.hold("trello", "token", "main_operator")
        reply = self.read(self.cite("900000000000000001", "800000000000000001"))
        self.assertTrue(reply["ok"])
        self.assertTrue(self.fetched)

    def test_citing_another_user_s_message_is_refused_not_honoured(self) -> None:
        self.hold("trello", "api_key", "main_operator")
        self.hold("trello", "token", "main_operator")
        # Mikey's message, on the main operator's socket. Neither identity is
        # handed out: the two sources of truth disagree.
        with self.assertRaises(BrokerError):
            self.read(self.cite("900000000000000002", "800000000000000002"))
        self.assertEqual(self.recorder.calls, [])

    def test_a_message_discord_does_not_have_establishes_nobody(self) -> None:
        self.hold("trello", "api_key", "main_operator")
        self.hold("trello", "token", "main_operator")
        with self.assertRaises(BrokerError):
            self.read(self.cite("900000000000000009", "800000000000000001"))
        self.assertEqual(self.recorder.calls, [])

    def test_a_message_from_another_channel_is_refused(self) -> None:
        self.hold("trello", "api_key", "main_operator")
        self.hold("trello", "token", "main_operator")
        with self.assertRaises(BrokerError):
            # The message exists, but not where the caller says it does.
            self.read(self.cite("900000000000000002", "800000000000000001"))
        self.assertEqual(self.recorder.calls, [])

    def test_an_unserved_channel_is_nobody_at_all(self) -> None:
        self.hold("trello", "api_key", "main_operator")
        self.hold("trello", "token", "main_operator")
        with self.assertRaises(BrokerError):
            self.read(self.cite("900000000000000001", "800000000000000009"))

    def test_the_bot_s_own_message_is_not_somebody_asking(self) -> None:
        self.hold("trello", "api_key", "main_operator")
        self.hold("trello", "token", "main_operator")
        self.messages["900000000000000001"]["author"] = {
            "id": "700000000000000001",
            "bot": True,
        }
        with self.assertRaises(BrokerError):
            self.read(self.cite("900000000000000001", "800000000000000001"))

    def test_the_shared_gateway_socket_answers_nobody_without_a_citation(self) -> None:
        """The container is not an actor, so silence establishes no one.

        A worker with its own account has already been identified by the
        kernel. The pinned runtime container has not: it is one process serving
        three profiles, and no uid it could have would say which of them is
        asking. On its socket a citation is the only identity there is.
        """

        from assistant.scotty_broker.broker import RUNTIME_ACTOR, RUNTIME_UID

        self.hold("trello", "api_key", "main_operator")
        self.hold("trello", "token", "main_operator")
        container = Peer(pid=11, uid=RUNTIME_UID, gid=RUNTIME_UID)
        with self.assertRaises(BrokerError):
            self.broker.handle(
                container,
                {
                    "op": "execute",
                    "operation": "trello.list_board_cards",
                    "arguments": {"board_id": "board-1"},
                },
                actor=RUNTIME_ACTOR,
            )
        self.assertEqual(self.recorder.calls, [])

    def test_the_shared_gateway_socket_acts_as_whoever_discord_names(self) -> None:
        from assistant.scotty_broker.broker import RUNTIME_ACTOR, RUNTIME_UID

        self.hold("trello", "api_key", "employee")
        self.hold("trello", "token", "employee")
        container = Peer(pid=11, uid=RUNTIME_UID, gid=RUNTIME_UID)
        reply = self.broker.handle(
            container,
            {
                "op": "execute",
                "operation": "trello.list_board_cards",
                "arguments": {"board_id": "board-1"},
                # Mikey's own message, in Mikey's own channel.
                "provenance": self.cite("900000000000000002", "800000000000000002"),
            },
            actor=RUNTIME_ACTOR,
        )
        self.assertTrue(reply["ok"])

    def test_the_shared_gateway_cannot_reach_a_user_who_has_not_spoken(self) -> None:
        from assistant.scotty_broker.broker import RUNTIME_ACTOR, RUNTIME_UID

        self.hold("trello", "api_key", "employee")
        self.hold("trello", "token", "employee")
        container = Peer(pid=11, uid=RUNTIME_UID, gid=RUNTIME_UID)
        with self.assertRaises(BrokerError):
            self.broker.handle(
                container,
                {
                    "op": "execute",
                    "operation": "trello.list_board_cards",
                    "arguments": {"board_id": "board-1"},
                    # A message id nobody wrote.
                    "provenance": self.cite("900000000000000099", "800000000000000002"),
                },
                actor=RUNTIME_ACTOR,
            )
        self.assertEqual(self.recorder.calls, [])


if __name__ == "__main__":
    unittest.main()
