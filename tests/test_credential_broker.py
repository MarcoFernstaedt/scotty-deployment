from __future__ import annotations

import json
import os
import pwd
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from assistant.scotty_broker.broker import (
    ACTOR_OPERATIONS,
    CREDENTIAL_CLASSES,
    MAX_FRAME_BYTES,
    MAX_MATERIAL_CHARS,
    ROOT_OPERATIONS,
    RUNTIME_ACTOR,
    RUNTIME_UID,
    Broker,
    BrokerError,
    CredentialStore,
    Peer,
    bind_control_socket,
    bind_socket,
    serve_forever,
)

SECRET = "synthetic-provider-key-000000"  # noqa: S105 - synthetic fixture
OTHER = "synthetic-provider-key-000001"  # noqa: S105 - synthetic fixture
ROOT = Peer(pid=1, uid=0, gid=0)
RUNTIME = Peer(pid=2, uid=RUNTIME_UID, gid=RUNTIME_UID)


def as_runtime(broker, request):
    """One request from the shared runtime container, on its own socket.

    The socket is the identity now, so a test that reaches the broker as the
    runtime has to arrive the way the runtime does.
    """

    return broker.handle(RUNTIME, request, actor=RUNTIME_ACTOR)


STRANGER = Peer(pid=3, uid=1234, gid=1234)


class BrokerHarness(unittest.TestCase):
    def broker(self, **kwargs):
        directory = tempfile.TemporaryDirectory(prefix="scotty-broker-")
        self.addCleanup(directory.cleanup)
        store = CredentialStore(Path(directory.name) / "credentials.json")
        return Broker(store, **kwargs), store

    def commit(self, broker, provider="trello", credential_class="api_key", material=SECRET):
        opened = broker.handle(
            ROOT, {"op": "open", "provider": provider, "credential_class": credential_class}
        )
        return broker.handle(
            ROOT,
            {
                "op": "commit",
                "provider": provider,
                "credential_class": credential_class,
                "window": opened["window"],
                "material": material,
            },
        )


class AuthorizationTests(BrokerHarness):
    """Authority comes from the kernel's peer credentials, not the message."""

    def test_root_may_use_every_operation(self) -> None:
        for operation in sorted(ROOT_OPERATIONS | ACTOR_OPERATIONS):
            with self.subTest(operation=operation):
                self.assertTrue(ROOT.may(operation))

    def test_the_runtime_may_only_ask_for_status(self) -> None:
        self.assertEqual(
            {operation for operation in ROOT_OPERATIONS if RUNTIME.may(operation)}, set()
        )
        self.assertTrue(RUNTIME.may("status", actor=RUNTIME_ACTOR))

    def test_an_unprivileged_caller_may_do_nothing_at_all(self) -> None:
        for operation in sorted(ROOT_OPERATIONS | ACTOR_OPERATIONS):
            with self.subTest(operation=operation):
                self.assertFalse(STRANGER.may(operation, actor=RUNTIME_ACTOR))

    def test_the_runtime_cannot_commit_or_revoke_a_credential(self) -> None:
        broker, store = self.broker()
        self.commit(broker)
        for request in (
            {
                "op": "commit",
                "provider": "trello",
                "credential_class": "api_key",
                "window": "0" * 32,
                "material": OTHER,
            },
            {"op": "revoke", "provider": "trello", "credential_class": "api_key"},
            {"op": "open", "provider": "trello", "credential_class": "api_key"},
            {
                "op": "validate",
                "provider": "trello",
                "credential_class": "api_key",
                "material": OTHER,
            },
        ):
            with self.subTest(op=request["op"]), self.assertRaises(BrokerError) as caught:
                as_runtime(broker, request)
            self.assertEqual(str(caught.exception), "unauthorized")
        self.assertTrue(store.present("trello", "api_key"))

    def test_an_unknown_caller_is_refused_before_the_request_is_parsed(self) -> None:
        broker, _ = self.broker()
        with self.assertRaises(BrokerError):
            broker.handle(
                STRANGER, {"op": "status", "provider": "trello", "credential_class": "api_key"}
            )


class FixedOperationTests(BrokerHarness):
    def test_a_committed_credential_reads_as_present_but_never_comes_back(self) -> None:
        broker, store = self.broker()
        outcome = self.commit(broker)
        self.assertEqual(outcome, {"ok": True, "state": "credential present"})

        status = as_runtime(
            broker, {"op": "status", "provider": "trello", "credential_class": "api_key"}
        )
        self.assertEqual(status, {"ok": True, "state": "credential present"})
        self.assertNotIn(SECRET, json.dumps(status))
        self.assertNotIn(SECRET, repr(store))

    def test_an_absent_credential_reports_absent(self) -> None:
        broker, _ = self.broker()
        self.assertEqual(
            as_runtime(
                broker, {"op": "status", "provider": "ghl", "credential_class": "private_token"}
            ),
            {"ok": False, "state": "credential absent"},
        )

    def test_revoking_removes_the_credential_and_is_idempotent(self) -> None:
        broker, store = self.broker()
        self.commit(broker)
        first = broker.handle(
            ROOT, {"op": "revoke", "provider": "trello", "credential_class": "api_key"}
        )
        second = broker.handle(
            ROOT, {"op": "revoke", "provider": "trello", "credential_class": "api_key"}
        )
        self.assertEqual(first, {"ok": True, "state": "credential removed"})
        self.assertEqual(second, {"ok": False, "state": "no credential"})
        self.assertFalse(store.present("trello", "api_key"))

    def test_validation_answers_without_storing_anything(self) -> None:
        broker, store = self.broker()
        outcome = broker.handle(
            ROOT,
            {
                "op": "validate",
                "provider": "trello",
                "credential_class": "api_key",
                "material": SECRET,
            },
        )
        self.assertEqual(outcome, {"ok": True, "state": "validation passed"})
        self.assertFalse(store.present("trello", "api_key"))

    def test_a_provider_rejection_stores_nothing(self) -> None:
        broker, store = self.broker(validator=lambda *_: False)
        outcome = self.commit(broker)
        self.assertEqual(outcome, {"ok": False, "state": "validation failed"})
        self.assertFalse(store.present("trello", "api_key"))

    def test_only_the_named_providers_and_classes_exist(self) -> None:
        broker, _ = self.broker()
        for provider, credential_class in (
            ("zillow", "api_key"),
            ("trello", "password"),
            ("google_workspace", "refresh_token"),
            ("", ""),
            (None, None),
            (7, 7),
        ):
            with self.subTest(provider=provider), self.assertRaises(BrokerError):
                as_runtime(
                    broker,
                    {"op": "status", "provider": provider, "credential_class": credential_class},
                )
        self.assertNotIn("google_workspace", CREDENTIAL_CLASSES)


class WindowTests(BrokerHarness):
    def test_a_commit_without_an_open_window_is_refused(self) -> None:
        broker, store = self.broker()
        with self.assertRaises(BrokerError) as caught:
            broker.handle(
                ROOT,
                {
                    "op": "commit",
                    "provider": "trello",
                    "credential_class": "api_key",
                    "window": "a" * 32,
                    "material": SECRET,
                },
            )
        self.assertEqual(str(caught.exception), "no open window")
        self.assertFalse(store.present("trello", "api_key"))

    def test_a_window_is_single_use_so_a_replay_is_refused(self) -> None:
        broker, _ = self.broker()
        opened = broker.handle(
            ROOT, {"op": "open", "provider": "trello", "credential_class": "api_key"}
        )
        request = {
            "op": "commit",
            "provider": "trello",
            "credential_class": "api_key",
            "window": opened["window"],
            "material": SECRET,
        }
        self.assertTrue(broker.handle(ROOT, request)["ok"])
        with self.assertRaises(BrokerError):
            broker.handle(ROOT, dict(request, material=OTHER))

    def test_a_failed_commit_still_consumes_its_window(self) -> None:
        broker, _ = self.broker(validator=lambda *_: False)
        opened = broker.handle(
            ROOT, {"op": "open", "provider": "trello", "credential_class": "api_key"}
        )
        request = {
            "op": "commit",
            "provider": "trello",
            "credential_class": "api_key",
            "window": opened["window"],
            "material": SECRET,
        }
        self.assertFalse(broker.handle(ROOT, request)["ok"])
        with self.assertRaises(BrokerError):
            broker.handle(ROOT, request)

    def test_an_expired_window_is_refused(self) -> None:
        now = [1000.0]
        broker, store = self.broker(clock=lambda: now[0], window_seconds=300)
        opened = broker.handle(
            ROOT, {"op": "open", "provider": "trello", "credential_class": "api_key"}
        )
        now[0] += 301
        with self.assertRaises(BrokerError):
            broker.handle(
                ROOT,
                {
                    "op": "commit",
                    "provider": "trello",
                    "credential_class": "api_key",
                    "window": opened["window"],
                    "material": SECRET,
                },
            )
        self.assertFalse(store.present("trello", "api_key"))

    def test_a_window_never_satisfies_a_different_credential(self) -> None:
        broker, store = self.broker()
        opened = broker.handle(
            ROOT, {"op": "open", "provider": "trello", "credential_class": "api_key"}
        )
        with self.assertRaises(BrokerError):
            broker.handle(
                ROOT,
                {
                    "op": "commit",
                    "provider": "ghl",
                    "credential_class": "private_token",
                    "window": opened["window"],
                    "material": SECRET,
                },
            )
        self.assertFalse(store.present("ghl", "private_token"))

    def test_a_malformed_window_identifier_is_refused(self) -> None:
        broker, _ = self.broker()
        for window in ("", "not-hex", "A" * 32, "0" * 31, None, 7):
            with self.subTest(window=window), self.assertRaises(BrokerError):
                broker.handle(
                    ROOT,
                    {
                        "op": "commit",
                        "provider": "trello",
                        "credential_class": "api_key",
                        "window": window,
                        "material": SECRET,
                    },
                )

    def test_a_restart_invalidates_every_open_window(self) -> None:
        broker, store = self.broker()
        opened = broker.handle(
            ROOT, {"op": "open", "provider": "trello", "credential_class": "api_key"}
        )
        restarted = Broker(store)
        with self.assertRaises(BrokerError):
            restarted.handle(
                ROOT,
                {
                    "op": "commit",
                    "provider": "trello",
                    "credential_class": "api_key",
                    "window": opened["window"],
                    "material": SECRET,
                },
            )


class MalformedInputTests(BrokerHarness):
    def test_a_malformed_request_or_unknown_operation_is_refused(self) -> None:
        broker, _ = self.broker()
        for request in (None, [], "status", 7, {}, {"op": "read"}, {"op": ""}, {"op": 7}):
            with self.subTest(request=request), self.assertRaises(BrokerError):
                broker.handle(ROOT, request)

    def test_oversized_or_malformed_material_is_refused(self) -> None:
        broker, store = self.broker()
        for material in (
            "short",
            "x" * (MAX_MATERIAL_CHARS + 1),
            "has spaces",
            "line\nbreak",
            None,
            7,
        ):
            with self.subTest(material=str(material)[:20]), self.assertRaises(BrokerError):
                broker.handle(
                    ROOT,
                    {
                        "op": "validate",
                        "provider": "trello",
                        "credential_class": "api_key",
                        "material": material,
                    },
                )
        self.assertFalse(store.present("trello", "api_key"))


class StoreSafetyTests(BrokerHarness):
    def test_the_store_is_written_owner_only(self) -> None:
        broker, store = self.broker()
        self.commit(broker)
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_a_group_readable_store_is_refused_rather_than_trusted(self) -> None:
        broker, store = self.broker()
        self.commit(broker)
        store.path.chmod(0o644)
        with self.assertRaises(BrokerError):
            store.present("trello", "api_key")

    def test_a_symlinked_store_path_is_never_followed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-broker-link-") as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "store.json"
            link.symlink_to(target)
            store = CredentialStore(link)
            self.assertFalse(store.present("trello", "api_key"))
            with self.assertRaises(BrokerError):
                store.put("trello", "api_key", SECRET)
            self.assertEqual(target.read_text(encoding="utf-8"), "{}")

    def test_a_corrupt_store_is_refused_rather_than_guessed_at(self) -> None:
        broker, store = self.broker()
        store.path.write_text("not json", encoding="utf-8")
        store.path.chmod(0o600)
        with self.assertRaises(BrokerError):
            store.present("trello", "api_key")


class InstalledSocketTests(unittest.TestCase):
    """The real artefact over a real socket, not a mock of one."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="scotty-broker-socket-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.socket_path = self.root / "credential-broker.sock"
        self.store = CredentialStore(self.root / "credentials.json")
        # The runtime uid is deployment configuration: which account the
        # container was installed to run as. Here that is this test process, so
        # the peer the kernel reports is genuinely the runtime account and the
        # runtime operation is genuinely authorized. Production policy is
        # untouched — root operations are gated on uid 0 alone, so a non-root
        # harness cannot reach them however this is set.
        self.broker = Broker(self.store, actor_uids={RUNTIME_ACTOR: os.getuid()})
        self.server = bind_socket(self.socket_path, group=os.getgid())
        self.addCleanup(self.server.close)
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=serve_forever,
            args=(self.broker, self.server),
            kwargs={"actor": RUNTIME_ACTOR, "should_stop": self.stop.is_set},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.thread.join, 3.0)
        self.addCleanup(self.stop.set)

    def commit_as_root(self, provider: str, credential_class: str, material: str) -> None:
        """Store one credential through the identity production requires.

        Committing is root's alone. Where this process is not root it cannot
        become root, and faking the peer would be testing a fiction — so the
        commit goes through the broker's own handler with the root peer, which
        is the same production code path minus the socket. The socket, and the
        kernel's answer about who is calling, are proven separately by the
        operations this peer really holds.
        """

        opened = self.broker.handle(
            ROOT, {"op": "open", "provider": provider, "credential_class": credential_class}
        )
        self.assertTrue(opened["ok"])
        committed = self.broker.handle(
            ROOT,
            {
                "op": "commit",
                "provider": provider,
                "credential_class": credential_class,
                "window": opened["window"],
                "material": material,
            },
        )
        self.assertEqual(committed["state"], "credential present")

    def call(self, request, *, raw: bytes | None = None):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        client.connect(str(self.socket_path))
        with client:
            payload = raw if raw is not None else json.dumps(request).encode("utf-8") + b"\n"
            client.sendall(payload)
            client.shutdown(socket.SHUT_WR)
            return json.loads(client.recv(4096).decode("utf-8"))

    def test_the_socket_answers_the_operation_this_peer_may_actually_run(self) -> None:
        """Real socket, real SO_PEERCRED, and the one operation this peer has.

        The credential is committed through the broker as root — the identity
        production requires — and then read back over the wire as the runtime
        account, which is exactly the split the deployment runs with.
        """

        self.commit_as_root("trello", "api_key", SECRET)
        status = self.call({"op": "status", "provider": "trello", "credential_class": "api_key"})
        self.assertEqual(status, {"ok": True, "state": "credential present"})

    @unittest.skipIf(os.geteuid() == 0, "this process is root, so nothing is refused")
    def test_a_root_only_operation_is_refused_over_the_real_socket(self) -> None:
        """The kernel says who is calling, and a non-root caller may not open."""

        for operation in ("open", "validate", "commit", "revoke"):
            with self.subTest(operation=operation):
                reply = self.call(
                    {
                        "op": operation,
                        "provider": "trello",
                        "credential_class": "api_key",
                        "material": SECRET,
                        "window": "whatever",
                    }
                )
                self.assertEqual(reply, {"ok": False, "state": "unauthorized"})

    @unittest.skipUnless(os.geteuid() == 0, "the full lifecycle needs a genuinely root peer")
    def test_the_whole_lifecycle_runs_over_the_real_socket_when_root(self) -> None:
        """When the test peer really is root, nothing is simulated at all.

        Root's operations arrive on root's own socket. The actor sockets carry
        no root authority at all now -- not even for root -- which is why this
        binds the control socket rather than reusing the one above.
        """

        control_path = self.root / "control.sock"
        control = bind_control_socket(control_path)
        self.addCleanup(control.close)
        stop = threading.Event()
        thread = threading.Thread(
            target=serve_forever,
            args=(self.broker, control),
            kwargs={"should_stop": stop.is_set},
            daemon=True,
        )
        thread.start()
        self.addCleanup(thread.join, 3.0)
        self.addCleanup(stop.set)
        saved, self.socket_path = self.socket_path, control_path
        self.addCleanup(setattr, self, "socket_path", saved)

        opened = self.call({"op": "open", "provider": "trello", "credential_class": "api_key"})
        self.assertTrue(opened["ok"])
        committed = self.call(
            {
                "op": "commit",
                "provider": "trello",
                "credential_class": "api_key",
                "window": opened["window"],
                "material": SECRET,
            }
        )
        self.assertEqual(committed["state"], "credential present")
        self.assertEqual(
            self.call({"op": "status", "provider": "trello", "credential_class": "api_key"}),
            {"ok": True, "state": "credential present"},
        )

    def test_the_socket_is_owner_and_group_only(self) -> None:
        mode = self.socket_path.stat().st_mode
        self.assertTrue(stat.S_ISSOCK(mode))
        self.assertEqual(mode & 0o777, 0o660)
        self.assertEqual(mode & stat.S_IRWXO, 0)

    def test_no_reply_ever_carries_credential_material(self) -> None:
        """Every operation, over both the root path and the real wire."""

        opened = self.broker.handle(
            ROOT, {"op": "open", "provider": "ghl", "credential_class": "private_token"}
        )
        replies = [
            opened,
            self.broker.handle(
                ROOT,
                {
                    "op": "commit",
                    "provider": "ghl",
                    "credential_class": "private_token",
                    "window": opened["window"],
                    "material": SECRET,
                },
            ),
            self.broker.handle(
                ROOT,
                {
                    "op": "validate",
                    "provider": "ghl",
                    "credential_class": "private_token",
                    "material": SECRET,
                },
            ),
            self.broker.handle(
                ROOT, {"op": "revoke", "provider": "ghl", "credential_class": "private_token"}
            ),
            # And the same again over the real socket, as the runtime account.
            self.call({"op": "status", "provider": "ghl", "credential_class": "private_token"}),
        ]
        rendered = json.dumps(replies)
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn(SECRET[:12], rendered)

    def test_a_malformed_or_oversized_frame_is_refused_without_crashing(self) -> None:
        for raw in (
            b"\n",
            b"not json\n",
            b"[]\n",
            b'{"op":"read"}\n',
            b"x" * (MAX_FRAME_BYTES + 10) + b"\n",
        ):
            with self.subTest(raw=raw[:20]):
                reply = self.call(None, raw=raw)
                self.assertFalse(reply["ok"])
        # The server is still serving afterwards.
        self.assertFalse(
            self.call({"op": "status", "provider": "trello", "credential_class": "api_key"})["ok"]
        )

    def test_an_unavailable_broker_is_reported_rather_than_assumed_present(self) -> None:
        from assistant.scotty_business.credential_intake import UnixSocketBroker

        client = UnixSocketBroker(self.socket_path)
        self.assertTrue(client.available())
        self.stop.set()
        self.thread.join(3.0)
        self.server.close()
        self.socket_path.unlink(missing_ok=True)
        self.assertFalse(client.available())
        self.assertFalse(client.validate("trello", "api_key", SECRET))
        self.assertFalse(client.commit("trello", "api_key", SECRET))

    def test_the_client_talks_to_the_real_socket(self) -> None:
        """The production client's own frames, over the production wire.

        `status` is the one operation the runtime account holds, so it is the
        one the container's client can prove end to end. A commit from the same
        client is refused, which is the boundary this deployment depends on.
        """

        from assistant.scotty_business.credential_intake import UnixSocketBroker

        client = UnixSocketBroker(self.socket_path)
        self.assertTrue(client.available())
        self.assertFalse(client.status("trello", "api_key"))
        self.commit_as_root("trello", "api_key", SECRET)
        self.assertTrue(client.status("trello", "api_key"))
        if os.geteuid() != 0:
            self.assertFalse(client.commit("trello", "api_key", OTHER))

    def test_a_restarted_broker_keeps_credentials_but_drops_windows(self) -> None:
        """A restart must not leave a usable window behind on disk."""

        self.commit_as_root("trello", "api_key", SECRET)
        stale = self.broker.handle(
            ROOT, {"op": "open", "provider": "trello", "credential_class": "api_key"}
        )

        self.stop.set()
        self.thread.join(3.0)
        self.server.close()
        restarted = Broker(self.store, actor_uids={RUNTIME_ACTOR: os.getuid()})
        self.server = bind_socket(self.socket_path, group=os.getgid())
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=serve_forever,
            args=(restarted, self.server),
            kwargs={"actor": RUNTIME_ACTOR, "should_stop": self.stop.is_set},
            daemon=True,
        )
        self.thread.start()

        # The credential survived the restart, read over the real socket.
        self.assertTrue(
            self.call({"op": "status", "provider": "trello", "credential_class": "api_key"})["ok"]
        )
        # The window did not: windows live in memory only, so a commit against
        # one opened before the restart has nothing to commit into. The wire
        # turns this into {"ok": false, "state": "no open window"}.
        with self.assertRaises(BrokerError) as refused:
            restarted.handle(
                ROOT,
                {
                    "op": "commit",
                    "provider": "trello",
                    "credential_class": "api_key",
                    "window": stale["window"],
                    "material": OTHER,
                },
            )
        self.assertEqual(str(refused.exception), "no open window")

    def test_a_symlinked_socket_path_is_refused(self) -> None:
        target = self.root / "elsewhere.sock"
        link = self.root / "link.sock"
        link.symlink_to(target)
        with self.assertRaises(BrokerError):
            bind_socket(link, group=os.getgid())


class RuntimeBrokerStatusTests(unittest.TestCase):
    """Guided setup reports what the broker holds, never what it stores."""

    def test_an_unavailable_broker_reports_unavailable_for_every_provider(self) -> None:
        import synthetic  # noqa: F401 - shared synthetic identifiers
        from test_provider_connection import runtime

        from assistant.scotty_business.runtime import BROKER_CREDENTIALS

        with runtime(DISCORD_BOT_TOKEN="synthetic-discord") as live:
            status = live.credential_store_status()
        self.assertEqual(set(status), set(BROKER_CREDENTIALS))
        self.assertEqual(set(status.values()), {"unavailable"})

    def test_google_is_never_asked_of_the_broker(self) -> None:
        from assistant.scotty_business.runtime import BROKER_CREDENTIALS

        self.assertNotIn("google_workspace", BROKER_CREDENTIALS)
        self.assertNotIn("discord", BROKER_CREDENTIALS)

    def test_the_status_reply_carries_no_material(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-broker-status-") as directory:
            root = Path(directory)
            store = CredentialStore(root / "credentials.json")
            broker = Broker(store)
            opened = broker.handle(
                ROOT, {"op": "open", "provider": "rentcast", "credential_class": "api_key"}
            )
            broker.handle(
                ROOT,
                {
                    "op": "commit",
                    "provider": "rentcast",
                    "credential_class": "api_key",
                    "window": opened["window"],
                    "material": SECRET,
                },
            )
            reply = as_runtime(
                broker, {"op": "status", "provider": "rentcast", "credential_class": "api_key"}
            )
            self.assertEqual(reply, {"ok": True, "state": "credential present"})
            self.assertNotIn(SECRET, json.dumps(reply))


def _installed_broker_files() -> tuple[str, ...]:
    """The broker package's file list, taken from install.sh's own array."""

    source = Path("install.sh").read_text(encoding="utf-8")
    block = source.split("readonly -a BROKER_FILES=(", 1)[1].split(")", 1)[0]
    names = tuple(line.strip().strip('"') for line in block.splitlines() if line.strip())
    if not names:  # pragma: no cover - the installer always declares them
        raise AssertionError("the installer declares no broker files")
    return names


def _unprivileged_uid() -> int | None:
    """A real non-root account this process may become, or None."""

    if os.geteuid() != 0:
        return os.geteuid()
    for name in ("nobody", "daemon"):
        try:
            entry = pwd.getpwnam(name)
        except KeyError:
            continue
        if entry.pw_uid != 0:
            return entry.pw_uid
    return None


def _terminate(process: subprocess.Popen[str]) -> None:
    """Stop one spawned broker and close everything it held."""

    with suppress(OSError):
        process.kill()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=10)
    for stream in (process.stdout, process.stderr, process.stdin):
        if stream is not None:
            with suppress(OSError):
                stream.close()


def _drop_to(uid: int) -> Callable[[], None]:
    """Become that account in the child before it executes anything."""

    def drop() -> None:  # pragma: no cover - runs in the forked child
        if os.geteuid() == 0:
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)

    return drop


class PackagedArtefactTests(unittest.TestCase):
    """The installed executable, run as a real process against a real socket."""

    def test_the_broker_executable_refuses_to_run_unprivileged(self) -> None:
        """A genuinely non-root process, refused for the stated reason.

        There is no environment override for the effective uid, because a
        refusal a caller can switch off is not a refusal. Where this process is
        root it drops privileges in the child rather than skipping — running
        the executable as root here would bind the deployment's real socket.

        This runs from a source checkout, where the installed tree does not
        exist, so it also proves the refusal comes before the import of it: an
        executable that imported first would fail with ModuleNotFoundError and
        never say what was actually wrong.
        """

        unprivileged = _unprivileged_uid()
        if unprivileged is None:
            self.skipTest("no unprivileged account is available to drop to")
        with tempfile.TemporaryDirectory(prefix="scotty-broker-refusal-") as directory:
            # Run from a copy the dropped-to account can actually read. A
            # checkout is not necessarily world-readable -- on a CI runner it is
            # not -- and a child that cannot open the file exits 2 from the
            # interpreter without the guard ever running, which says nothing
            # about the guard either way.
            staged = Path(directory) / "scotty-credential-broker"
            staged.write_bytes(Path("scotty-credential-broker").read_bytes())
            staged.chmod(0o755)
            Path(directory).chmod(0o755)
            result = subprocess.run(  # noqa: S603 - fixed interpreter and argument
                [sys.executable, staged.name],
                cwd=directory,
                env={"PATH": "/usr/bin:/bin"},
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
                preexec_fn=_drop_to(unprivileged),  # noqa: PLW1509 - that is the point
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("must run as root", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Permission denied", result.stderr)

    def test_the_packaged_module_serves_a_real_client(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scotty-broker-pkg-") as directory:
            root = Path(directory)
            staged = root / "plugins" / "scotty_broker"
            staged.mkdir(parents=True)
            # Stage exactly what the installer stages, read from the installer
            # itself, so the package and this test cannot drift apart.
            for name in _installed_broker_files():
                (staged / name).write_bytes(Path("assistant/scotty_broker", name).read_bytes())
            socket_path = root / "broker.sock"
            store_path = root / "credentials.json"
            script = (
                "import sys, threading, time\n"
                f"sys.path.insert(0, {str(root / 'plugins')!r})\n"
                "from scotty_broker.broker import (Broker, CredentialStore,"
                " bind_socket, serve_forever)\n"
                f"server = bind_socket({str(socket_path)!r}, group={os.getgid()})\n"
                # The staged package is served with this process as the runtime
                # account, so the client below is genuinely authorized for the
                # runtime operation rather than pretending to be.
                "serve_forever(Broker(CredentialStore("
                f"{str(store_path)!r}), actor_uids={{'runtime': {os.getuid()}}}), server,"
                " actor='runtime')\n"
            )
            process = subprocess.Popen(  # noqa: S603 - fixed interpreter and script
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            # Killing is not reaping. A cleanup that only kills leaves a zombie
            # and two open pipes behind for the rest of the suite to trip over.
            self.addCleanup(_terminate, process)
            deadline = time.monotonic() + 10
            while not socket_path.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(socket_path.exists(), "the staged broker never bound its socket")

            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(5.0)
            client.connect(str(socket_path))
            with client:
                client.sendall(
                    json.dumps(
                        {"op": "status", "provider": "trello", "credential_class": "api_key"}
                    ).encode("utf-8")
                    + b"\n"
                )
                client.shutdown(socket.SHUT_WR)
                reply = json.loads(client.recv(4096).decode("utf-8"))
            self.assertEqual(reply, {"ok": False, "state": "credential absent"})

    def test_the_staged_broker_mints_a_google_token_over_a_real_socket(self) -> None:
        """Client and server, on the wire, in the code the installer stages.

        Both halves of `google_token` were tested and never together: the
        broker's handler by calling it in-process, and the runtime's client by
        reading its reply shape. A frame that one side writes and the other
        cannot read would have passed both.

        This runs the staged package as a separate process, connects with the
        runtime's own client, and checks a token comes back and the material
        that minted it does not.
        """

        from assistant.scotty_business.credential_intake import UnixSocketBroker

        with tempfile.TemporaryDirectory(prefix="scotty-broker-google-") as directory:
            root = Path(directory)
            staged = root / "plugins" / "scotty_broker"
            staged.mkdir(parents=True)
            for name in _installed_broker_files():
                (staged / name).write_bytes(Path("assistant/scotty_broker", name).read_bytes())
            socket_path = root / "broker.sock"
            store_path = root / "credentials.json"

            # Root's own store, written the way setup writes it.
            sys.path.insert(0, str(root / "plugins"))
            self.addCleanup(lambda: sys.path.remove(str(root / "plugins")))
            from scotty_broker.broker import CredentialStore  # type: ignore[import-not-found]

            store = CredentialStore(store_path)
            store.put("google", "client_id", "synthetic-client-id", "shared")
            store.put("google", "client_secret", "synthetic-client-secret", "shared")
            store.put("google", "refresh_token", "synthetic-refresh-operator", "main_operator")

            script = (
                "import sys\n"
                f"sys.path.insert(0, {str(root / 'plugins')!r})\n"
                "from scotty_broker.broker import (Broker, CredentialStore,"
                " bind_socket, serve_forever)\n"
                "from scotty_broker.google import GoogleTokenMinter\n"
                "def exchange(url, fields):\n"
                "    return {'access_token': 'synthetic-access-1', 'expires_in': 3600,\n"
                "            'scope': 'https://www.googleapis.com/auth/drive'}\n"
                f"store = CredentialStore({str(store_path)!r})\n"
                f"server = bind_socket({str(socket_path)!r}, group={os.getgid()})\n"
                "serve_forever(Broker(store, google=GoogleTokenMinter(store, exchange=exchange),"
                f" actor_uids={{'main_operator': {os.getuid()}}}), server, actor='main_operator')\n"
            )
            process = subprocess.Popen(  # noqa: S603 - fixed interpreter and script
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(_terminate, process)
            deadline = time.monotonic() + 10
            while not socket_path.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(socket_path.exists(), "the staged broker never bound its socket")

            broker = UnixSocketBroker(socket_path)
            minted = broker.google_token(("https://www.googleapis.com/auth/drive",))
            self.assertIsNotNone(minted, "the staged broker returned no token")
            assert minted is not None
            access, expires_at = minted
            self.assertEqual(access, "synthetic-access-1")
            self.assertGreater(expires_at, 0)

            # Nothing that could mint another crossed the socket. Read the raw
            # frame too, so this does not rest on the client's own parsing.
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(5.0)
            client.connect(str(socket_path))
            with client:
                client.sendall(
                    json.dumps(
                        {
                            "op": "google_token",
                            "scopes": ["https://www.googleapis.com/auth/drive"],
                        }
                    ).encode("utf-8")
                    + b"\n"
                )
                client.shutdown(socket.SHUT_WR)
                raw = client.recv(4096).decode("utf-8")
            for secret in (
                "synthetic-refresh-operator",
                "synthetic-client-secret",
                "synthetic-client-id",
            ):
                self.assertNotIn(secret, raw)

    def test_the_broker_lives_outside_every_container_writable_path(self) -> None:
        """The container owns /srv/Scotty/data, so root must not import from it."""

        installer = Path("install.sh").read_text(encoding="utf-8")
        executable = Path("scotty-credential-broker").read_text(encoding="utf-8")

        self.assertIn("readonly BROKER_DIR=/usr/local/lib/scotty/scotty_broker", installer)
        self.assertIn('sys.path.insert(0, "/usr/local/lib/scotty")', executable)
        self.assertNotIn("/srv/Scotty/data", executable)
        # The installer proves this for every privileged package it lays down,
        # so the guarantee cannot be true of the broker and quietly untrue of
        # the host supervisor sitting beside it.
        self.assertIn("for privileged in scotty_broker scotty_supervisor; do", installer)
        self.assertIn("must never sit inside the container-writable data mount", installer)

    def test_the_runtime_directory_survives_a_broker_restart(self) -> None:
        """Removing /run/scotty would pin the container's mount to a dead inode."""

        unit = Path("broker/scotty-credential-broker.service").read_text(encoding="utf-8")
        self.assertIn("RuntimeDirectoryPreserve=yes", unit)

    def test_the_broker_is_never_staged_into_a_client_profile(self) -> None:
        installer = Path("install.sh").read_text(encoding="utf-8")
        self.assertIn("scotty-credential-broker.service", installer)
        self.assertIn("install_broker_file", installer)
        self.assertIn("-o root -g root -m 0644", installer)
        self.assertNotIn('install_broker_file "$broker_file" "${PROFILES_DIR}', installer)
        self.assertIn("a client profile must never carry ${privileged}", installer)

    def test_the_unit_keeps_the_broker_bounded(self) -> None:
        unit = Path("broker/scotty-credential-broker.service").read_text(encoding="utf-8")
        for directive in (
            "NoNewPrivileges=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "RestrictAddressFamilies=AF_UNIX",
            "ReadWritePaths=/run/scotty /var/lib/scotty",
            "StateDirectoryMode=0700",
        ):
            with self.subTest(directive=directive):
                self.assertIn(directive, unit)
        self.assertNotIn("PrivateNetwork=false", unit)

    def test_the_container_mounts_only_the_broker_runtime_directory(self) -> None:
        compose = Path("compose.yaml").read_text(encoding="utf-8")
        self.assertIn("source: /run/scotty", compose)
        self.assertIn("target: /run/scotty", compose)
        # The store itself is never mounted anywhere.
        self.assertNotIn("/var/lib/scotty", compose)
        self.assertEqual(compose.count("create_host_path: false"), 2)


if __name__ == "__main__":
    unittest.main()
