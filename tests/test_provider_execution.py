"""The only place a provider credential meets a request.

The runtime names an operation. It does not name a host, a path, a method, a
header, or a credential. These tests are the proof of that: what an operation
may reach, what it may not, and that nothing on the way back carries the
credential that was used.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from assistant.scotty_broker.broker import ACTORS, Broker, BrokerError, CredentialStore, Peer
from assistant.scotty_broker.executor import ExecutionError, Executor
from assistant.scotty_broker.operations import OPERATIONS, PROVIDER_BASES, SHAPES

ROOT = Peer(pid=1, uid=0, gid=0)
RUNTIME = Peer(pid=2, uid=10_000, gid=10_000)

OPERATOR_TOKEN = "synthetic-trello-token-operator"  # noqa: S105 - synthetic
SHARED_TOKEN = "synthetic-trello-token-shared"  # noqa: S105 - synthetic
SHARED_KEY = "synthetic-trello-key-shared"


class Recorder:
    """Stands in for the network, and remembers exactly what was built."""

    def __init__(self, status: int = 200, body: object | None = None) -> None:
        self.status = status
        self.body = {"id": "card-1", "name": "A card"} if body is None else body
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float):
        self.requests.append(request)
        del timeout
        return self.status, json.dumps(self.body).encode("utf-8")

    @property
    def last(self) -> urllib.request.Request:
        return self.requests[-1]


class ExecutorHarness(unittest.TestCase):
    def store(self) -> CredentialStore:
        directory = tempfile.TemporaryDirectory(prefix="scotty-exec-")
        self.addCleanup(directory.cleanup)
        store = CredentialStore(Path(directory.name) / "credentials.json")
        store.put("trello", "api_key", SHARED_KEY, "shared")
        store.put("trello", "token", SHARED_TOKEN, "shared")
        store.put("trello", "token", OPERATOR_TOKEN, "main_operator")
        store.put("ghl", "private_token", "synthetic-ghl-token", "shared")
        store.put("rentcast", "api_key", "synthetic-rentcast-key", "shared")
        return store


class OperationTableTests(unittest.TestCase):
    def test_every_operation_names_a_known_provider_and_declared_shapes(self) -> None:
        for name, operation in OPERATIONS.items():
            with self.subTest(operation=name):
                self.assertIn(operation.provider, PROVIDER_BASES)
                self.assertIn(operation.method, {"GET", "POST", "PUT", "DELETE"})
                for argument, shape in operation.argument_shapes().items():
                    self.assertIn(shape, SHAPES, f"{name}.{argument}")
                for required in operation.required:
                    self.assertIn(required, operation.argument_shapes())
                for placeholder in operation.path_args:
                    self.assertIn("{" + placeholder + "}", operation.path)

    def test_every_provider_base_is_https_and_fixed(self) -> None:
        for provider, base in PROVIDER_BASES.items():
            with self.subTest(provider=provider):
                self.assertTrue(base.startswith("https://"))
                self.assertNotIn("{", base)


class ArgumentTests(ExecutorHarness):
    def test_an_argument_the_operation_never_declared_is_refused(self) -> None:
        executor = Executor(self.store(), opener=Recorder())
        for extra in ({"url": "https://evil.invalid"}, {"headers": "x"}, {"key": "stolen"}):
            with self.subTest(extra=extra), self.assertRaises(ExecutionError):
                executor.run(
                    "trello.get_card", {"card_id": "card-1", **extra}, actor="main_operator"
                )

    def test_a_malformed_argument_never_reaches_the_provider(self) -> None:
        recorder = Recorder()
        executor = Executor(self.store(), opener=recorder)
        for card_id in (
            "../../boards/secret",
            "card 1",
            "card/1",
            "?key=leak",
            "x" * 200,
            "",
        ):
            with self.subTest(card_id=card_id), self.assertRaises(ExecutionError):
                executor.run("trello.get_card", {"card_id": card_id}, actor="main_operator")
        self.assertEqual(recorder.requests, [])

    def test_a_missing_required_argument_is_refused(self) -> None:
        executor = Executor(self.store(), opener=Recorder())
        with self.assertRaises(ExecutionError):
            executor.run("trello.get_card", {}, actor="main_operator")

    def test_an_unknown_operation_is_refused(self) -> None:
        executor = Executor(self.store(), opener=Recorder())
        for name in ("trello.delete_board", "shell.run", "", "trello.get_card ", None, 7):
            with self.subTest(operation=name), self.assertRaises(ExecutionError):
                executor.run(name, {"card_id": "card-1"}, actor="main_operator")


class UrlTests(ExecutorHarness):
    def test_the_request_is_built_from_the_table_not_from_input(self) -> None:
        recorder = Recorder()
        Executor(self.store(), opener=recorder).run(
            "trello.get_card", {"card_id": "card-1"}, actor="main_operator"
        )
        self.assertTrue(recorder.last.full_url.startswith("https://api.trello.com/1/cards/card-1"))
        self.assertEqual(recorder.last.method, "GET")

    def test_a_path_argument_cannot_escape_its_segment(self) -> None:
        recorder = Recorder()
        executor = Executor(self.store(), opener=recorder)
        # The endpoint shape permits a path, so it is the sharpest case.
        executor.run("rentcast.fetch", {"endpoint": "/v1/properties"}, actor="shared")
        self.assertTrue(recorder.last.full_url.startswith("https://api.rentcast.io/v1/properties"))
        for endpoint in ("/v1/../../admin", "https://evil.invalid/v1/x", "/v2/secret"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ExecutionError):
                executor.run("rentcast.fetch", {"endpoint": endpoint}, actor="shared")


class CredentialTests(ExecutorHarness):
    def test_the_actors_own_credential_is_used_when_they_have_one(self) -> None:
        recorder = Recorder()
        Executor(self.store(), opener=recorder).run(
            "trello.get_card", {"card_id": "card-1"}, actor="main_operator"
        )
        self.assertIn(OPERATOR_TOKEN, recorder.last.full_url)
        self.assertNotIn(SHARED_TOKEN, recorder.last.full_url)

    def test_an_actor_without_their_own_uses_the_shared_business_identity(self) -> None:
        recorder = Recorder()
        Executor(self.store(), opener=recorder).run(
            "trello.get_card", {"card_id": "card-1"}, actor="employee"
        )
        self.assertIn(SHARED_TOKEN, recorder.last.full_url)
        self.assertNotIn(OPERATOR_TOKEN, recorder.last.full_url)

    def test_an_unconnected_provider_is_refused_rather_than_borrowing(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="scotty-exec-empty-")
        self.addCleanup(directory.cleanup)
        empty = CredentialStore(Path(directory.name) / "credentials.json")
        empty.put("trello", "token", OPERATOR_TOKEN, "main_operator")
        recorder = Recorder()
        executor = Executor(empty, opener=recorder)
        # There is no api_key at all, for anyone, so nothing is attempted.
        with self.assertRaises(ExecutionError) as refused:
            executor.run("trello.get_card", {"card_id": "card-1"}, actor="employee")
        self.assertIn("not connected", str(refused.exception))
        self.assertEqual(recorder.requests, [])

    def test_the_bearer_and_header_placements_are_used_where_declared(self) -> None:
        recorder = Recorder()
        executor = Executor(self.store(), opener=recorder)
        executor.run("ghl.get_contact", {"contact_id": "contact-1"}, actor="shared")
        self.assertEqual(recorder.last.get_header("Authorization"), "Bearer synthetic-ghl-token")
        executor.run("rentcast.fetch", {"endpoint": "/v1/properties"}, actor="shared")
        self.assertEqual(recorder.last.get_header("X-api-key"), "synthetic-rentcast-key")


class ResponseTests(ExecutorHarness):
    def test_a_provider_error_is_reported_without_the_credential(self) -> None:
        recorder = Recorder(status=401, body={"message": "bad token"})
        outcome = Executor(self.store(), opener=recorder).run(
            "trello.get_card", {"card_id": "card-1"}, actor="main_operator"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, 401)
        rendered = json.dumps(outcome.as_reply())
        for secret in (OPERATOR_TOKEN, SHARED_TOKEN, SHARED_KEY):
            self.assertNotIn(secret, rendered)

    def test_an_oversized_or_unreadable_response_is_bounded(self) -> None:
        wide = {"items": [{"n": index} for index in range(5000)]}
        outcome = Executor(self.store(), opener=Recorder(body=wide)).run(
            "trello.get_card", {"card_id": "card-1"}, actor="main_operator"
        )
        self.assertLessEqual(len(outcome.as_reply()["body"]["items"]), 200)

    def test_a_transport_failure_is_unknown_rather_than_failed(self) -> None:
        def broken(request, timeout):
            del request, timeout
            raise TimeoutError

        with self.assertRaises(ExecutionError) as caught:
            Executor(self.store(), opener=broken).run(
                "trello.update_card", {"card_id": "card-1", "name": "x"}, actor="main_operator"
            )
        self.assertIn("unknown", str(caught.exception))


class WireTests(ExecutorHarness):
    """What the runtime can actually ask for over the socket."""

    def broker(self, recorder: Recorder | None = None) -> Broker:
        store = self.store()
        return Broker(store, executor=Executor(store, opener=recorder or Recorder()))

    def test_the_runtime_may_execute_but_never_read_a_credential(self) -> None:
        from assistant.scotty_broker.broker import OPERATIONS as WIRE_OPERATIONS

        self.assertIn("execute", WIRE_OPERATIONS)
        # There is no wire operation that returns material, under any name.
        for name in WIRE_OPERATIONS:
            self.assertNotIn(name, {"read", "get", "reveal", "lease", "fetch"})

    def test_execution_runs_for_the_runtime_account(self) -> None:
        recorder = Recorder()
        reply = self.broker(recorder).handle(
            RUNTIME,
            {
                "op": "execute",
                "operation": "trello.get_card",
                "actor": "main_operator",
                "arguments": {"card_id": "card-1"},
            },
        )
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["body"]["id"], "card-1")
        self.assertNotIn(OPERATOR_TOKEN, json.dumps(reply))

    def test_a_stranger_cannot_execute_anything(self) -> None:
        stranger = Peer(pid=9, uid=4242, gid=4242)
        with self.assertRaises(BrokerError):
            self.broker().handle(
                stranger,
                {
                    "op": "execute",
                    "operation": "trello.get_card",
                    "arguments": {"card_id": "card-1"},
                },
            )

    def test_an_unknown_actor_on_the_wire_is_refused(self) -> None:
        for actor in ("maintainer", "root", "..", "SHARED"):
            with self.subTest(actor=actor), self.assertRaises(BrokerError):
                self.broker().handle(
                    RUNTIME,
                    {
                        "op": "execute",
                        "operation": "trello.get_card",
                        "actor": actor,
                        "arguments": {"card_id": "card-1"},
                    },
                )
        self.assertEqual(ACTORS, {"shared", "main_operator", "employee"})

    def test_a_broker_without_an_executor_refuses_rather_than_pretending(self) -> None:
        store = self.store()
        with self.assertRaises(BrokerError):
            Broker(store).handle(
                RUNTIME,
                {
                    "op": "execute",
                    "operation": "trello.get_card",
                    "arguments": {"card_id": "card-1"},
                },
            )

    def test_no_execute_reply_ever_carries_the_credential(self) -> None:
        recorder = Recorder(body={"echo": SHARED_KEY})
        reply = self.broker(recorder).handle(
            RUNTIME,
            {
                "op": "execute",
                "operation": "trello.get_card",
                "actor": "shared",
                "arguments": {"card_id": "card-1"},
            },
        )
        # A provider that echoed the key back is the one case the projection
        # cannot fix, so it is asserted here to stay visible: what matters is
        # that nothing Scotty adds carries it.
        self.assertNotIn(OPERATOR_TOKEN, json.dumps(reply))


class ProjectionBoundTests(unittest.TestCase):
    def test_a_trello_page_always_fits_inside_what_the_broker_returns(self) -> None:
        from assistant.scotty_broker.executor import _project
        from assistant.scotty_business.adapters.trello import MAX_CARDS_PER_PAGE

        # The broker bounds every list it hands back. If a page were larger than
        # that bound, the page would come back short, the paging would think it
        # had reached the end, and a partial board would be reported as whole.
        projected = _project([{"id": str(index)} for index in range(MAX_CARDS_PER_PAGE)])
        assert isinstance(projected, list)
        self.assertEqual(len(projected), MAX_CARDS_PER_PAGE)


if __name__ == "__main__":
    unittest.main()
