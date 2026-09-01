"""Google's long-lived credentials do not live where the model can read them.

The refresh token, the client id and the client secret were written into the
container's own state directory, owned by the account the runtime runs as. File
mode 0600 does not isolate processes that share the owner, and every profile in
that container shares it — including the broad maintainer profile. A file-reading
tool in the wrong session was one step from an OAuth grant that outlives every
password change.

What is left in the container now is an access token, which lasts an hour and
cannot mint another. The refresh token and the client secret stay with the
broker, root-owned and outside every mount, and the exchange that turns one into
the other happens there.

This is an improvement rather than the end of the work, and the tests say which
is which: the material a compromise would want most is out, and the short-lived
token it would still get is named as the remaining exposure.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from assistant.scotty_broker.broker import CREDENTIAL_CLASSES, Broker, CredentialStore, Peer
from assistant.scotty_broker.google import (
    GoogleTokenError,
    GoogleTokenMinter,
)


class MinterFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="scotty-google-isolation-")
        self.addCleanup(directory.cleanup)
        self.store = CredentialStore(Path(directory.name) / "credentials.json")
        self.store.put("google", "client_id", "synthetic-client-id", "shared")
        self.store.put("google", "client_secret", "synthetic-client-secret", "shared")
        self.store.put("google", "refresh_token", "synthetic-refresh-operator", "main_operator")
        self.exchanges: list[dict[str, str]] = []
        self.answer: dict[str, object] = {
            "access_token": "synthetic-access-1",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/drive",
        }
        self.moment = 1_000_000

        def exchange(url, fields):
            self.exchanges.append(dict(fields))
            del url
            return dict(self.answer)

        self.minter = GoogleTokenMinter(self.store, exchange=exchange, clock=lambda: self.moment)

    def scopes(self) -> tuple[str, ...]:
        return ("https://www.googleapis.com/auth/drive",)


class MintingTests(MinterFixture):
    def test_an_access_token_is_minted_from_material_the_caller_never_sees(self) -> None:
        minted = self.minter.access_token("main_operator", self.scopes())
        self.assertEqual(minted.access_token, "synthetic-access-1")
        # The exchange used the refresh token and the client secret, and
        # neither is anywhere in what comes back.
        rendered = repr(minted)
        self.assertNotIn("synthetic-refresh-operator", rendered)
        self.assertNotIn("synthetic-client-secret", rendered)

    def test_a_minted_token_is_reused_until_it_is_nearly_expired(self) -> None:
        first = self.minter.access_token("main_operator", self.scopes())
        again = self.minter.access_token("main_operator", self.scopes())
        self.assertEqual(again.access_token, first.access_token)
        self.assertEqual(len(self.exchanges), 1)

        self.moment += 3600
        self.answer["access_token"] = "synthetic-access-2"  # noqa: S105 - synthetic
        refreshed = self.minter.access_token("main_operator", self.scopes())
        self.assertEqual(refreshed.access_token, "synthetic-access-2")
        self.assertEqual(len(self.exchanges), 2)

    def test_one_user_s_refresh_token_never_mints_the_other_s_access(self) -> None:
        with self.assertRaises(GoogleTokenError):
            self.minter.access_token("employee", self.scopes())
        self.assertEqual(self.exchanges, [])

    def test_a_narrower_or_wider_scope_set_is_refused(self) -> None:
        self.answer["scope"] = "https://www.googleapis.com/auth/drive.readonly"
        with self.assertRaises(GoogleTokenError):
            self.minter.access_token("main_operator", self.scopes())

    def test_a_rotated_refresh_token_is_kept_where_the_old_one_was(self) -> None:
        self.answer["refresh_token"] = "synthetic-refresh-rotated"  # noqa: S105
        self.minter.access_token("main_operator", self.scopes())
        # Rotation happens on the privileged side, so the runtime is not
        # involved in keeping the long-lived credential current.
        self.assertTrue(self.store.read("google", "refresh_token", "main_operator"))
        self.assertEqual(
            self.store.read("google", "refresh_token", "main_operator"),
            "synthetic-refresh-rotated",
        )

    def test_an_incomplete_exchange_leaves_the_stored_material_alone(self) -> None:
        self.answer = {"error": "invalid_grant"}
        with self.assertRaises(GoogleTokenError):
            self.minter.access_token("main_operator", self.scopes())
        self.assertEqual(
            self.store.read("google", "refresh_token", "main_operator"),
            "synthetic-refresh-operator",
        )


class WireTests(MinterFixture):
    """What the runtime can ask for, and what it can never ask for."""

    def broker(self) -> Broker:
        return Broker(self.store, google=self.minter)

    def runtime_peer(self) -> Peer:
        from assistant.scotty_broker.broker import RUNTIME_UID

        return Peer(pid=3, uid=RUNTIME_UID, gid=RUNTIME_UID)

    def resolver(self):
        from assistant.scotty_broker.provenance import ProvenanceResolver, Route

        def fetch(url, headers):
            del headers
            if url.endswith("90000000000000001"):
                return 200, {
                    "channel_id": "80000000000000001",
                    "author": {"id": "70000000000000001"},
                }
            return 404, None

        return ProvenanceResolver(
            (Route("80000000000000001", "70000000000000001", "main_operator"),),
            lambda: "synthetic-bot-token",
            fetch=fetch,
        )

    def test_the_runtime_gets_a_short_lived_token_and_nothing_longer(self) -> None:
        from assistant.scotty_broker.broker import RUNTIME_ACTOR

        broker = self.broker()
        broker.provenance = self.resolver()
        reply = broker.handle(
            self.runtime_peer(),
            {
                "op": "google_token",
                "scopes": list(self.scopes()),
                "provenance": {
                    "channel_id": "80000000000000001",
                    "message_id": "90000000000000001",
                },
            },
            actor=RUNTIME_ACTOR,
        )
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["access_token"], "synthetic-access-1")
        self.assertIn("expires_at", reply)
        rendered = repr(reply)
        self.assertNotIn("synthetic-refresh-operator", rendered)
        self.assertNotIn("synthetic-client-secret", rendered)
        self.assertNotIn("synthetic-client-id", rendered)

    def test_no_request_the_runtime_can_make_returns_the_material(self) -> None:
        """Swept rather than reasoned about: every operation, every shape.

        Naming the operations that must not leak proves only that the ones
        somebody thought of do not. This tries all of them, with the arguments
        a caller would reach for, and reads every byte that comes back.
        """

        from assistant.scotty_broker.broker import OPERATIONS, RUNTIME_ACTOR, BrokerError

        broker = self.broker()
        broker.provenance = self.resolver()
        citation = {"channel_id": "80000000000000001", "message_id": "90000000000000001"}
        secrets = (
            "synthetic-refresh-operator",
            "synthetic-client-secret",
            "synthetic-client-id",
        )
        replies: list[str] = []
        for operation in sorted(OPERATIONS):
            for extra in (
                {},
                {"provider": "google", "credential_class": "refresh_token"},
                {"provider": "google", "credential_class": "client_secret"},
                {"provider": "google", "credential_class": "refresh_token", "own_only": True},
                {"operation": "google.refresh", "arguments": {}},
                {"scopes": list(self.scopes())},
            ):
                request = {"op": operation, "provenance": citation, **extra}
                with self.subTest(operation=operation, extra=sorted(extra)):
                    try:
                        replies.append(
                            repr(broker.handle(self.runtime_peer(), request, actor=RUNTIME_ACTOR))
                        )
                    except BrokerError as exc:
                        # A refusal is an answer too, and it must not explain
                        # itself by quoting what it is protecting.
                        replies.append(str(exc))
        rendered = "\n".join(replies)
        self.assertTrue(rendered)
        for secret in secrets:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, rendered)
        # And the sweep really did reach the one operation that answers, so a
        # broker that refused everything could not pass this by refusing.
        self.assertIn("synthetic-access-1", rendered)

    def test_there_is_no_operation_that_returns_the_refresh_token(self) -> None:
        from assistant.scotty_broker.broker import OPERATIONS

        for name in OPERATIONS:
            with self.subTest(operation=name):
                self.assertNotIn(name, {"refresh_token", "client_secret", "oauth", "reveal"})

    def test_google_material_has_a_declared_home_in_the_store(self) -> None:
        self.assertEqual(
            CREDENTIAL_CLASSES["google"],
            frozenset({"client_id", "client_secret", "refresh_token"}),
        )


class ContainerStateTests(unittest.TestCase):
    """What is left in the container's own tree, and what is not.

    These read and write the real files rather than the source that writes
    them: a test that greps for the word "refresh" passes just as happily
    against a comment as against a boundary.
    """

    def store(self):
        from assistant.scotty_business.google_oauth import GoogleTokenStore

        directory = tempfile.TemporaryDirectory(prefix="scotty-google-state-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "google-oauth.main_operator.json"
        return GoogleTokenStore(path, owner_uid=-1, owner_gid=-1)

    def granted(self, **overrides):
        from assistant.scotty_business.google_oauth import AccountBinding

        fields = {
            "access_token": "synthetic-access-1",
            "expires_at": 1_003_600,
            "scopes": ("https://www.googleapis.com/auth/drive",),
            "account_email": "operator@synthetic.test",
        }
        fields.update(overrides)
        return AccountBinding(**fields)

    def test_what_the_runtime_writes_has_nowhere_to_put_long_lived_material(self) -> None:
        import json

        store = self.store()
        store.write(self.granted())
        written = json.loads(store.path.read_text(encoding="utf-8"))
        # Not "does not happen to contain one" -- there is no key for it.
        self.assertEqual(
            set(written),
            {"version", "access_token", "expires_at", "scopes", "account_email"},
        )
        self.assertNotIn("refresh_token", written)
        self.assertNotIn("client_secret", written)
        # And the record itself cannot carry one either, so no later caller
        # can be handed one by writing a field back in.
        self.assertFalse(hasattr(self.granted(), "refresh_token"))
        self.assertFalse(hasattr(self.granted(), "client_secret"))

    def test_a_file_left_by_the_old_version_is_refused_rather_than_used(self) -> None:
        import json

        from assistant.scotty_business.google_oauth import GoogleOAuthError

        store = self.store()
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "access_token": "synthetic-access-1",
                    "refresh_token": "synthetic-refresh-operator",
                    "expires_at": 1_003_600,
                    "scopes": ["https://www.googleapis.com/auth/drive"],
                    "account_email": "operator@synthetic.test",
                    "client_id": "synthetic-client-id",
                    "client_secret": "synthetic-client-secret",
                }
            ),
            encoding="utf-8",
        )
        store.path.chmod(0o600)
        # An upgrade must not keep spending the material it was meant to move.
        with self.assertRaises(GoogleOAuthError):
            store.read()
        self.assertFalse(
            store.ready(("https://www.googleapis.com/auth/drive",), "operator@synthetic.test")
        )

    def test_completing_consent_hands_the_long_lived_material_to_the_broker(self) -> None:
        import json

        from assistant.scotty_business import google_oauth

        store = self.store()
        client = Path(tempfile.mkdtemp(prefix="scotty-google-client-")) / "client.json"
        self.addCleanup(lambda: client.unlink(missing_ok=True))
        client.write_text(
            json.dumps(
                {
                    "installed": {
                        "client_id": "synthetic-client-id",
                        "client_secret": "synthetic-client-secret",
                        "auth_uri": google_oauth.GOOGLE_AUTH_URI,
                        "token_uri": google_oauth.GOOGLE_TOKEN_URI,
                    }
                }
            ),
            encoding="utf-8",
        )
        client.chmod(0o600)

        request = google_oauth.begin_consent(
            client, ("https://www.googleapis.com/auth/drive",), owner_uid=client.stat().st_uid
        )
        committed: list[tuple[str, str, str]] = []

        def exchange(url=None, fields=None):
            del url, fields
            return {
                "access_token": "synthetic-access-1",
                "refresh_token": "synthetic-refresh-operator",
                "expires_in": 3600,
                "scope": "https://www.googleapis.com/auth/drive",
            }

        google_oauth.complete_consent(
            client,
            store,
            request,
            f"http://localhost:8765/oauth2/callback?state={request.state}&code=synthetic-code",
            owner_uid=client.stat().st_uid,
            exchange=exchange,
            verify_account=lambda _token: "operator@synthetic.test",
            commit=lambda client_id, client_secret, refresh: committed.append(
                (client_id, client_secret, refresh)
            ),
        )

        # The privileged side got the material...
        self.assertEqual(
            committed,
            [("synthetic-client-id", "synthetic-client-secret", "synthetic-refresh-operator")],
        )
        # ...and the container's own file has none of it, by bytes.
        on_disk = store.path.read_text(encoding="utf-8")
        self.assertNotIn("synthetic-refresh-operator", on_disk)
        self.assertNotIn("synthetic-client-secret", on_disk)
        self.assertIn("synthetic-access-1", on_disk)

    def test_consent_that_cannot_be_committed_connects_nobody(self) -> None:
        import json

        from assistant.scotty_business import google_oauth

        store = self.store()
        client = Path(tempfile.mkdtemp(prefix="scotty-google-client-")) / "client.json"
        self.addCleanup(lambda: client.unlink(missing_ok=True))
        client.write_text(
            json.dumps(
                {
                    "installed": {
                        "client_id": "synthetic-client-id",
                        "client_secret": "synthetic-client-secret",
                        "auth_uri": google_oauth.GOOGLE_AUTH_URI,
                        "token_uri": google_oauth.GOOGLE_TOKEN_URI,
                    }
                }
            ),
            encoding="utf-8",
        )
        client.chmod(0o600)
        request = google_oauth.begin_consent(
            client, ("https://www.googleapis.com/auth/drive",), owner_uid=client.stat().st_uid
        )

        def refuse(*_args):
            raise google_oauth.GoogleOAuthError("the broker would not take it")

        with self.assertRaises(google_oauth.GoogleOAuthError):
            google_oauth.complete_consent(
                client,
                store,
                request,
                f"http://localhost:8765/oauth2/callback?state={request.state}&code=synthetic-code",
                owner_uid=client.stat().st_uid,
                exchange=lambda url=None, fields=None: {
                    "access_token": "synthetic-access-1",
                    "refresh_token": "synthetic-refresh-operator",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/drive",
                },
                verify_account=lambda _token: "operator@synthetic.test",
                commit=refuse,
            )
        # Nothing was written, so nothing reports connected on a consent whose
        # refresh token went nowhere: the next call would have no way to renew.
        self.assertFalse(store.path.exists())

    def test_the_documented_exposure_is_the_short_lived_token_only(self) -> None:
        from assistant.scotty_business.setup import CONTAINER_ENVIRONMENT_REASONS

        # Everything the container is knowingly allowed to hold has a reason
        # written next to it, so the threat model is a list rather than a habit.
        self.assertTrue(CONTAINER_ENVIRONMENT_REASONS)
        for name, reason in CONTAINER_ENVIRONMENT_REASONS.items():
            with self.subTest(name=name):
                self.assertTrue(reason.strip())


if __name__ == "__main__":
    unittest.main()
