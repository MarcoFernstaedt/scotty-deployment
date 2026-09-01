"""No raw provider secret is reachable from the model-visible runtime.

The container's data directory is bind-mounted read-write and owned by the same
account the runtime runs as, so anything written there is readable by every
profile in that container — including Marco's broad maintainer profile. That
makes the data tree the wrong place for a credential, whatever its file mode.

These tests are the boundary: what setup writes there, what the runtime asks
for, and what a file-reading tool inside the container could find.
"""

from __future__ import annotations

import unittest

import synthetic

from assistant.scotty_business.setup import (
    CONTAINER_ENVIRONMENT_NAMES,
    SetupInputs,
    runtime_environment,
)

#: Distinctive synthetic values, so a leak is unambiguous wherever it appears.
SECRETS = {
    "DISCORD_BOT_TOKEN": "synthetic-discord-token-aaaa",
    "SCOTTY_TRELLO_API_KEY": "synthetic-trello-key-bbbb",
    "SCOTTY_TRELLO_TOKEN": "synthetic-trello-token-cccc",
    "SCOTTY_GHL_PRIVATE_TOKEN": "synthetic-ghl-token-dddd",
    "SCOTTY_RENTCAST_API_KEY": "synthetic-rentcast-key-eeee",
    "SCOTTY_TRELLO_TOKEN_EMPLOYEE": "synthetic-trello-token-ffff",
    "OPENROUTER_API_KEY": "synthetic-model-key-gggg",
}


def inputs(**overrides: object) -> SetupInputs:
    body: dict[str, object] = {
        "model_provider": "openrouter",
        "model_name": "synthetic/model",
        "guild_id": synthetic.CLIENT_GUILD,
        "operator_channel_id": synthetic.OPERATOR_CHANNEL,
        "operator_user_id": synthetic.OPERATOR_USER,
        "employee_channel_id": synthetic.EMPLOYEE_CHANNEL,
        "employee_user_id": synthetic.EMPLOYEE_USER,
        "route_guild_id": synthetic.ROUTE_GUILD,
        "route_channel_id": synthetic.ROUTE_CHANNEL,
        "route_user_id": synthetic.ROUTE_USER,
        "secrets": dict(SECRETS),
    }
    body.update(overrides)
    return SetupInputs(**body)  # type: ignore[arg-type]


class ContainerEnvironmentTests(unittest.TestCase):
    def test_only_what_the_pinned_runtime_itself_needs_reaches_the_container(self) -> None:
        environment = runtime_environment(inputs())
        reached = set(environment) & set(SECRETS)
        self.assertLessEqual(reached, set(CONTAINER_ENVIRONMENT_NAMES))
        # Everything the fixture supplies that is allowed through is through.
        self.assertEqual(reached, set(SECRETS) & set(CONTAINER_ENVIRONMENT_NAMES))

    def test_no_provider_credential_is_written_where_the_model_can_read_it(self) -> None:
        rendered = "\n".join(
            f"{name}={value}" for name, value in runtime_environment(inputs()).items()
        )
        for name, value in SECRETS.items():
            if name in CONTAINER_ENVIRONMENT_NAMES:
                continue
            with self.subTest(secret=name):
                self.assertNotIn(value, rendered)

    def test_the_provider_credentials_that_stay_out_are_named_explicitly(self) -> None:
        # Trello, GoHighLevel and RentCast have no business in the container.
        for name in (
            "SCOTTY_TRELLO_API_KEY",
            "SCOTTY_TRELLO_TOKEN",
            "SCOTTY_GHL_PRIVATE_TOKEN",
            "SCOTTY_RENTCAST_API_KEY",
            "SCOTTY_TRELLO_TOKEN_EMPLOYEE",
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, CONTAINER_ENVIRONMENT_NAMES)

    def test_the_model_credential_the_pinned_runtime_needs_is_documented(self) -> None:
        # The pinned runtime dispatches to the model itself, so its key is in
        # the container by necessity. That is stated rather than hidden.
        from assistant.scotty_business.setup import CONTAINER_ENVIRONMENT_REASONS

        for name in CONTAINER_ENVIRONMENT_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, CONTAINER_ENVIRONMENT_REASONS)
                self.assertTrue(CONTAINER_ENVIRONMENT_REASONS[name].strip())


class BrokerCommitTests(unittest.TestCase):
    def test_setup_commits_every_withheld_credential_to_the_broker(self) -> None:
        from assistant.scotty_business.setup import broker_commitments

        withheld = {
            name: value
            for name, value in SECRETS.items()
            if name not in CONTAINER_ENVIRONMENT_NAMES
        }
        commitments = dict(broker_commitments(inputs()))
        self.assertEqual(set(commitments), set(withheld))
        for name, commitment in commitments.items():
            with self.subTest(name=name):
                self.assertEqual(commitment.material, withheld[name])
                self.assertTrue(commitment.provider)
                self.assertTrue(commitment.credential_class)
                self.assertIn(commitment.actor, {"shared", "main_operator", "employee"})
        # The per-actor token is addressed to that actor, not to the shared one.
        self.assertEqual(commitments["SCOTTY_TRELLO_TOKEN_EMPLOYEE"].actor, "employee")
        self.assertEqual(commitments["SCOTTY_TRELLO_TOKEN"].actor, "shared")

    def test_a_commitment_never_renders_its_material(self) -> None:
        from assistant.scotty_business.setup import broker_commitments

        commitments = broker_commitments(inputs())
        rendered = " ".join(repr(item) for item in commitments.values())
        for value in SECRETS.values():
            self.assertNotIn(value, rendered)

    def test_a_secret_with_no_broker_address_is_refused_not_written_out(self) -> None:
        from assistant.scotty_business.setup import SetupError, broker_commitments

        with self.assertRaises(SetupError):
            broker_commitments(inputs(secrets={**SECRETS, "SCOTTY_MYSTERY_KEY": "x" * 20}))


class BrokerStoreTests(unittest.TestCase):
    """The store the credentials actually land in, addressed per actor."""

    def store(self):
        import tempfile
        from pathlib import Path

        from assistant.scotty_broker.broker import CredentialStore

        directory = tempfile.TemporaryDirectory(prefix="scotty-secret-store-")
        self.addCleanup(directory.cleanup)
        return CredentialStore(Path(directory.name) / "credentials.json")

    def test_setup_commits_everything_withheld_and_nothing_else(self) -> None:
        import os
        import tempfile
        from pathlib import Path

        from assistant.scotty_business.setup import commit_to_broker

        with tempfile.TemporaryDirectory(prefix="scotty-commit-") as directory:
            path = Path(directory) / "credentials.json"
            stored = commit_to_broker(inputs(), store_path=path, owner_uid=os.geteuid())
            self.assertEqual(set(stored), set(SECRETS) - set(CONTAINER_ENVIRONMENT_NAMES))
            body = path.read_text(encoding="utf-8")
            # The material is in the root-only store and nowhere the runtime
            # can read, but it is genuinely there.
            self.assertIn(SECRETS["SCOTTY_TRELLO_API_KEY"], body)
            self.assertNotIn(SECRETS["DISCORD_BOT_TOKEN"], body)

    def test_one_actors_slot_is_not_reachable_through_another(self) -> None:
        store = self.store()
        store.put("trello", "token", "operator-material", "main_operator")
        store.put("trello", "token", "shared-material", "shared")

        self.assertTrue(store.present("trello", "token", "main_operator"))
        self.assertTrue(store.present("trello", "token", "shared"))
        # The employee has no token of their own, and cannot find one.
        self.assertFalse(store.present("trello", "token", "employee"))
        # Dropping one actor's leaves the others exactly as they were.
        self.assertTrue(store.drop("trello", "token", "main_operator"))
        self.assertFalse(store.present("trello", "token", "main_operator"))
        self.assertTrue(store.present("trello", "token", "shared"))

    def test_a_window_opened_for_one_actor_cannot_commit_into_another(self) -> None:
        from assistant.scotty_broker.broker import Broker, BrokerError, Peer

        broker = Broker(self.store())
        root = Peer(pid=1, uid=0, gid=0)
        opened = broker.handle(
            root,
            {
                "op": "open",
                "provider": "trello",
                "credential_class": "token",
                "actor": "employee",
            },
        )
        with self.assertRaises(BrokerError):
            broker.handle(
                root,
                {
                    "op": "commit",
                    "provider": "trello",
                    "credential_class": "token",
                    "actor": "main_operator",
                    "window": opened["window"],
                    "material": "synthetic-material-0001",
                },
            )

    def test_an_unknown_actor_is_refused(self) -> None:
        from assistant.scotty_broker.broker import Broker, BrokerError, Peer

        broker = Broker(self.store())
        for actor in ("maintainer", "root", "", "../shared", "MAIN_OPERATOR"):
            with self.subTest(actor=actor), self.assertRaises(BrokerError):
                broker.handle(
                    Peer(pid=1, uid=0, gid=0),
                    {
                        "op": "status",
                        "provider": "trello",
                        "credential_class": "token",
                        "actor": actor,
                    },
                )


if __name__ == "__main__":
    unittest.main()
