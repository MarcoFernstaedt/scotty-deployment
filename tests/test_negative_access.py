"""What each account the deployment creates can actually reach, tried for real.

Every other test in this suite reasons about authority from inside one process.
This one does not: it forks, drops to each effective uid and gid the installed
namespace uses, and has that child try to open the files and connect to the
sockets that hold credential material. What it reports is what the kernel
actually did, not what the code intended.

That distinction matters here because the claim being checked is a negative
one. "The runtime cannot read the store" is not established by the runtime not
calling `open`; it is established by `open` failing when it does.

These need root, because only root can drop to an arbitrary account. Where
that is not available the proof is unobtainable rather than passed, so they
skip and say so.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from assistant.scotty_broker.broker import (
    ACTOR_UIDS,
    RUNTIME_UID,
    CredentialStore,
    bind_actor_socket,
    bind_control_socket,
)

#: A uid nothing in this deployment runs as, standing in for "some other
#: process on the host". It must fare no better than the service accounts.
STRANGER_UID = 65_534

#: Every effective account in the installed namespace, and the group each one
#: runs with. The actor groups are per actor by construction; the runtime
#: container's account is in none of them.
SERVICE_ACCOUNTS: dict[str, tuple[int, int]] = {
    "runtime": (RUNTIME_UID, RUNTIME_UID),
    **{actor: (uid, uid) for actor, uid in ACTOR_UIDS.items()},
    "stranger": (STRANGER_UID, STRANGER_UID),
}


def _in_child(uid: int, gid: int, work) -> str:
    """Run `work` as that account in a forked child and bring back its answer.

    A fork rather than a thread, because dropping privilege is per process and
    is not something to do to the test runner. The child never raises out: it
    writes one line and exits, so a crash in the work reads as a failure of the
    work rather than as a hang.
    """

    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns to the runner
        os.close(read_fd)
        code = 0
        try:
            os.setgroups([gid])
            os.setgid(gid)
            os.setuid(uid)
            answer = work()
        except BaseException as exc:  # noqa: BLE001 - the answer is the report
            answer = f"{type(exc).__name__}: {exc}"
            code = 0
        try:
            os.write(write_fd, str(answer).encode("utf-8", "replace")[:4096])
        finally:
            os.close(write_fd)
        os._exit(code)
    os.close(write_fd)
    chunks: list[bytes] = []
    with os.fdopen(read_fd, "rb") as stream:
        chunks.append(stream.read())
    os.waitpid(pid, 0)
    return b"".join(chunks).decode("utf-8", "replace")


def _read(path: Path):
    def work() -> str:
        return "READ:" + path.read_text(encoding="utf-8")[:200]

    return work


def _connect(path: Path):
    def work() -> str:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(path))
        finally:
            client.close()
        return "CONNECTED"

    return work


@unittest.skipUnless(os.geteuid() == 0, "dropping to another account needs root")
class InstalledNamespaceTests(unittest.TestCase):
    """The installed layout, built by the code that installs it, then probed."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="scotty-negative-access-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        # Root's own tree, at the modes the deployment writes. Built through
        # the store itself rather than by hand, so what is probed is what the
        # product actually creates.
        self.store_path = self.root / "credentials.json"
        store = CredentialStore(self.store_path)
        store.put("google", "client_secret", "synthetic-client-secret", "shared")
        store.put("google", "refresh_token", "synthetic-refresh-operator", "main_operator")
        store.put("discord", "bot_token", "synthetic-discord-bot-token", "shared")
        # The Discord route map: who a channel belongs to. Not material, but
        # deciding it is exactly the authority the runtime must not have.
        self.routes_path = self.root / "routes.json"
        self.routes_path.write_text(
            json.dumps([{"channel_id": "8" * 18, "user_id": "7" * 18, "actor": "main_operator"}]),
            encoding="utf-8",
        )
        os.chmod(self.routes_path, 0o600)
        self.root.chmod(0o755)

    def test_no_service_account_can_read_the_credential_store(self) -> None:
        for name, (uid, gid) in SERVICE_ACCOUNTS.items():
            with self.subTest(account=name):
                answer = _in_child(uid, gid, _read(self.store_path))
                # Not "did not happen to read it" -- the kernel said no.
                self.assertNotIn("READ:", answer)
                self.assertTrue(
                    answer.startswith("PermissionError"),
                    f"{name} got {answer!r} rather than a refusal",
                )

    def test_the_stored_material_never_appears_in_any_account_s_answer(self) -> None:
        for name, (uid, gid) in SERVICE_ACCOUNTS.items():
            for target in (self.store_path, self.routes_path):
                with self.subTest(account=name, target=target.name):
                    answer = _in_child(uid, gid, _read(target))
                    for secret in (
                        "synthetic-client-secret",
                        "synthetic-refresh-operator",
                        "synthetic-discord-bot-token",
                    ):
                        self.assertNotIn(secret, answer)

    def test_no_service_account_can_reach_the_control_socket(self) -> None:
        """The socket that opens windows, grants, and approves. Root only.

        Reachability is the whole question: a peer check inside the broker is a
        second lock, and this is the first one -- nothing else can even
        connect, so the peer check is never the only thing standing there.
        """

        path = self.root / "control.sock"
        server = bind_control_socket(path)
        self.addCleanup(server.close)
        for name, (uid, gid) in SERVICE_ACCOUNTS.items():
            with self.subTest(account=name):
                answer = _in_child(uid, gid, _connect(path))
                self.assertNotEqual(answer, "CONNECTED")

    def test_an_actor_socket_admits_that_actor_and_refuses_the_others(self) -> None:
        """Per-actor sockets, owned by per-actor groups, probed from each.

        This is the topology the broker's authority model rests on where it is
        available: the employee's worker cannot ask as the main operator
        because it cannot open that socket, not because it is told no.
        """

        sockets: dict[str, Path] = {}
        for actor, uid in ACTOR_UIDS.items():
            path = self.root / f"{actor}.sock"
            server = bind_actor_socket(actor, group=uid, path=path)
            self.addCleanup(server.close)
            sockets[actor] = path

        for owner, path in sockets.items():
            for name, (uid, gid) in SERVICE_ACCOUNTS.items():
                expected = "CONNECTED" if name == owner else "refused"
                with self.subTest(socket=owner, account=name, expected=expected):
                    answer = _in_child(uid, gid, _connect(path))
                    if name == owner:
                        self.assertEqual(answer, "CONNECTED")
                    else:
                        self.assertNotEqual(answer, "CONNECTED")

    def test_the_container_s_own_account_reaches_no_actor_socket(self) -> None:
        """The account the pinned single-gateway runtime runs as, specifically.

        It is the one account a compromise of the container actually gets, and
        the one that must not be able to ask as a person. It has its own socket
        for that, on which nothing is answered until Discord says who wrote the
        cited message.
        """

        for actor, uid in ACTOR_UIDS.items():
            path = self.root / f"only-{actor}.sock"
            server = bind_actor_socket(actor, group=uid, path=path)
            self.addCleanup(server.close)
            with self.subTest(actor=actor):
                answer = _in_child(RUNTIME_UID, RUNTIME_UID, _connect(path))
                self.assertNotEqual(answer, "CONNECTED")


if __name__ == "__main__":
    unittest.main()


class DeclaredExposureTests(unittest.TestCase):
    """What the container is knowingly allowed to hold, and nothing more.

    Not a root check, so it runs everywhere: the point is that the list of
    exposures in the contract is the list the code produces, rather than prose
    somebody updated once.
    """

    def test_the_contract_names_exactly_what_reaches_the_container(self) -> None:
        from pathlib import Path as _Path

        from assistant.scotty_business.setup import CONTAINER_ENVIRONMENT_REASONS

        contract = _Path("docs/scotty-basic-release-engineering-contract.md").read_text(
            encoding="utf-8"
        )
        section = contract[contract.index("## What is not isolated") :]
        section = section[: section.index("## Live acceptance")]
        # Every environment secret the container gets is named there...
        self.assertIn("DISCORD_BOT_TOKEN", section)
        for name in CONTAINER_ENVIRONMENT_REASONS:
            with self.subTest(name=name):
                self.assertTrue(
                    name in section or "model provider key" in section,
                    f"{name} reaches the container and the contract does not say so",
                )
        # ...and the short-lived Google token, which is not an environment
        # variable and so would otherwise go unlisted.
        self.assertIn("access token", section.casefold())

    def test_the_contract_does_not_claim_the_pinned_exposures_are_closed(self) -> None:
        from pathlib import Path as _Path

        contract = (
            _Path("docs/scotty-basic-release-engineering-contract.md")
            .read_text(encoding="utf-8")
            .casefold()
        )
        self.assertIn("cannot be closed under the pinned image", contract)
