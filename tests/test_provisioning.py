from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

from assistant.scotty_business.adapters.http import AmbiguousEffectError, ProviderError
from assistant.scotty_business.provisioning import (
    BOT_ALLOW,
    MEMBER_ALLOW,
    VIEW_CHANNEL,
    ChannelPlan,
    ProvisionStatus,
    ensure_private_channels,
    intended_overwrites,
    preview_text,
)

FIXTURE = json.loads(Path("fixtures/discord.provisioning.json").read_text(encoding="utf-8"))
GUILD = FIXTURE["guild"]["id"]
BOT = FIXTURE["bot"]["id"]
OPERATOR_USER = FIXTURE["users"]["main_operator"]
EMPLOYEE_USER = FIXTURE["users"]["employee"]


def plans() -> tuple[ChannelPlan, ...]:
    return (
        ChannelPlan(
            key="main_operator",
            name="scotty-operator",
            guild_id=GUILD,
            user_id=OPERATOR_USER,
        ),
        ChannelPlan(
            key="employee",
            name="scotty-employee",
            guild_id=GUILD,
            user_id=EMPLOYEE_USER,
        ),
    )


class FakeDiscord:
    """Synthetic Discord REST double. No live call is ever made."""

    def __init__(
        self,
        *,
        channels: list[dict[str, object]] | None = None,
        manage_channels: bool = True,
        administrator: bool = False,
        bot_id: str = BOT,
        guild_id: str = GUILD,
        post_error: Exception | None = None,
        post_body: object | None = None,
        readback_error: Exception | None = None,
        readback_override: dict[str, object] | None = None,
    ) -> None:
        self.channels = channels if channels is not None else []
        self.manage_channels = manage_channels
        self.administrator = administrator
        self.bot_id = bot_id
        self.guild_id = guild_id
        self.post_error = post_error
        self.post_body = post_body
        self.readback_error = readback_error
        self.readback_override = readback_override
        self.posts: list[tuple[str, Mapping[str, object]]] = []
        self.gets: list[str] = []
        self._next = 700000000000000001 + len(self.channels)

    def _permissions(self) -> str:
        value = 0
        if self.manage_channels:
            value |= 1 << 4
        if self.administrator:
            value |= 1 << 3
        return str(value)

    def get(self, path: str) -> object:
        self.gets.append(path)
        if path == "/users/@me":
            return {"id": self.bot_id, "bot": True}
        if path == f"/guilds/{GUILD}":
            return {"id": self.guild_id, "owner_id": "999000000000000001"}
        if path == f"/guilds/{GUILD}/members/@me":
            return {"user": {"id": self.bot_id}, "roles": ["400000000000000001"]}
        if path == f"/guilds/{GUILD}/roles":
            return [
                {"id": GUILD, "permissions": "0"},
                {"id": "400000000000000001", "permissions": self._permissions()},
            ]
        if path == f"/guilds/{GUILD}/channels":
            return deepcopy(self.channels)
        if path.startswith("/channels/"):
            if self.readback_error is not None:
                raise self.readback_error
            if self.readback_override is not None:
                return deepcopy(self.readback_override)
            channel_id = path.rsplit("/", 1)[-1]
            for channel in self.channels:
                if channel["id"] == channel_id:
                    return deepcopy(channel)
            raise ProviderError("channel readback unavailable")
        raise AssertionError(path)

    def post(self, path: str, json_body: Mapping[str, object]) -> object:
        self.posts.append((path, dict(json_body)))
        if self.post_error is not None:
            raise self.post_error
        if self.post_body is not None:
            return deepcopy(self.post_body)
        channel = {
            "id": str(self._next),
            "guild_id": GUILD,
            "type": 0,
            "name": json_body["name"],
            "permission_overwrites": deepcopy(json_body["permission_overwrites"]),
        }
        self._next += 1
        self.channels.append(channel)
        return deepcopy(channel)


def existing(name: str, user_id: str, channel_id: str = "500000000000000001") -> dict[str, object]:
    return {
        "id": channel_id,
        "guild_id": GUILD,
        "type": 0,
        "name": name,
        "permission_overwrites": [dict(item) for item in intended_overwrites(GUILD, user_id, BOT)],
    }


def always_confirm(_: str) -> bool:
    return True


def never_confirm(_: str) -> bool:
    return False


class IntendedPermissionTests(unittest.TestCase):
    def test_everyone_is_denied_view_channel_and_principals_get_only_chat(self) -> None:
        overwrites = intended_overwrites(GUILD, OPERATOR_USER, BOT)
        everyone = next(item for item in overwrites if item["id"] == GUILD)
        self.assertEqual(everyone["type"], 0)
        self.assertTrue(int(str(everyone["deny"])) & VIEW_CHANNEL)
        self.assertEqual(everyone["allow"], "0")
        member = next(item for item in overwrites if item["id"] == OPERATOR_USER)
        self.assertEqual(member["type"], 1)
        self.assertEqual(member["allow"], str(MEMBER_ALLOW))
        bot = next(item for item in overwrites if item["id"] == BOT)
        self.assertEqual(bot["allow"], str(BOT_ALLOW))
        for role in (MEMBER_ALLOW, BOT_ALLOW):
            self.assertFalse(role & (1 << 3), "Administrator is never granted")
            self.assertFalse(role & (1 << 5), "Manage Guild is never granted")
            self.assertFalse(role & (1 << 4), "Manage Channels is never granted in-channel")
        self.assertEqual(len(overwrites), 3)


class ProvisioningTests(unittest.TestCase):
    def test_creation_is_previewed_confirmed_read_back_and_recorded(self) -> None:
        client = FakeDiscord()
        previews: list[str] = []

        def confirm(preview: str) -> bool:
            previews.append(preview)
            return True

        outcome = ensure_private_channels(plans(), client, confirm=confirm)
        self.assertIsNone(outcome.error)
        self.assertEqual(len(client.posts), 2)
        self.assertEqual(len(previews), 2)
        for plan in plans():
            channel = outcome.channels[plan.key]
            self.assertEqual(channel.status, ProvisionStatus.CREATED)
            assert channel.channel_id is not None
            self.assertTrue(channel.channel_id.isdigit())
        self.assertIn("scotty-operator", previews[0])
        self.assertIn(OPERATOR_USER, previews[0])

    def test_nothing_is_created_without_an_explicit_local_confirmation(self) -> None:
        client = FakeDiscord()
        outcome = ensure_private_channels(plans(), client, confirm=never_confirm)
        self.assertEqual(client.posts, [])
        self.assertIsNotNone(outcome.error)
        self.assertEqual(outcome.channels, {})

    def test_reruns_reuse_matching_channels_and_never_duplicate(self) -> None:
        client = FakeDiscord()
        first = ensure_private_channels(plans(), client, confirm=always_confirm)
        self.assertIsNone(first.error)
        second = ensure_private_channels(plans(), client, confirm=always_confirm)
        self.assertIsNone(second.error)
        self.assertEqual(len(client.posts), 2, "a rerun creates nothing")
        for key, channel in second.channels.items():
            self.assertEqual(channel.status, ProvisionStatus.REUSED)
            self.assertEqual(channel.channel_id, first.channels[key].channel_id)

    def test_recorded_channel_ids_are_verified_and_never_recreated(self) -> None:
        channels = [
            existing("scotty-operator", OPERATOR_USER, "500000000000000001"),
            existing("scotty-employee", EMPLOYEE_USER, "500000000000000002"),
        ]
        client = FakeDiscord(channels=channels)
        outcome = ensure_private_channels(
            plans(),
            client,
            confirm=never_confirm,
            recorded={
                "main_operator": "500000000000000001",
                "employee": "500000000000000002",
            },
        )
        self.assertIsNone(outcome.error)
        self.assertEqual(client.posts, [])
        self.assertEqual(outcome.channels["main_operator"].channel_id, "500000000000000001")

    def test_an_unknown_prior_outcome_is_never_resolved_by_creating_another(self) -> None:
        client = FakeDiscord()
        outcome = ensure_private_channels(
            plans(), client, confirm=always_confirm, recorded={"main_operator": "unknown"}
        )
        self.assertEqual(client.posts, [])
        self.assertIsNotNone(outcome.error)
        self.assertEqual(outcome.channels["main_operator"].status, ProvisionStatus.UNKNOWN)

    def test_wrong_guild_stops_before_any_mutation(self) -> None:
        client = FakeDiscord(guild_id="899000000000000001")
        outcome = ensure_private_channels(plans(), client, confirm=always_confirm)
        self.assertEqual(client.posts, [])
        self.assertIsNotNone(outcome.error)

    def test_wrong_bot_identity_stops_before_any_mutation(self) -> None:
        class WrongBot(FakeDiscord):
            def get(self, path: str) -> object:
                if path == f"/guilds/{GUILD}/members/@me":
                    self.gets.append(path)
                    return {"user": {"id": "899000000000000002"}, "roles": []}
                return super().get(path)

        client = WrongBot()
        outcome = ensure_private_channels(plans(), client, confirm=always_confirm)
        self.assertEqual(client.posts, [])
        self.assertIsNotNone(outcome.error)

    def test_manage_channels_is_required_and_administrator_is_not(self) -> None:
        allowed = FakeDiscord(manage_channels=True, administrator=False)
        self.assertIsNone(ensure_private_channels(plans(), allowed, confirm=always_confirm).error)
        denied = FakeDiscord(manage_channels=False, administrator=False)
        outcome = ensure_private_channels(plans(), denied, confirm=always_confirm)
        self.assertEqual(denied.posts, [])
        self.assertIsNotNone(outcome.error)

    def test_name_collision_stops_rather_than_hijacking_a_channel(self) -> None:
        duplicate = [
            existing("scotty-operator", OPERATOR_USER, "500000000000000001"),
            existing("scotty-operator", OPERATOR_USER, "500000000000000003"),
        ]
        client = FakeDiscord(channels=duplicate)
        outcome = ensure_private_channels(plans(), client, confirm=always_confirm)
        self.assertEqual(client.posts, [])
        self.assertIsNotNone(outcome.error)

    def test_permission_drift_on_an_existing_channel_stops_rather_than_hijacking(self) -> None:
        drifted = existing("scotty-operator", OPERATOR_USER)
        overwrites = list(drifted["permission_overwrites"])  # type: ignore[arg-type]
        overwrites[0] = {**overwrites[0], "deny": "0"}
        drifted["permission_overwrites"] = overwrites
        client = FakeDiscord(channels=[drifted])
        outcome = ensure_private_channels(plans(), client, confirm=always_confirm)
        self.assertEqual(client.posts, [])
        self.assertIsNotNone(outcome.error)

    def test_an_existing_channel_for_the_wrong_user_is_never_reused(self) -> None:
        wrong_user = existing("scotty-operator", EMPLOYEE_USER)
        client = FakeDiscord(channels=[wrong_user])
        outcome = ensure_private_channels(plans(), client, confirm=always_confirm)
        self.assertEqual(client.posts, [])
        self.assertIsNotNone(outcome.error)

    def test_forbidden_response_stops_and_reports_partial_progress(self) -> None:
        client = FakeDiscord(post_error=ProviderError("provider returned HTTP 403"))
        outcome = ensure_private_channels(plans(), client, confirm=always_confirm)
        self.assertIsNotNone(outcome.error)
        self.assertEqual(outcome.channels, {})

    def test_partial_creation_keeps_the_completed_channel_for_an_idempotent_rerun(self) -> None:
        class FailSecond(FakeDiscord):
            def post(self, path: str, json_body: Mapping[str, object]) -> object:
                if json_body["name"] == "scotty-employee":
                    raise ProviderError("provider returned HTTP 403")
                return super().post(path, json_body)

        client = FailSecond()
        outcome = ensure_private_channels(plans(), client, confirm=always_confirm)
        self.assertIsNotNone(outcome.error)
        self.assertEqual(outcome.channels["main_operator"].status, ProvisionStatus.CREATED)
        self.assertNotIn("employee", outcome.channels)
        rerun = ensure_private_channels(
            plans(), FakeDiscord(channels=client.channels), confirm=always_confirm
        )
        self.assertIsNone(rerun.error)
        self.assertEqual(rerun.channels["main_operator"].status, ProvisionStatus.REUSED)

    def test_timeout_marks_unknown_and_never_creates_another_channel(self) -> None:
        client = FakeDiscord(post_error=AmbiguousEffectError("provider mutation outcome unknown"))
        outcome = ensure_private_channels(plans(), client, confirm=always_confirm)
        self.assertEqual(outcome.channels["main_operator"].status, ProvisionStatus.UNKNOWN)
        self.assertIsNone(outcome.channels["main_operator"].channel_id)
        self.assertIsNotNone(outcome.error)
        self.assertEqual(len(client.posts), 1, "an unknown outcome stops the run")

    def test_ambiguous_create_response_marks_unknown(self) -> None:
        client = FakeDiscord(post_body={"guild_id": GUILD, "type": 0})
        outcome = ensure_private_channels(plans(), client, confirm=always_confirm)
        self.assertEqual(outcome.channels["main_operator"].status, ProvisionStatus.UNKNOWN)
        self.assertIsNotNone(outcome.error)

    def test_unavailable_readback_marks_unknown_rather_than_success(self) -> None:
        client = FakeDiscord(readback_error=ProviderError("provider read failed"))
        outcome = ensure_private_channels(plans(), client, confirm=always_confirm)
        self.assertEqual(outcome.channels["main_operator"].status, ProvisionStatus.UNKNOWN)
        self.assertIsNotNone(outcome.error)

    def test_readback_privacy_drift_fails_instead_of_reporting_success(self) -> None:
        leaked = {
            "id": "500000000000000009",
            "guild_id": GUILD,
            "type": 0,
            "name": "scotty-operator",
            "permission_overwrites": [],
        }
        client = FakeDiscord(readback_override=leaked)
        outcome = ensure_private_channels(plans(), client, confirm=always_confirm)
        self.assertIsNotNone(outcome.error)
        self.assertNotEqual(
            outcome.channels.get("main_operator", None)
            and outcome.channels["main_operator"].status,
            ProvisionStatus.CREATED,
        )

    def test_preview_never_contains_a_credential_and_names_the_exact_scope(self) -> None:
        plan = plans()[0]
        preview = preview_text(plan, BOT)
        self.assertIn(GUILD, preview)
        self.assertIn(OPERATOR_USER, preview)
        self.assertIn("scotty-operator", preview)
        self.assertIn("View Channel", preview)
        self.assertNotIn("Bot ", preview)
        self.assertNotIn("Authorization", preview)


class ProvisioningFixtureTests(unittest.TestCase):
    def test_fixture_identifiers_are_synthetic_snowflakes(self) -> None:
        values = [GUILD, BOT, OPERATOR_USER, EMPLOYEE_USER]
        for value in values:
            with self.subTest(value=value):
                self.assertTrue(value.isdigit())
                self.assertTrue(17 <= len(value) <= 20)
        self.assertIn("Synthetic", json.dumps(FIXTURE))


if __name__ == "__main__":
    unittest.main()
