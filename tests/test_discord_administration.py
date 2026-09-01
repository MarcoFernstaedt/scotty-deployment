"""Functional Discord administration, without the Administrator bit.

The `Administrator` permission bypasses channel overwrites, which is exactly
what keeps Trent's private channel private from Mikey and both of them out of
the maintainer route. So the bot must never hold it, and every administrative
operation must be reachable with ordinary named permissions instead.
"""

from __future__ import annotations

import unittest

import synthetic

from assistant.scotty_business.discord_permissions import (
    ADMINISTRATOR,
    MANAGE_CHANNELS,
    MANAGE_ROLES,
    OPERATION_PERMISSIONS,
    PERMISSION_BITS,
    isolation_overwrites,
    missing_permissions,
    required_permissions,
    role_is_assignable,
)
from assistant.scotty_business.discord_policy import (
    ADMINISTRATION_DISCORD_OPERATIONS,
    DiscordActionClass,
    classify_discord_action,
)
from assistant.scotty_business.policy import Role


class PermissionModelTests(unittest.TestCase):
    def test_the_required_set_never_includes_administrator(self) -> None:
        required = required_permissions()
        self.assertTrue(required)
        self.assertFalse(required & ADMINISTRATOR)
        self.assertNotIn("ADMINISTRATOR", PERMISSION_BITS)

    def test_every_operation_names_the_exact_permissions_it_needs(self) -> None:
        for operation, bits in OPERATION_PERMISSIONS.items():
            with self.subTest(operation=operation):
                self.assertTrue(bits, f"{operation} declares no permission")
                self.assertFalse(bits & ADMINISTRATOR)
                self.assertEqual(bits & required_permissions(), bits)

    def test_every_administration_operation_has_a_permission_requirement(self) -> None:
        for operation in ADMINISTRATION_DISCORD_OPERATIONS:
            self.assertIn(operation, OPERATION_PERMISSIONS)

    def test_a_bot_without_the_bit_is_told_exactly_what_is_missing(self) -> None:
        granted = required_permissions() & ~MANAGE_CHANNELS
        missing = missing_permissions(granted, "create_channel")
        self.assertEqual(missing, ("MANAGE_CHANNELS",))
        self.assertEqual(missing_permissions(required_permissions(), "create_channel"), ())

    def test_administrator_is_never_accepted_as_a_substitute(self) -> None:
        # A guild that granted only Administrator still cannot pass: the
        # deployment refuses to depend on the bit it must not hold.
        self.assertEqual(
            missing_permissions(ADMINISTRATOR, "create_channel"),
            ("MANAGE_CHANNELS",),
        )


class RoleHierarchyTests(unittest.TestCase):
    def test_a_role_at_or_above_the_bot_is_never_assignable(self) -> None:
        self.assertTrue(role_is_assignable(bot_position=5, role_position=4, managed=False))
        self.assertFalse(role_is_assignable(bot_position=5, role_position=5, managed=False))
        self.assertFalse(role_is_assignable(bot_position=5, role_position=9, managed=False))

    def test_a_managed_or_privileged_role_is_never_assignable(self) -> None:
        self.assertFalse(role_is_assignable(bot_position=5, role_position=1, managed=True))
        self.assertFalse(
            role_is_assignable(
                bot_position=5, role_position=1, managed=False, permissions=ADMINISTRATOR
            )
        )
        self.assertFalse(
            role_is_assignable(
                bot_position=5, role_position=1, managed=False, permissions=MANAGE_ROLES
            )
        )


class IsolationTests(unittest.TestCase):
    def test_a_private_channel_is_created_denied_to_everyone_else(self) -> None:
        config = synthetic.config()
        overwrites = isolation_overwrites(config, Role.EMPLOYEE)
        by_id = {item["id"]: item for item in overwrites}
        # @everyone is the guild id in Discord's model, and it is denied.
        self.assertIn(synthetic.CLIENT_GUILD, by_id)
        self.assertTrue(int(by_id[synthetic.CLIENT_GUILD]["deny"]))
        self.assertEqual(by_id[synthetic.CLIENT_GUILD]["allow"], "0")
        # The one member who belongs there is allowed explicitly.
        self.assertIn(synthetic.EMPLOYEE_USER, by_id)
        self.assertTrue(int(by_id[synthetic.EMPLOYEE_USER]["allow"]))
        # The other client user is never granted anything.
        self.assertNotIn(synthetic.OPERATOR_USER, by_id)

    def test_the_maintainer_route_is_never_named_in_an_overwrite(self) -> None:
        config = synthetic.config()
        rendered = str(isolation_overwrites(config, Role.MAIN_OPERATOR))
        for identifier in (
            synthetic.ROUTE_GUILD,
            synthetic.ROUTE_CHANNEL,
            synthetic.ROUTE_USER,
        ):
            self.assertNotIn(identifier, rendered)


class AdministrationClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = synthetic.config()
        self.operator = self.config.principal_for(Role.MAIN_OPERATOR)
        self.destinations = {self.operator.channel_id}

    def classify(self, operation, payload):
        return classify_discord_action(
            operation,
            payload,
            destinations=self.destinations,
            shared=self.config.announcement_channel_ids,
            guild_id=self.config.principals[0].guild_id,
            private_channels={principal.channel_id for principal in self.config.principals},
        )

    def test_every_administrative_operation_is_a_consequence_never_routine(self) -> None:
        payloads = {
            "create_channel": {"name": "deal-flow"},
            "edit_channel": {"channel_id": "230000000000000001", "name": "deal-flow"},
            "archive_channel": {"channel_id": "230000000000000001"},
            "create_category": {"name": "Deals"},
            "reorder_channels": {"channel_ids": ["230000000000000001"]},
            "set_channel_permissions": {
                "channel_id": "230000000000000001",
                "overwrites": [{"id": "1", "allow": "0", "deny": "1024"}],
            },
            "create_forum_post": {"channel_id": "230000000000000001", "name": "lead"},
            "assign_role": {"user_id": synthetic.EMPLOYEE_USER, "role_id": "240000000000000001"},
            "remove_role": {"user_id": synthetic.EMPLOYEE_USER, "role_id": "240000000000000001"},
            "create_event": {"name": "Livestream", "start": "2026-10-01T18:00:00Z"},
            "create_webhook": {"channel_id": "230000000000000001", "name": "updates"},
            "kick_member": {"user_id": "390000000000000001"},
            "ban_member": {"user_id": "390000000000000001"},
            "read_member_permissions": {"user_id": synthetic.EMPLOYEE_USER},
        }
        for operation in sorted(ADMINISTRATION_DISCORD_OPERATIONS):
            with self.subTest(operation=operation):
                self.assertIn(operation, payloads, f"{operation} has no synthetic payload")
                self.assertEqual(
                    self.classify(
                        operation, {"guild_id": self.operator.guild_id, **payloads[operation]}
                    ),
                    DiscordActionClass.CONSEQUENCE,
                )

    def test_an_administrative_action_in_another_guild_is_forbidden(self) -> None:
        self.assertEqual(
            self.classify(
                "create_channel", {"guild_id": "999000000000000009", "name": "elsewhere"}
            ),
            DiscordActionClass.FORBIDDEN,
        )

    def test_touching_a_private_channel_is_forbidden_not_approvable(self) -> None:
        for operation in ("edit_channel", "archive_channel", "set_channel_permissions"):
            with self.subTest(operation=operation):
                self.assertEqual(
                    self.classify(
                        operation,
                        {
                            "guild_id": self.operator.guild_id,
                            "channel_id": synthetic.EMPLOYEE_CHANNEL,
                            "name": "renamed",
                            "overwrites": [],
                        },
                    ),
                    DiscordActionClass.FORBIDDEN,
                )

    def test_granting_a_dangerous_permission_is_forbidden(self) -> None:
        self.assertEqual(
            self.classify(
                "set_channel_permissions",
                {
                    "guild_id": self.operator.guild_id,
                    "channel_id": "230000000000000001",
                    "overwrites": [{"id": "1", "allow": str(int(ADMINISTRATOR)), "deny": "0"}],
                },
            ),
            DiscordActionClass.FORBIDDEN,
        )

    def test_installing_a_bot_or_changing_the_bots_own_role_stays_absent(self) -> None:
        for operation in ("install_bot", "edit_own_role", "delete_guild", "transfer_ownership"):
            with self.subTest(operation=operation):
                self.assertEqual(
                    self.classify(operation, {"guild_id": self.operator.guild_id}),
                    DiscordActionClass.FORBIDDEN,
                )

    def test_ordinary_messaging_is_still_routine(self) -> None:
        self.assertEqual(
            self.classify(
                "send_message",
                {"channel_id": self.operator.channel_id, "content": "hello"},
            ),
            DiscordActionClass.ROUTINE,
        )


class SyntheticGuild:
    """A guild that stores what it is told and answers reads from that state."""

    def __init__(self, permissions: int) -> None:
        from assistant.scotty_business.adapters.http import HttpResponse

        self.response = HttpResponse
        self.permissions = permissions
        self.channels: dict[str, dict] = {}
        self.members: dict[str, dict] = {"390000000000000001": {"roles": []}}
        self.events: dict[str, dict] = {}
        self.bans: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.next_id = 1

    def _ok(self, body, status=200):
        return self.response(status=status, headers={}, body=body)

    def request(self, method, url, *, headers=None, query=None, json_body=None):
        self.calls.append((method, url))
        path = url.split("discord.com/api/v10", 1)[1]
        if path == "/users/@me/guilds":
            return self._ok([{"id": synthetic.CLIENT_GUILD, "permissions": self.permissions}])
        if method == "POST" and path.endswith("/channels"):
            channel_id = f"23000000000000000{self.next_id}"
            self.next_id += 1
            self.channels[channel_id] = {
                "id": channel_id,
                "permission_overwrites": list(json_body.get("permission_overwrites", [])),
                **{k: v for k, v in json_body.items() if k != "permission_overwrites"},
            }
            return self._ok(dict(self.channels[channel_id]), status=201)
        if method == "PATCH" and path.startswith("/channels/"):
            channel_id = path.split("/")[2]
            self.channels.setdefault(channel_id, {"id": channel_id}).update(json_body or {})
            return self._ok(dict(self.channels[channel_id]))
        if method == "PUT" and "/permissions/" in path:
            channel_id, overwrite_id = path.split("/")[2], path.split("/")[4]
            channel = self.channels.setdefault(
                channel_id, {"id": channel_id, "permission_overwrites": []}
            )
            existing = [
                item
                for item in channel.get("permission_overwrites", [])
                if item.get("id") != overwrite_id
            ]
            existing.append({"id": overwrite_id, **(json_body or {})})
            channel["permission_overwrites"] = existing
            return self._ok(None, status=204)
        if method == "GET" and path.startswith("/channels/"):
            channel_id = path.split("/")[2]
            if channel_id not in self.channels:
                return self._ok(None, status=404)
            return self._ok(dict(self.channels[channel_id]))
        if "/members/" in path and "/roles/" in path:
            member_id, role_id = path.split("/")[4], path.split("/")[6]
            member = self.members.setdefault(member_id, {"roles": []})
            if method == "PUT" and role_id not in member["roles"]:
                member["roles"].append(role_id)
            if method == "DELETE" and role_id in member["roles"]:
                member["roles"].remove(role_id)
            return self._ok(None, status=204)
        if "/members/" in path:
            member_id = path.split("/")[4]
            if method == "DELETE":
                self.members.pop(member_id, None)
                return self._ok(None, status=204)
            if member_id not in self.members:
                return self._ok(None, status=404)
            return self._ok({"user_id": member_id, **self.members[member_id]})
        if "/scheduled-events" in path:
            if method == "POST":
                event_id = f"25000000000000000{self.next_id}"
                self.next_id += 1
                self.events[event_id] = {"id": event_id, **(json_body or {})}
                return self._ok(dict(self.events[event_id]), status=201)
            return self._ok(dict(self.events[path.split("/")[-1]]))
        if "/webhooks" in path:
            return self._ok(
                {
                    "id": "260000000000000001",
                    "name": (json_body or {}).get("name"),
                    "token": "synthetic-webhook-token",
                },
                status=201,
            )
        if "/bans/" in path:
            member_id = path.split("/")[4]
            if method == "PUT":
                self.bans.add(member_id)
                return self._ok(None, status=204)
            return self._ok(
                {"user": {"id": member_id}} if member_id in self.bans else None,
                status=200 if member_id in self.bans else 404,
            )
        return self._ok(None, status=404)


class AdminAdapterTests(unittest.TestCase):
    def adapter(self, permissions=None):
        from assistant.scotty_business.adapters.discord_admin import DiscordAdminAdapter

        guild = SyntheticGuild(required_permissions() if permissions is None else permissions)
        return (
            DiscordAdminAdapter(guild, "synthetic-bot-token", synthetic.CLIENT_GUILD),
            guild,
        )

    def test_a_created_channel_is_read_back_before_it_counts(self) -> None:
        adapter, guild = self.adapter()
        created = adapter.create_channel("deal-flow", kind="text")
        self.assertEqual(created["name"], "deal-flow")
        self.assertTrue(any(method == "GET" for method, _ in guild.calls))

    def test_a_private_channel_is_created_with_its_isolating_overwrites(self) -> None:
        adapter, _ = self.adapter()
        created = adapter.create_channel(
            "mikey-private",
            overwrites=isolation_overwrites(synthetic.config(), Role.EMPLOYEE),
        )
        by_id = {item["id"]: item for item in created["permission_overwrites"]}
        self.assertEqual(by_id[synthetic.CLIENT_GUILD]["allow"], "0")
        self.assertIn(synthetic.EMPLOYEE_USER, by_id)

    def test_a_missing_permission_is_named_before_any_call_is_made(self) -> None:
        from assistant.scotty_business.adapters.http import ProviderError

        adapter, _ = self.adapter(permissions=required_permissions() & ~MANAGE_CHANNELS)
        with self.assertRaises(ProviderError) as caught:
            adapter.require_permission("create_channel")
        self.assertIn("MANAGE_CHANNELS", str(caught.exception))

    def test_an_overwrite_granting_a_dangerous_permission_is_refused(self) -> None:
        from assistant.scotty_business.adapters.http import ProviderError

        adapter, guild = self.adapter()
        with self.assertRaises(ProviderError):
            adapter.set_channel_permissions(
                "230000000000000001",
                [{"id": synthetic.EMPLOYEE_USER, "allow": str(int(ADMINISTRATOR)), "deny": "0"}],
            )
        self.assertEqual(guild.calls, [])

    def test_permission_changes_are_read_back_exactly(self) -> None:
        adapter, _ = self.adapter()
        created = adapter.create_channel("deal-flow")
        observed = adapter.set_channel_permissions(
            created["id"], [{"id": synthetic.EMPLOYEE_USER, "allow": "1024", "deny": "0"}]
        )
        by_id = {item["id"]: item for item in observed["permission_overwrites"]}
        self.assertEqual(by_id[synthetic.EMPLOYEE_USER]["allow"], "1024")

    def test_a_role_at_or_above_the_bot_is_refused_before_the_call(self) -> None:
        from assistant.scotty_business.adapters.http import ProviderError

        adapter, guild = self.adapter()
        with self.assertRaises(ProviderError):
            adapter.assign_role(
                "390000000000000001",
                "240000000000000001",
                bot_position=3,
                role={"position": 9, "managed": False, "permissions": 0},
            )
        self.assertEqual(guild.calls, [])

    def test_an_assigned_role_is_confirmed_on_the_member(self) -> None:
        adapter, _ = self.adapter()
        observed = adapter.assign_role(
            "390000000000000001",
            "240000000000000001",
            bot_position=9,
            role={"position": 2, "managed": False, "permissions": 0},
        )
        self.assertIn("240000000000000001", observed.roles)
        removed = adapter.remove_role("390000000000000001", "240000000000000001")
        self.assertNotIn("240000000000000001", removed.roles)

    def test_a_webhook_never_returns_its_token(self) -> None:
        adapter, _ = self.adapter()
        created = adapter.create_webhook("230000000000000001", "updates")
        self.assertNotIn("token", created)
        self.assertNotIn("synthetic-webhook-token", str(created))

    def test_archiving_never_deletes_and_is_confirmed(self) -> None:
        adapter, guild = self.adapter()
        created = adapter.create_channel("old-deals")
        archived = adapter.archive_channel(created["id"])
        self.assertTrue(archived["archived"])
        self.assertNotIn("DELETE", {method for method, _ in guild.calls})

    def test_a_kick_is_confirmed_by_the_member_being_gone(self) -> None:
        adapter, _ = self.adapter()
        result = adapter.kick_member("390000000000000001")
        self.assertTrue(result["removed"])

    def test_an_event_is_read_back_after_it_is_scheduled(self) -> None:
        adapter, _ = self.adapter()
        created = adapter.create_event("Livestream", "2026-10-01T18:00:00Z")
        self.assertEqual(created["name"], "Livestream")


class ApprovalPathTests(unittest.TestCase):
    """Administration is reachable only through an approved proposal."""

    def runtime(self):
        from test_provider_connection import runtime

        return runtime(DISCORD_BOT_TOKEN="synthetic-discord")

    def test_the_routine_tool_never_performs_an_administrative_action(self) -> None:
        with self.runtime() as runtime:
            operator = runtime.config.principal_for(Role.MAIN_OPERATOR)
            for operation in sorted(ADMINISTRATION_DISCORD_OPERATIONS):
                with self.subTest(operation=operation), self.assertRaises(PermissionError):
                    runtime.handle_read(
                        operator,
                        {
                            "operation": "discord",
                            "discord_operation": operation,
                            "payload": {"name": "deal-flow"},
                        },
                    )

    def test_a_proposal_records_the_guild_and_only_the_allowed_payload_keys(self) -> None:
        with self.runtime() as runtime:
            operator = runtime.config.principal_for(Role.MAIN_OPERATOR)
            proposal = runtime.service.propose_discord_administration(
                operator,
                "create_channel",
                {"name": "deal-flow", "topic": "leads", "smuggled": "nope"},
            )
            self.assertIn(synthetic.CLIENT_GUILD, proposal.target_ids)
            self.assertNotIn("smuggled", proposal.payload)
            self.assertEqual(proposal.payload["name"], "deal-flow")

    def test_a_proposal_touching_a_private_channel_is_refused_outright(self) -> None:
        from assistant.scotty_business.adapters.http import ProviderError

        with self.runtime() as runtime:
            operator = runtime.config.principal_for(Role.MAIN_OPERATOR)
            with self.assertRaises(ProviderError):
                runtime.service.propose_discord_administration(
                    operator,
                    "archive_channel",
                    {"channel_id": synthetic.EMPLOYEE_CHANNEL},
                )

    def test_an_employee_cannot_approve_their_own_administration_proposal(self) -> None:
        with self.runtime() as runtime:
            employee = runtime.config.principal_for(Role.EMPLOYEE)
            proposal = runtime.service.propose_discord_administration(
                employee, "create_channel", {"name": "deal-flow"}
            )
            self.assertEqual(proposal.approver.role, Role.MAIN_OPERATOR)


if __name__ == "__main__":
    unittest.main()
