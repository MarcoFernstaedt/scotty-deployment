"""Each client user reaches only their own provider identity.

Broad OAuth consent and a shared bot token are not shared authority. The actor
behind a tool call decides which Google account, which Trello token, and which
GHL identity that call may use, and the actor is resolved from Discord
provenance the model cannot influence.
"""

from __future__ import annotations

import unittest

import synthetic

from assistant.scotty_business.config import RuntimeConfig
from assistant.scotty_business.policy import Principal, Role
from assistant.scotty_business.provider_identity import (
    ProviderIdentityError,
    ProviderIdentityResolver,
    reject_identity_override,
)

GOOGLE_SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/contacts",
]

BOTH_ACCOUNTS = {
    "main_operator": {
        "account_email": "operator.synthetic@example.invalid",
        "oauth_scopes": GOOGLE_SCOPES,
    },
    "employee": {
        "account_email": "employee.synthetic@example.invalid",
        "oauth_scopes": GOOGLE_SCOPES,
    },
}


def config(**overrides: object) -> RuntimeConfig:
    return RuntimeConfig.from_mapping(
        synthetic.private_mapping(google_workspace=BOTH_ACCOUNTS, **overrides)
    )


def principal(role: Role) -> Principal:
    """One configured route, as somebody who has just said something.

    A principal with no citation is nobody in particular now: the broker
    settles identity from the message, so a test that asks "what can this
    person reach" has to give it one.
    """

    from dataclasses import replace

    configured = config().principal_for(role)
    return replace(configured, message_id="9" + configured.channel_id[1:])


class PerActorGoogleIdentityTests(unittest.TestCase):
    def test_each_client_user_resolves_to_their_own_account_and_token_file(self) -> None:
        resolver = ProviderIdentityResolver(config())
        operator = resolver.resolve(principal(Role.MAIN_OPERATOR))
        employee = resolver.resolve(principal(Role.EMPLOYEE))

        self.assertEqual(operator.google_account, "operator.synthetic@example.invalid")
        self.assertEqual(employee.google_account, "employee.synthetic@example.invalid")
        self.assertNotEqual(operator.google_token_name, employee.google_token_name)
        self.assertEqual(operator.profile, "scotty-main-operator")
        self.assertEqual(employee.profile, "scotty-employee")

    def test_a_token_file_name_is_bounded_and_derived_only_from_the_role(self) -> None:
        resolver = ProviderIdentityResolver(config())
        for role in (Role.MAIN_OPERATOR, Role.EMPLOYEE):
            name = resolver.resolve(principal(role)).google_token_name
            self.assertIn(role.value, name)
            self.assertNotIn("/", name)
            self.assertNotIn("..", name)
            self.assertTrue(name.endswith(".json"))

    def test_an_unlinked_actor_has_no_account_and_no_provider_access(self) -> None:
        only_operator = {"main_operator": BOTH_ACCOUNTS["main_operator"]}
        resolver = ProviderIdentityResolver(
            RuntimeConfig.from_mapping(synthetic.private_mapping(google_workspace=only_operator))
        )
        employee = resolver.resolve(principal(Role.EMPLOYEE))
        self.assertIsNone(employee.google_account)
        self.assertFalse(employee.google_linked)
        # The token name still exists so status can be reported, but nothing
        # about the operator's identity leaks into it.
        self.assertNotIn("operator", employee.google_token_name)

    def test_the_maintainer_is_not_a_client_provider_actor(self) -> None:
        resolver = ProviderIdentityResolver(config())
        route = config().maintainer_route
        maintainer = Principal(
            guild_id=route.guild_id,
            channel_id=route.channel_id,
            user_id=route.user_id,
            role=Role.MAINTAINER,
        )
        with self.assertRaises(ProviderIdentityError):
            resolver.resolve(maintainer)


class CredentialSelectionTests(unittest.TestCase):
    def holder(self, **held: bool):
        """A stand-in for the broker: says what is held, never what it is.

        It answers per citation now rather than per actor name, because that is
        what the wire carries: the request cites a message and the broker
        decides whose it is. The stand-in maps the channel back to the actor
        the same way root's route map does.
        """

        actors = {
            synthetic.OPERATOR_CHANNEL: "main_operator",
            synthetic.EMPLOYEE_CHANNEL: "employee",
        }

        class Holder:
            @staticmethod
            def _actor(provenance) -> str:
                return actors.get(str(provenance.get("channel_id")), "")

            def status_for(self, provider, credential_class, provenance) -> bool:
                del credential_class
                actor = self._actor(provenance)
                return bool(held.get(f"{provider}:{actor}") or held.get(f"{provider}:shared"))

            def owned_for(self, provider, credential_class, provenance) -> bool:
                del credential_class
                return bool(held.get(f"{provider}:{self._actor(provenance)}"))

        return Holder()

    def test_a_per_actor_credential_is_preferred_over_the_shared_one(self) -> None:
        resolver = ProviderIdentityResolver(
            config(),
            self.holder(**{"trello:main_operator": True, "trello:shared": True}),
        )
        operator = resolver.resolve(principal(Role.MAIN_OPERATOR))
        employee = resolver.resolve(principal(Role.EMPLOYEE))
        # True means this actor's own credential; False means the shared one.
        self.assertIs(operator.held["trello"], True)
        self.assertIs(employee.held["trello"], False)

    def test_one_actor_never_receives_another_actors_credential(self) -> None:
        resolver = ProviderIdentityResolver(config(), self.holder(**{"trello:main_operator": True}))
        employee = resolver.resolve(principal(Role.EMPLOYEE))
        # No credential of their own and no shared one: not reachable at all.
        self.assertNotIn("trello", employee.held)

    def test_a_resolved_identity_carries_no_material_at_all(self) -> None:
        resolver = ProviderIdentityResolver(config(), self.holder(**{"trello:main_operator": True}))
        identity = resolver.resolve(principal(Role.MAIN_OPERATOR))
        rendered = f"{identity!r} {identity} {identity.attribution()} {identity.held}"
        # There is nothing to leak: this process never receives the value.
        self.assertNotIn("token", rendered.casefold().replace("google_token_name", ""))

    def test_every_operation_carries_the_authenticated_actor_for_attribution(self) -> None:
        resolver = ProviderIdentityResolver(config(), self.holder(**{"trello:shared": True}))
        identity = resolver.resolve(principal(Role.EMPLOYEE))
        attribution = identity.attribution()
        self.assertEqual(attribution["role"], "employee")
        self.assertEqual(attribution["profile"], "scotty-employee")
        self.assertEqual(attribution["user_id"], synthetic.EMPLOYEE_USER)
        # The shared identity is named as shared, so effects stay attributable.
        self.assertEqual(attribution["shared_identities"], ["trello"])


class ModelOverrideTests(unittest.TestCase):
    """The model may never choose whose identity a call runs as."""

    def test_every_identity_naming_argument_is_refused(self) -> None:
        for name in (
            "actor",
            "account",
            "account_email",
            "as_user",
            "credential",
            "credential_id",
            "oauth_client",
            "on_behalf_of",
            "profile",
            "refresh_token",
            "role",
            "tenant",
            "token",
            "token_path",
            "user_id",
        ):
            with self.subTest(argument=name), self.assertRaises(ProviderIdentityError):
                reject_identity_override({name: "anything"})

    def test_a_nested_override_is_refused_too(self) -> None:
        with self.assertRaises(ProviderIdentityError):
            reject_identity_override({"payload": {"options": {"as_user": "someone"}}})

    def test_ordinary_arguments_are_untouched(self) -> None:
        reject_identity_override(
            {
                "operation": "google_workspace",
                "google_operation": "search_gmail",
                "query": "invoice",
                "payload": {"addLabelIds": ["Label_1"]},
                "resource_id": "message-1",
            }
        )


class ApprovalActorBindingTests(unittest.TestCase):
    """An approval authorizes one actor's action on that actor's own account."""

    def service(self, workspaces):
        import tempfile

        from assistant.scotty_business.approvals import ApprovalStore
        from assistant.scotty_business.service import ScottyService

        directory = tempfile.TemporaryDirectory(prefix="scotty-actor-")
        self.addCleanup(directory.cleanup)
        store = ApprovalStore(f"{directory.name}/approvals.db")
        store.initialize()
        unused = object()
        return (
            ScottyService(
                config(),
                store,
                trello=unused,
                ghl=unused,
                rentcast=None,
                discord=unused,
                google_workspace=workspaces.get,
            ),
            store,
        )

    def recorder(self):
        from assistant.scotty_business.adapters.records import ProviderRecord, utc_now

        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def mutate(self, operation, resource_id, payload):
                self.calls.append((operation, resource_id))
                return ProviderRecord("google_workspace", resource_id, utc_now(), "v1", {}, ())

        return Recorder()

    def test_an_employees_approved_action_runs_on_the_employees_own_account(self) -> None:
        workspaces = {role: self.recorder() for role in (Role.MAIN_OPERATOR, Role.EMPLOYEE)}
        service, store = self.service(workspaces)
        employee = principal(Role.EMPLOYEE)
        operator = principal(Role.MAIN_OPERATOR)

        proposal = service.propose_google_workspace_write(
            employee, "drive_delete_permanently", "file-1", {}
        )
        # The employee proposes; only the operator may approve.
        self.assertIn("employee.synthetic@example.invalid", proposal.target_ids)
        self.assertNotIn("operator.synthetic@example.invalid", proposal.target_ids)
        approved = store.approve(proposal.proposal_id, operator, proposal.version)
        service.execute(
            operator,
            proposal.proposal_id,
            expected_version=approved.version,
            execution_nonce=approved.execution_nonce,
        )
        # Approving the employee's action never moves it onto the approver's
        # own Workspace.
        self.assertEqual(workspaces[Role.EMPLOYEE].calls, [("drive_delete_permanently", "file-1")])
        self.assertEqual(workspaces[Role.MAIN_OPERATOR].calls, [])

    def test_an_action_bound_to_a_disconnected_account_never_runs_elsewhere(self) -> None:
        from assistant.scotty_business.approvals import ApprovalError

        workspaces = {Role.MAIN_OPERATOR: self.recorder(), Role.EMPLOYEE: self.recorder()}
        service, store = self.service(workspaces)
        employee = principal(Role.EMPLOYEE)
        operator = principal(Role.MAIN_OPERATOR)
        proposal = service.propose_google_workspace_write(
            employee, "drive_delete_permanently", "file-1", {}
        )
        approved = store.approve(proposal.proposal_id, operator, proposal.version)
        # The employee's consent is gone by the time the approval executes.
        del workspaces[Role.EMPLOYEE]
        with self.assertRaises(ApprovalError):
            service.execute(
                operator,
                proposal.proposal_id,
                expected_version=approved.version,
                execution_nonce=approved.execution_nonce,
            )
        self.assertEqual(workspaces[Role.MAIN_OPERATOR].calls, [])


class PerUserSetupProgressTests(unittest.TestCase):
    def test_one_users_connected_account_is_not_the_other_users_progress(self) -> None:
        from assistant.scotty_business.setup_flow import setup_progress

        only_operator = RuntimeConfig.from_mapping(
            synthetic.private_mapping(
                google_workspace={"main_operator": BOTH_ACCOUNTS["main_operator"]}
            )
        )
        connected = dict.fromkeys(
            ("discord", "trello", "ghl", "rentcast", "google_workspace"), False
        )
        google = {
            role: next(
                item
                for item in setup_progress(only_operator, connected, role=role)
                if item.provider == "google_workspace"
            )
            for role in (Role.MAIN_OPERATOR, Role.EMPLOYEE)
        }
        self.assertEqual(google[Role.MAIN_OPERATOR].missing, ())
        self.assertTrue(google[Role.EMPLOYEE].missing)


class RuntimeWiringTests(unittest.TestCase):
    """The resolver is what the running system actually uses, not a library."""

    def runtime(self, **environment):
        from test_provider_connection import runtime

        return runtime(DISCORD_BOT_TOKEN="synthetic-discord", **environment)

    @staticmethod
    def principal(instance, role):
        from test_provider_connection import principal_for

        return principal_for(instance, role)

    def test_each_user_gets_an_adapter_bound_to_their_own_actor(self) -> None:
        """The runtime holds no credential, so the binding is the actor itself."""

        with self.runtime(
            SCOTTY_TRELLO_API_KEY="shared-key",
            SCOTTY_TRELLO_TOKEN="shared-token",  # noqa: S106 - synthetic
            SCOTTY_TRELLO_API_KEY_MAIN_OPERATOR="operator-key",
            SCOTTY_TRELLO_TOKEN_MAIN_OPERATOR="operator-token",  # noqa: S106 - synthetic
        ) as runtime:
            operator = self.principal(runtime, Role.MAIN_OPERATOR)
            employee = self.principal(runtime, Role.EMPLOYEE)
            self.assertIsNot(runtime._trello(operator), runtime._trello(employee))
            # The broker holds the operator's own. The employee has none, and a
            # shared business identity existing is not permission to use it --
            # so Trello is not something they reach at all until somebody says
            # so in a grant.
            self.assertIs(runtime.identity_for(operator).held["trello"], True)
            self.assertNotIn("trello", runtime.identity_for(employee).held)
            # Nothing in this process ever held either value.
            self.assertFalse(hasattr(runtime.identity_for(operator), "trello_token"))

    def test_a_granted_shared_identity_becomes_reachable_and_reads_as_shared(self) -> None:
        from datetime import UTC, datetime, timedelta

        from assistant.scotty_broker.grants import Grant

        with self.runtime(
            SCOTTY_TRELLO_API_KEY="shared-key",
            SCOTTY_TRELLO_TOKEN="shared-token",  # noqa: S106 - synthetic
        ) as runtime:
            employee = self.principal(runtime, Role.EMPLOYEE)
            self.assertNotIn("trello", runtime.identity_for(employee).held)
            runtime.broker_grants.put(
                Grant(
                    actor="employee",
                    provider="trello",
                    operations=("trello.list_board_cards", "trello.get_card"),
                    resources=(),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            held = runtime.identity_for(employee).held
            self.assertIn("trello", held)
            # Reachable, and named as the business's rather than their own.
            self.assertIs(held["trello"], False)

    def test_a_user_without_a_credential_is_not_connected_to_that_provider(self) -> None:
        from assistant.scotty_business.runtime import ProviderNotConnected

        with self.runtime(
            SCOTTY_TRELLO_API_KEY_MAIN_OPERATOR="operator-key",
            SCOTTY_TRELLO_TOKEN_MAIN_OPERATOR="operator-token",  # noqa: S106 - synthetic
        ) as runtime:
            operator = self.principal(runtime, Role.MAIN_OPERATOR)
            employee = self.principal(runtime, Role.EMPLOYEE)
            self.assertTrue(runtime.actor_connection_status(operator)["trello"])
            self.assertFalse(runtime.actor_connection_status(employee)["trello"])
            with self.assertRaises(ProviderNotConnected):
                runtime.handle_read(employee, {"operation": "trello_cards"})

    def test_one_users_read_never_runs_as_the_other(self) -> None:
        """Whose identity a read runs as, observed where it is actually decided.

        There is no per-role adapter table to swap any more, which is the
        point: the adapter is built for the principal making the call and
        carries that principal's own citation. So this watches the privileged
        side -- the only place that decides whose credential is spent -- and
        asserts it saw the right person.
        """

        with self.runtime(
            SCOTTY_TRELLO_API_KEY="shared-key",
            SCOTTY_TRELLO_TOKEN="shared-token",  # noqa: S106 - synthetic
            SCOTTY_TRELLO_TOKEN_EMPLOYEE="employee-token",  # noqa: S106 - synthetic
            SCOTTY_TRELLO_TOKEN_MAIN_OPERATOR="operator-token",  # noqa: S106 - synthetic
        ) as runtime:
            seen: list[str] = []

            class Watching:
                def run(self, operation_id, arguments, *, actor, timeout=10.0):
                    del operation_id, arguments, timeout
                    seen.append(actor)

                    class Outcome:
                        ok = True
                        status = 200
                        body: list[object] = []

                        @staticmethod
                        def as_reply():
                            return {"ok": True, "status": 200, "body": [], "state": ""}

                    return Outcome()

            runtime.broker_harness._broker.executor = Watching()
            runtime.handle_read(
                self.principal(runtime, Role.EMPLOYEE), {"operation": "trello_cards"}
            )
            runtime.handle_read(
                self.principal(runtime, Role.MAIN_OPERATOR), {"operation": "trello_cards"}
            )
            self.assertEqual(seen, ["employee", "main_operator"])

    def test_local_setup_collects_a_credential_for_each_user(self) -> None:
        from assistant.scotty_business.setup import OPTIONAL_SECRETS, PER_ACTOR_SECRETS

        for role in ("MAIN_OPERATOR", "EMPLOYEE"):
            for provider in (
                "SCOTTY_TRELLO_API_KEY",
                "SCOTTY_TRELLO_TOKEN",
                "SCOTTY_GHL_PRIVATE_TOKEN",
                "SCOTTY_RENTCAST_API_KEY",
            ):
                name = f"{provider}_{role}"
                self.assertIn(name, PER_ACTOR_SECRETS)
                self.assertIn(name, OPTIONAL_SECRETS)


if __name__ == "__main__":
    unittest.main()
