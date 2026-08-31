from __future__ import annotations

import unittest

import synthetic

from assistant.scotty_business.discord_policy import (
    BULK_MESSAGE_THRESHOLD,
    CONSEQUENCE_DISCORD_OPERATIONS,
    MAX_MENTIONS,
    ROUTINE_DISCORD_OPERATIONS,
    DiscordActionClass,
    announcement_is_safe,
    classify_discord_action,
    permitted_destinations,
)
from assistant.scotty_business.policy import Role

OPERATOR_MESSAGE = "900000000000000001"


def operator():
    return synthetic.config().principal_for(Role.MAIN_OPERATOR)


def employee():
    return synthetic.config().principal_for(Role.EMPLOYEE)


class DestinationScopeTests(unittest.TestCase):
    def test_each_client_reaches_only_their_own_channel_and_shared_destinations(self) -> None:
        config = synthetic.config()
        for principal in (operator(), employee()):
            with self.subTest(role=principal.role):
                allowed = permitted_destinations(config, principal)
                self.assertIn(principal.channel_id, allowed)
                self.assertIn(synthetic.ANNOUNCEMENT_CHANNEL, allowed)

    def test_neither_client_can_reach_the_other_private_channel(self) -> None:
        config = synthetic.config()
        self.assertNotIn(synthetic.EMPLOYEE_CHANNEL, permitted_destinations(config, operator()))
        self.assertNotIn(synthetic.OPERATOR_CHANNEL, permitted_destinations(config, employee()))

    def test_no_client_ever_reaches_the_private_maintainer_route(self) -> None:
        config = synthetic.config()
        for principal in (operator(), employee()):
            with self.subTest(role=principal.role):
                allowed = permitted_destinations(config, principal)
                self.assertNotIn(synthetic.ROUTE_CHANNEL, allowed)
                self.assertNotIn(synthetic.ROUTE_GUILD, allowed)


class ActionClassificationTests(unittest.TestCase):
    def classify(self, operation, payload, principal=None):
        config = synthetic.config()
        return classify_discord_action(
            operation,
            payload,
            destinations=permitted_destinations(config, principal or operator()),
        )

    def test_ordinary_assistant_work_in_the_caller_channel_is_routine(self) -> None:
        for operation in sorted(ROUTINE_DISCORD_OPERATIONS):
            payload = {"channel_id": synthetic.OPERATOR_CHANNEL, "content": "Working on it."}
            if operation == "attach_file":
                payload |= {"filename": "summary.md", "size_bytes": 2048}
            with self.subTest(operation=operation):
                self.assertEqual(self.classify(operation, payload), DiscordActionClass.ROUTINE)

    def test_publishing_to_a_shared_destination_is_consequence_gated(self) -> None:
        self.assertEqual(
            self.classify(
                "announce",
                {"channel_id": synthetic.ANNOUNCEMENT_CHANNEL, "content": "Weekly summary."},
            ),
            DiscordActionClass.CONSEQUENCE,
        )

    def test_bulk_messaging_and_a_large_audience_are_consequence_gated(self) -> None:
        self.assertEqual(
            self.classify(
                "send_message",
                {
                    "channel_id": synthetic.OPERATOR_CHANNEL,
                    "content": "Update",
                    "message_count": BULK_MESSAGE_THRESHOLD + 1,
                },
            ),
            DiscordActionClass.CONSEQUENCE,
        )
        mentions = " ".join(f"<@{300000000000000000 + n}>" for n in range(MAX_MENTIONS + 1))
        self.assertEqual(
            self.classify(
                "send_message", {"channel_id": synthetic.OPERATOR_CHANNEL, "content": mentions}
            ),
            DiscordActionClass.CONSEQUENCE,
        )

    def test_administrative_and_moderation_actions_have_no_operation_at_all(self) -> None:
        for operation in (
            "create_channel",
            "delete_channel",
            "edit_channel_permissions",
            "add_role",
            "remove_role",
            "ban_member",
            "kick_member",
            "timeout_member",
            "create_webhook",
            "install_bot",
            "bulk_delete_messages",
            "purge_history",
            "delete_any_message",
            "edit_any_message",
            "leave_guild",
            "",
            None,
            17,
        ):
            with self.subTest(operation=operation):
                self.assertEqual(
                    self.classify(
                        operation,
                        {"channel_id": synthetic.OPERATOR_CHANNEL, "content": "x"},
                    ),
                    DiscordActionClass.FORBIDDEN,
                )

    def test_another_users_channel_is_forbidden_not_merely_approvable(self) -> None:
        for operation in ("send_message", "read_channel", "announce", "reply_message"):
            with self.subTest(operation=operation):
                self.assertEqual(
                    self.classify(
                        operation,
                        {"channel_id": synthetic.EMPLOYEE_CHANNEL, "content": "hello"},
                    ),
                    DiscordActionClass.FORBIDDEN,
                )
                self.assertEqual(
                    self.classify(
                        operation,
                        {"channel_id": synthetic.OPERATOR_CHANNEL, "content": "hello"},
                        principal=employee(),
                    ),
                    DiscordActionClass.FORBIDDEN,
                )

    def test_a_channel_outside_the_configured_guild_is_forbidden(self) -> None:
        for channel in (synthetic.ROUTE_CHANNEL, "999000000000000009", "", None):
            with self.subTest(channel=channel):
                self.assertEqual(
                    self.classify("send_message", {"channel_id": channel, "content": "hello"}),
                    DiscordActionClass.FORBIDDEN,
                )

    def test_a_mass_mention_is_absent_rather_than_approvable(self) -> None:
        for content in ("@everyone please read", "hey @here", "cc @everyone"):
            with self.subTest(content=content):
                self.assertEqual(
                    self.classify(
                        "send_message",
                        {"channel_id": synthetic.OPERATOR_CHANNEL, "content": content},
                    ),
                    DiscordActionClass.FORBIDDEN,
                )
                self.assertEqual(
                    self.classify(
                        "announce",
                        {"channel_id": synthetic.ANNOUNCEMENT_CHANNEL, "content": content},
                    ),
                    DiscordActionClass.FORBIDDEN,
                )

    def test_an_oversized_message_or_unapproved_attachment_is_forbidden(self) -> None:
        self.assertEqual(
            self.classify(
                "send_message",
                {"channel_id": synthetic.OPERATOR_CHANNEL, "content": "x" * 2001},
            ),
            DiscordActionClass.FORBIDDEN,
        )
        for attachment in (
            {"filename": "payload.exe", "size_bytes": 10},
            {"filename": "notes.md", "size_bytes": 9_000_000},
            {"filename": "../escape.md", "size_bytes": 10},
            {"filename": ".hidden.md", "size_bytes": 10},
            {"filename": "notes.md", "size_bytes": 0},
            {"filename": "", "size_bytes": 10},
        ):
            with self.subTest(attachment=attachment):
                self.assertEqual(
                    self.classify(
                        "attach_file", {"channel_id": synthetic.OPERATOR_CHANNEL, **attachment}
                    ),
                    DiscordActionClass.FORBIDDEN,
                )

    def test_every_consequence_operation_is_named_and_none_is_routine(self) -> None:
        self.assertEqual(CONSEQUENCE_DISCORD_OPERATIONS, {"announce", "bulk_message"})
        self.assertFalse(ROUTINE_DISCORD_OPERATIONS & CONSEQUENCE_DISCORD_OPERATIONS)


class AnnouncementLeakageTests(unittest.TestCase):
    def test_ordinary_status_text_may_be_published(self) -> None:
        config = synthetic.config()
        self.assertTrue(announcement_is_safe("Three listings were updated today.", config))

    def test_a_private_channel_or_user_identifier_is_never_published(self) -> None:
        config = synthetic.config()
        for identifier in (
            synthetic.OPERATOR_CHANNEL,
            synthetic.EMPLOYEE_CHANNEL,
            synthetic.OPERATOR_USER,
            synthetic.EMPLOYEE_USER,
        ):
            with self.subTest(identifier=identifier):
                self.assertFalse(announcement_is_safe(f"Update for {identifier}", config))

    def test_maintainer_route_details_are_never_published(self) -> None:
        config = synthetic.config()
        for identifier in (
            synthetic.ROUTE_GUILD,
            synthetic.ROUTE_CHANNEL,
            synthetic.ROUTE_USER,
            synthetic.ROUTE_PROFILE,
        ):
            with self.subTest(identifier=identifier):
                self.assertFalse(announcement_is_safe(f"see {identifier}", config))

    def test_credential_shaped_text_is_never_published(self) -> None:
        config = synthetic.config()
        # Built at run time so the repository secret scanner never has to make
        # an exception for this file.
        forge = "gh" + "p_" + "synthetic" + "0" * 24
        for content in (
            "api key is synthetic-value-000000000000",
            "token=synthetic-value-000000000000",
            "ya29." + "synthetic-access-value-" + "0" * 10,
            forge,
        ):
            with self.subTest(content=content[:20]):
                self.assertFalse(announcement_is_safe(content, config))

    def test_empty_or_non_text_content_is_never_published(self) -> None:
        config = synthetic.config()
        for content in ("", "   ", None, 7, ["text"]):
            with self.subTest(content=content):
                self.assertFalse(announcement_is_safe(content, config))


BOT = "600000000000000001"


class RecordingTransport:
    """Scripted Discord responses keyed by method and URL suffix."""

    def __init__(self, routes=None, default=None):
        from assistant.scotty_business.adapters.http import HttpResponse

        self.routes = routes or {}
        self.default = default or HttpResponse(200, {}, {})
        self.calls: list[tuple[str, str, object, object]] = []

    def request(self, method, url, *, headers=None, query=None, json_body=None, attachment=None):
        self.calls.append((method, url, json_body, attachment))
        for (want_method, suffix), response in self.routes.items():
            if method == want_method and suffix in url:
                return response(len(self.calls)) if callable(response) else response
        return self.default


class AdapterHarness(unittest.TestCase):
    def adapter(self, routes=None, default=None):
        from assistant.scotty_business.adapters.discord import DiscordAdapter
        from assistant.scotty_business.adapters.http import HttpResponse

        routes = dict(routes or {})
        routes.setdefault(("GET", "/users/@me"), HttpResponse(200, {}, {"id": BOT}))
        transport = RecordingTransport(routes, default)
        adapter = DiscordAdapter(
            transport,
            "synthetic-bot-token",
            (
                synthetic.OPERATOR_CHANNEL,
                synthetic.EMPLOYEE_CHANNEL,
                synthetic.ANNOUNCEMENT_CHANNEL,
            ),
        )
        return adapter, transport

    def message(self, author=BOT, content="hello", channel=None):
        return {
            "id": OPERATOR_MESSAGE,
            "channel_id": channel or synthetic.OPERATOR_CHANNEL,
            "author": {"id": author},
            "content": content,
        }


class TypedDiscordOperationTests(AdapterHarness):
    def test_reading_a_configured_channel_returns_bounded_history(self) -> None:
        from assistant.scotty_business.adapters.http import HttpResponse

        adapter, transport = self.adapter(
            {("GET", "/messages"): HttpResponse(200, {}, [self.message()])}
        )
        messages = adapter.read_channel(synthetic.OPERATOR_CHANNEL, limit=5)
        self.assertEqual(messages[0]["id"], OPERATOR_MESSAGE)
        self.assertTrue(any("/messages" in call[1] for call in transport.calls))

    def test_reading_refuses_an_unconfigured_channel_or_an_unbounded_limit(self) -> None:
        from assistant.scotty_business.adapters.http import ProviderError

        adapter, transport = self.adapter()
        before = len(transport.calls)
        with self.assertRaises(ProviderError):
            adapter.read_channel(synthetic.ROUTE_CHANNEL)
        for limit in (0, -1, 500, "5", None):
            with self.subTest(limit=limit), self.assertRaises(ProviderError):
                adapter.read_channel(synthetic.OPERATOR_CHANNEL, limit=limit)
        self.assertEqual(len(transport.calls), before)

    def test_a_reply_references_the_exact_message_and_parses_no_mentions(self) -> None:
        from assistant.scotty_business.adapters.http import HttpResponse

        adapter, transport = self.adapter(
            {("POST", "/messages"): HttpResponse(201, {}, self.message())}
        )
        adapter.reply_message(synthetic.OPERATOR_CHANNEL, OPERATOR_MESSAGE, "On it.")
        body = transport.calls[-1][2]
        self.assertEqual(body["message_reference"]["message_id"], OPERATOR_MESSAGE)
        self.assertTrue(body["message_reference"]["fail_if_not_exists"])
        self.assertEqual(body["allowed_mentions"], {"parse": []})

    def test_scotty_may_edit_and_delete_only_its_own_message(self) -> None:
        from assistant.scotty_business.adapters.http import HttpResponse, ProviderError

        adapter, transport = self.adapter(
            {
                ("GET", f"/messages/{OPERATOR_MESSAGE}"): HttpResponse(200, {}, self.message()),
                ("PATCH", "/messages"): HttpResponse(200, {}, self.message(content="Done.")),
                ("DELETE", "/messages"): HttpResponse(204, {}, None),
            }
        )
        self.assertEqual(
            adapter.edit_own_message(synthetic.OPERATOR_CHANNEL, OPERATOR_MESSAGE, "Done.")[
                "message_id"
            ],
            OPERATOR_MESSAGE,
        )

        other, transport = self.adapter(
            {
                ("GET", f"/messages/{OPERATOR_MESSAGE}"): HttpResponse(
                    200, {}, self.message(author=synthetic.OPERATOR_USER)
                )
            }
        )
        before = len(transport.calls)
        for call in (
            lambda: other.edit_own_message(synthetic.OPERATOR_CHANNEL, OPERATOR_MESSAGE, "Done."),
            lambda: other.delete_own_message(synthetic.OPERATOR_CHANNEL, OPERATOR_MESSAGE),
        ):
            with self.subTest(call=call), self.assertRaises(ProviderError):
                call()
        self.assertFalse(
            any(method in {"PATCH", "DELETE"} for method, _, _, _ in transport.calls[before:])
        )

    def test_an_edit_whose_readback_disagrees_is_ambiguous_not_success(self) -> None:
        from assistant.scotty_business.adapters.http import (
            AmbiguousEffectError,
            HttpResponse,
        )

        adapter, _ = self.adapter(
            {
                ("GET", f"/messages/{OPERATOR_MESSAGE}"): HttpResponse(200, {}, self.message()),
                ("PATCH", "/messages"): HttpResponse(200, {}, self.message(content="stale")),
            }
        )
        with self.assertRaises(AmbiguousEffectError):
            adapter.edit_own_message(synthetic.OPERATOR_CHANNEL, OPERATOR_MESSAGE, "Done.")

    def test_reactions_are_added_and_removed_only_as_scotty(self) -> None:
        from assistant.scotty_business.adapters.http import HttpResponse, ProviderError

        adapter, transport = self.adapter(default=HttpResponse(204, {}, None))
        self.assertTrue(
            adapter.add_reaction(synthetic.OPERATOR_CHANNEL, OPERATOR_MESSAGE, "\u2705")
        )
        self.assertTrue(
            adapter.remove_own_reaction(synthetic.OPERATOR_CHANNEL, OPERATOR_MESSAGE, "\u2705")
        )
        self.assertTrue(all(call[1].endswith("/@me") for call in transport.calls[-2:]))
        for emoji in ("", "x" * 40, "a/b"):
            with self.subTest(emoji=emoji), self.assertRaises(ProviderError):
                adapter.add_reaction(synthetic.OPERATOR_CHANNEL, OPERATOR_MESSAGE, emoji)

    def test_an_attachment_is_uploaded_as_one_bounded_multipart_body(self) -> None:
        from assistant.scotty_business.adapters.http import Attachment, HttpResponse

        adapter, transport = self.adapter(
            {("POST", "/messages"): HttpResponse(201, {}, self.message())}
        )
        adapter.attach_file(
            synthetic.OPERATOR_CHANNEL,
            "Here is the summary.",
            Attachment("summary.md", "text/markdown", b"# synthetic"),
        )
        attachment = transport.calls[-1][3]
        self.assertEqual(attachment.filename, "summary.md")

    def test_a_task_thread_is_created_under_a_configured_parent_only(self) -> None:
        from assistant.scotty_business.adapters.http import HttpResponse, ProviderError

        thread = "950000000000000001"
        adapter, _ = self.adapter(
            {
                ("POST", "/threads"): HttpResponse(
                    201, {}, {"id": thread, "parent_id": synthetic.OPERATOR_CHANNEL}
                )
            }
        )
        self.assertEqual(adapter.create_thread(synthetic.OPERATOR_CHANNEL, "Closing tasks"), thread)
        with self.assertRaises(ProviderError):
            adapter.create_thread(synthetic.ROUTE_CHANNEL, "Closing tasks")
        for name in ("", "x" * 101, "bad\nname"):
            with self.subTest(name=name), self.assertRaises(ProviderError):
                adapter.create_thread(synthetic.OPERATOR_CHANNEL, name)

    def test_a_thread_whose_parent_is_not_configured_is_never_posted_to(self) -> None:
        from assistant.scotty_business.adapters.http import HttpResponse, ProviderError

        thread = "950000000000000002"
        adapter, transport = self.adapter(
            {
                ("GET", f"/channels/{thread}"): HttpResponse(
                    200, {}, {"id": thread, "parent_id": synthetic.ROUTE_CHANNEL}
                )
            }
        )
        before = len(transport.calls)
        with self.assertRaises(ProviderError):
            adapter.send_thread_message(thread, "status")
        self.assertFalse(any(method == "POST" for method, _, _, _ in transport.calls[before:]))

    def test_only_a_thread_scotty_owns_may_be_archived(self) -> None:
        from assistant.scotty_business.adapters.http import HttpResponse, ProviderError

        thread = "950000000000000003"
        adapter, _ = self.adapter(
            {
                ("GET", f"/channels/{thread}"): HttpResponse(
                    200,
                    {},
                    {"id": thread, "parent_id": synthetic.OPERATOR_CHANNEL, "owner_id": BOT},
                ),
                ("PATCH", f"/channels/{thread}"): HttpResponse(200, {}, {"archived": True}),
            }
        )
        self.assertTrue(adapter.archive_own_thread(thread))

        foreign, transport = self.adapter(
            {
                ("GET", f"/channels/{thread}"): HttpResponse(
                    200,
                    {},
                    {
                        "id": thread,
                        "parent_id": synthetic.OPERATOR_CHANNEL,
                        "owner_id": synthetic.OPERATOR_USER,
                    },
                )
            }
        )
        before = len(transport.calls)
        with self.assertRaises(ProviderError):
            foreign.archive_own_thread(thread)
        self.assertFalse(any(method == "PATCH" for method, _, _, _ in transport.calls[before:]))

    def test_every_send_path_refuses_a_destination_outside_the_allowlist(self) -> None:
        from assistant.scotty_business.adapters.http import Attachment, ProviderError

        adapter, transport = self.adapter()
        before = len(transport.calls)
        for call in (
            lambda: adapter.reply_message(synthetic.ROUTE_CHANNEL, OPERATOR_MESSAGE, "hi"),
            lambda: adapter.edit_own_message(synthetic.ROUTE_CHANNEL, OPERATOR_MESSAGE, "hi"),
            lambda: adapter.delete_own_message(synthetic.ROUTE_CHANNEL, OPERATOR_MESSAGE),
            lambda: adapter.add_reaction(synthetic.ROUTE_CHANNEL, OPERATOR_MESSAGE, "\u2705"),
            lambda: adapter.attach_file(
                synthetic.ROUTE_CHANNEL, "hi", Attachment("a.md", "text/markdown", b"x")
            ),
        ):
            with self.subTest(call=call), self.assertRaises(ProviderError):
                call()
        self.assertEqual(len(transport.calls), before)

    def test_an_ambiguous_acknowledgement_is_never_reported_as_delivered(self) -> None:
        from assistant.scotty_business.adapters.http import (
            AmbiguousEffectError,
            HttpResponse,
        )

        adapter, _ = self.adapter(
            {
                ("POST", "/messages"): HttpResponse(
                    201, {}, {"id": OPERATOR_MESSAGE, "channel_id": synthetic.EMPLOYEE_CHANNEL}
                )
            }
        )
        with self.assertRaises(AmbiguousEffectError):
            adapter.reply_message(synthetic.OPERATOR_CHANNEL, OPERATOR_MESSAGE, "On it.")


class FakeChannel:
    """Records what a reporter actually wrote to Discord."""

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.posts: list[str] = []
        self.edits: list[str] = []
        self.fail_after = fail_after

    def send(self, channel_id: str, text: str):
        if self.fail_after is not None and len(self.posts) >= self.fail_after:
            raise RuntimeError("send unavailable")
        self.posts.append(text)
        return {"message_id": f"m-{len(self.posts)}", "channel_id": channel_id}

    def edit(self, channel_id: str, message_id: str, text: str):
        if self.fail_after is not None and len(self.edits) >= self.fail_after:
            raise RuntimeError("edit unavailable")
        self.edits.append(text)
        return {"message_id": message_id, "channel_id": channel_id}


class ProgressReportingTests(unittest.TestCase):
    def reporter(self, channel, now, **kwargs):
        from assistant.scotty_business.progress import ProgressReporter

        return ProgressReporter(
            synthetic.OPERATOR_CHANNEL,
            channel.send,
            channel.edit,
            clock=lambda: now[0],
            **kwargs,
        )

    def test_the_first_update_posts_and_later_ones_edit_in_place(self) -> None:
        from assistant.scotty_business.progress import ProgressState

        now = [0.0]
        channel = FakeChannel()
        reporter = self.reporter(channel, now)

        self.assertEqual(reporter.update("Starting").state, ProgressState.POSTED)
        now[0] += 10
        self.assertEqual(reporter.update("Halfway").state, ProgressState.EDITED)
        now[0] += 10
        self.assertEqual(reporter.update("Nearly done").state, ProgressState.EDITED)

        self.assertEqual(channel.posts, ["Starting"])
        self.assertEqual(channel.edits, ["Halfway", "Nearly done"])

    def test_rapid_updates_are_coalesced_into_one_later_edit(self) -> None:
        from assistant.scotty_business.progress import ProgressState

        now = [0.0]
        channel = FakeChannel()
        reporter = self.reporter(channel, now)
        reporter.update("Starting")

        for step in range(20):
            now[0] += 0.1
            self.assertEqual(reporter.update(f"step {step}").state, ProgressState.COALESCED)
        self.assertEqual(channel.edits, [])
        self.assertEqual(reporter.coalesced, "step 19")

        now[0] += 10
        self.assertEqual(reporter.update("step 20").state, ProgressState.EDITED)
        self.assertEqual(channel.edits, ["step 20"])
        self.assertEqual(len(channel.posts), 1)

    def test_a_long_task_can_never_exceed_its_write_budget(self) -> None:
        now = [0.0]
        channel = FakeChannel()
        reporter = self.reporter(channel, now, max_edits=3)
        reporter.update("Starting")
        for step in range(50):
            now[0] += 60
            reporter.update(f"step {step}")
        self.assertEqual(len(channel.posts), 1)
        self.assertLessEqual(len(channel.edits), 3)

    def test_a_finished_task_always_reports_its_final_state(self) -> None:
        from assistant.scotty_business.progress import ProgressState

        now = [0.0]
        channel = FakeChannel()
        reporter = self.reporter(channel, now, max_edits=1)
        reporter.update("Starting")
        now[0] += 60
        reporter.update("Working")
        now[0] += 60
        reporter.update("Still working")

        outcome = reporter.finish("Done: 3 listings updated.")

        self.assertEqual(outcome.state, ProgressState.EDITED)
        self.assertEqual(channel.edits[-1], "Done: 3 listings updated.")

    def test_a_failed_write_stops_rather_than_duplicating_a_status_message(self) -> None:
        from assistant.scotty_business.progress import ProgressState

        now = [0.0]
        channel = FakeChannel(fail_after=0)
        reporter = self.reporter(channel, now)
        self.assertEqual(reporter.update("Starting").state, ProgressState.UNAVAILABLE)
        now[0] += 60
        self.assertEqual(reporter.update("Again").state, ProgressState.UNAVAILABLE)
        self.assertEqual(channel.posts, [])
        self.assertEqual(channel.edits, [])

    def test_empty_or_non_text_updates_write_nothing(self) -> None:
        from assistant.scotty_business.progress import ProgressState

        now = [0.0]
        channel = FakeChannel()
        reporter = self.reporter(channel, now)
        for value in ("", "   ", None, 7, ["update"]):
            with self.subTest(value=value):
                self.assertEqual(reporter.update(value).state, ProgressState.UNAVAILABLE)
        self.assertEqual(channel.posts, [])

    def test_an_update_is_truncated_to_a_bounded_length(self) -> None:
        now = [0.0]
        channel = FakeChannel()
        reporter = self.reporter(channel, now)
        reporter.update("x" * 5000)
        self.assertLessEqual(len(channel.posts[0]), 1900)

    def test_the_interval_and_caps_can_be_tightened_but_never_widened(self) -> None:
        from assistant.scotty_business.progress import MAX_EDITS, MIN_EDIT_INTERVAL_SECONDS

        now = [0.0]
        reporter = self.reporter(
            FakeChannel(), now, min_interval=0.0, max_edits=10_000, max_posts=99
        )
        self.assertEqual(reporter.min_interval, MIN_EDIT_INTERVAL_SECONDS)
        self.assertEqual(reporter.max_edits, MAX_EDITS)
        self.assertLessEqual(reporter.max_posts, 3)


class AnnouncementProposalTests(unittest.TestCase):
    """A leak must be refused before a proposal exists, not at execution."""

    def service(self):
        import tempfile

        from assistant.scotty_business.approvals import ApprovalStore
        from assistant.scotty_business.service import ScottyService

        self._directory = tempfile.TemporaryDirectory(prefix="scotty-announce-")
        self.addCleanup(self._directory.cleanup)
        store = ApprovalStore(f"{self._directory.name}/approvals.db")
        store.initialize()
        unused = object()
        return ScottyService(
            synthetic.config(),
            store,
            trello=unused,
            ghl=unused,
            rentcast=None,
            discord=unused,
        )

    def test_an_ordinary_status_announcement_becomes_a_proposal(self) -> None:
        service = self.service()
        proposal = service.propose_discord_announcement(
            operator(), synthetic.ANNOUNCEMENT_CHANNEL, "Three listings were updated today."
        )
        self.assertEqual(proposal.action_class, "discord_announcement")

    def test_leaking_content_is_refused_without_repeating_it(self) -> None:
        from assistant.scotty_business.adapters.http import ProviderError

        service = self.service()
        for content in (
            f"see {synthetic.EMPLOYEE_CHANNEL}",
            f"ask {synthetic.OPERATOR_USER}",
            f"route {synthetic.ROUTE_CHANNEL}",
            synthetic.ROUTE_PROFILE,
            "api key is synthetic-value-000000000000",
        ):
            with self.subTest(content=content):
                with self.assertRaises(ProviderError) as caught:
                    service.propose_discord_announcement(
                        operator(), synthetic.ANNOUNCEMENT_CHANNEL, content
                    )
                self.assertNotIn(content, str(caught.exception))

    def test_an_announcement_outside_the_configured_destinations_is_refused(self) -> None:
        from assistant.scotty_business.adapters.http import ProviderError

        service = self.service()
        for channel in (synthetic.OPERATOR_CHANNEL, synthetic.ROUTE_CHANNEL):
            with self.subTest(channel=channel), self.assertRaises(ProviderError):
                service.propose_discord_announcement(operator(), channel, "Weekly summary.")

    def test_a_mass_mention_announcement_is_refused_even_with_approval_available(self) -> None:
        from assistant.scotty_business.adapters.http import ProviderError

        service = self.service()
        with self.assertRaises(ProviderError):
            service.propose_discord_announcement(
                operator(), synthetic.ANNOUNCEMENT_CHANNEL, "@everyone weekly summary"
            )


class DiscordThroughTheReadToolTests(unittest.TestCase):
    """The typed surface must be reachable, bounded, and per-caller."""

    def runtime(self):
        from test_provider_connection import runtime

        return runtime(DISCORD_BOT_TOKEN="synthetic-discord")

    def recorder(self, runtime):
        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[object, ...]]] = []

            def __getattr__(self, name):
                def call(*args, **kwargs):
                    self.calls.append((name, args))
                    if name == "read_channel":
                        return ({"id": OPERATOR_MESSAGE},)
                    if name == "create_thread":
                        return "950000000000000001"
                    if name in {"delete_own_message", "add_reaction", "archive_own_thread"}:
                        return True
                    return {"message_id": OPERATOR_MESSAGE, "channel_id": args[0]}

                return call

        recorder = Recorder()
        runtime.discord = recorder
        return recorder

    def read(self, runtime, principal, **payload):
        operation = payload.pop("discord_operation")
        return runtime.handle_read(
            principal,
            {"operation": "discord", "discord_operation": operation, "payload": payload},
        )

    def test_ordinary_work_in_the_callers_own_channel_succeeds(self) -> None:
        with self.runtime() as runtime:
            recorder = self.recorder(runtime)
            self.read(
                runtime,
                operator(),
                discord_operation="send_message",
                channel_id=synthetic.OPERATOR_CHANNEL,
                content="On it.",
            )
            self.read(
                runtime,
                operator(),
                discord_operation="read_channel",
                channel_id=synthetic.OPERATOR_CHANNEL,
                limit=5,
            )
            self.assertEqual([name for name, _ in recorder.calls], ["send_message", "read_channel"])

    def test_the_channel_defaults_to_the_callers_own_channel(self) -> None:
        with self.runtime() as runtime:
            recorder = self.recorder(runtime)
            self.read(runtime, employee(), discord_operation="send_message", content="Noted.")
            self.assertEqual(recorder.calls[-1][1][0], synthetic.EMPLOYEE_CHANNEL)

    def test_neither_client_can_act_in_the_other_private_channel(self) -> None:
        with self.runtime() as runtime:
            recorder = self.recorder(runtime)
            for principal, channel in (
                (operator(), synthetic.EMPLOYEE_CHANNEL),
                (employee(), synthetic.OPERATOR_CHANNEL),
            ):
                for operation in ("send_message", "read_channel", "reply_message"):
                    with (
                        self.subTest(role=principal.role, operation=operation),
                        self.assertRaises(PermissionError),
                    ):
                        self.read(
                            runtime,
                            principal,
                            discord_operation=operation,
                            channel_id=channel,
                            content="hello",
                            message_id=OPERATOR_MESSAGE,
                        )
            self.assertEqual(recorder.calls, [])

    def test_the_maintainer_route_is_unreachable_from_the_client_tool(self) -> None:
        with self.runtime() as runtime:
            recorder = self.recorder(runtime)
            with self.assertRaises(PermissionError):
                self.read(
                    runtime,
                    operator(),
                    discord_operation="send_message",
                    channel_id=synthetic.ROUTE_CHANNEL,
                    content="hello",
                )
            self.assertEqual(recorder.calls, [])

    def test_a_consequence_action_is_refused_here_and_left_to_a_proposal(self) -> None:
        with self.runtime() as runtime:
            recorder = self.recorder(runtime)
            with self.assertRaises(PermissionError) as caught:
                runtime.handle_read(
                    operator(),
                    {
                        "operation": "discord",
                        "discord_operation": "announce",
                        "payload": {
                            "channel_id": synthetic.ANNOUNCEMENT_CHANNEL,
                            "content": "Weekly summary.",
                        },
                    },
                )
            self.assertIn("needs an approved proposal", str(caught.exception))
            self.assertEqual(recorder.calls, [])

    def test_an_administrative_action_is_refused_as_absent(self) -> None:
        with self.runtime() as runtime:
            recorder = self.recorder(runtime)
            for operation in ("create_channel", "ban_member", "create_webhook", "purge_history"):
                with self.subTest(operation=operation):
                    with self.assertRaises(PermissionError) as caught:
                        runtime.handle_read(
                            operator(),
                            {
                                "operation": "discord",
                                "discord_operation": operation,
                                "payload": {
                                    "channel_id": synthetic.OPERATOR_CHANNEL,
                                    "content": "x",
                                },
                            },
                        )
                    self.assertIn("not one Scotty performs", str(caught.exception))
            self.assertEqual(recorder.calls, [])

    def test_an_attachment_is_read_only_from_scottys_own_outbox(self) -> None:
        with self.runtime() as runtime:
            recorder = self.recorder(runtime)
            outbox = runtime.state_dir / "outbox"
            outbox.mkdir(parents=True, exist_ok=True)
            (outbox / "summary.md").write_text("# synthetic", encoding="utf-8")
            self.read(
                runtime,
                operator(),
                discord_operation="attach_file",
                channel_id=synthetic.OPERATOR_CHANNEL,
                content="Here it is.",
                filename="summary.md",
                size_bytes=11,
            )
            self.assertEqual(recorder.calls[-1][0], "attach_file")

            for filename in ("../../etc/passwd", "absent.md", ".hidden.md", "notes.exe"):
                with (
                    self.subTest(filename=filename),
                    self.assertRaises((ValueError, PermissionError)),
                ):
                    self.read(
                        runtime,
                        operator(),
                        discord_operation="attach_file",
                        channel_id=synthetic.OPERATOR_CHANNEL,
                        content="Here it is.",
                        filename=filename,
                        size_bytes=11,
                    )

    def test_progress_updates_coalesce_across_calls_for_one_task(self) -> None:
        with self.runtime() as runtime:
            recorder = self.recorder(runtime)
            first = self.read(
                runtime,
                operator(),
                discord_operation="update_progress",
                channel_id=synthetic.OPERATOR_CHANNEL,
                content="Starting",
                task_id="task-1",
            )
            second = self.read(
                runtime,
                operator(),
                discord_operation="update_progress",
                channel_id=synthetic.OPERATOR_CHANNEL,
                content="Halfway",
                task_id="task-1",
            )
            self.assertEqual(first["progress"], "posted")
            self.assertEqual(second["progress"], "coalesced")
            self.assertEqual([name for name, _ in recorder.calls], ["send_message"])


if __name__ == "__main__":
    unittest.main()
