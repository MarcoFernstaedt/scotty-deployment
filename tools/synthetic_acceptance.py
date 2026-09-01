"""Credential-free synthetic acceptance run.

This proves the release behaviours end to end without any credential, live
provider, Discord call, container, or host mutation. Every identifier comes from
the synthetic fixtures in `fixtures/`.

Run it with:

    make acceptance
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assistant.scotty_business import client_tool_schemas, identity_prompt  # noqa: E402
from assistant.scotty_business.approvals import ApprovalStore  # noqa: E402
from assistant.scotty_business.config import RuntimeConfig  # noqa: E402
from assistant.scotty_business.guidance import (  # noqa: E402
    NOT_CONNECTED,
    PROVIDERS,
    provider_guidance,
)
from assistant.scotty_business.ingress import (  # noqa: E402
    EMPLOYEE_SUMMARY_COMMAND,
    IngressGuard,
)
from assistant.scotty_business.policy import (  # noqa: E402
    CODING_REFUSAL,
    FIXED_WIZARD_COMMAND,
    Principal,
    Role,
    can_approve,
    employee_summary,
    setup_wizard,
)
from assistant.scotty_business.provisioning import (  # noqa: E402
    ChannelPlan,
    ProvisionStatus,
    ensure_private_channels,
    intended_overwrites,
)
from assistant.scotty_business.routing import (  # noqa: E402
    ALL_TOOLSETS,
    CLIENT_PROFILES,
    CLIENT_TOOLSETS,
    MAINTAINER_PROFILE,
    SERVED_PROFILES,
    RouteKind,
    match_profile_route,
    parse_profile_routes,
    resolve_route,
)
from assistant.scotty_business.setup import (  # noqa: E402
    CODEX_AUTH_COMMAND,
    CODEX_PROVIDER,
    DISCORD_ALLOWED_USERS_ENV,
    GUARD_PLUGIN,
    SetupInputs,
    discord_allowed_users,
    hermes_config_mapping,
    next_steps,
    private_mapping,
    profile_config_mapping,
    runtime_environment,
)
from assistant.scotty_guard.guard import (  # noqa: E402
    GuardConfig,
    MaintainerGuard,
)

FIXTURES = ROOT / "fixtures"
CHECKS: list[str] = []


class AcceptanceFailure(RuntimeError):
    pass


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AcceptanceFailure(label)
    CHECKS.append(label)


def config() -> RuntimeConfig:
    raw = json.loads((FIXTURES / "scotty.private.example.json").read_text("utf-8"))
    return RuntimeConfig.from_mapping(raw)


def discord_only_config() -> RuntimeConfig:
    raw = json.loads((FIXTURES / "scotty.private.discord-only.example.json").read_text("utf-8"))
    return RuntimeConfig.from_mapping(raw)


def setup_inputs(runtime_config: RuntimeConfig) -> SetupInputs:
    operator = principal_for(runtime_config, Role.MAIN_OPERATOR)
    employee = principal_for(runtime_config, Role.EMPLOYEE)
    route = runtime_config.maintainer_route
    return SetupInputs(
        model_provider=CODEX_PROVIDER,
        model_name="synthetic/codex",
        guild_id=operator.guild_id,
        operator_channel_id=operator.channel_id,
        operator_user_id=operator.user_id,
        employee_channel_id=employee.channel_id,
        employee_user_id=employee.user_id,
        route_guild_id=route.guild_id,
        route_channel_id=route.channel_id,
        route_user_id=route.user_id,
        secrets={"DISCORD_BOT_TOKEN": "synthetic-discord-token"},
    )


def source(guild: str, channel: str, user: str, *, parent: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        platform=SimpleNamespace(value="discord"),
        guild_id=guild,
        scope_id=guild,
        chat_id=channel,
        user_id=user,
        parent_chat_id=parent,
        is_bot=False,
    )


def principal_for(runtime_config: RuntimeConfig, role: Role) -> Principal:
    return next(item for item in runtime_config.principals if item.role == role)


def check_native_configuration(runtime_config: RuntimeConfig) -> None:
    mapping = hermes_config_mapping(setup_inputs(runtime_config))
    gateway = mapping["gateway"]
    assert isinstance(gateway, dict)
    check("gateway.multiplex_profiles is enabled", gateway["multiplex_profiles"] is True)
    routes = parse_profile_routes(mapping)
    check("the native parser sees exactly three profile routes", len(routes) == 3)
    check(
        "every routed profile is in the served allowlist",
        set(gateway["multiplex_profile_allowlist"]) == set(SERVED_PROFILES)
        and all(item.profile in SERVED_PROFILES for item in routes),
    )
    check(
        "every route uses only the native keys",
        all(
            set(entry) == {"name", "platform", "guild_id", "chat_id", "profile"}
            for entry in gateway["profile_routes"]
        ),
    )

    operator = principal_for(runtime_config, Role.MAIN_OPERATOR)
    employee = principal_for(runtime_config, Role.EMPLOYEE)
    private = runtime_config.maintainer_route
    check(
        "the exact private tuple resolves to the full profile natively",
        match_profile_route(routes, source(private.guild_id, private.channel_id, private.user_id))
        == MAINTAINER_PROFILE,
    )
    check(
        "the main-operator channel resolves to its bounded profile natively",
        match_profile_route(
            routes, source(operator.guild_id, operator.channel_id, operator.user_id)
        )
        == CLIENT_PROFILES[Role.MAIN_OPERATOR],
    )
    check(
        "the employee channel resolves to its bounded profile natively",
        match_profile_route(
            routes, source(employee.guild_id, employee.channel_id, employee.user_id)
        )
        == CLIENT_PROFILES[Role.EMPLOYEE],
    )
    for label, candidate in (
        ("wrong guild", source("999000000000000001", operator.channel_id, operator.user_id)),
        (
            "private channel in the client guild",
            source(operator.guild_id, private.channel_id, private.user_id),
        ),
        (
            "client channel in the private guild",
            source(private.guild_id, operator.channel_id, operator.user_id),
        ),
    ):
        check(
            f"native routing matches no route: {label}",
            match_profile_route(routes, candidate) is None,
        )

    inputs = setup_inputs(runtime_config)
    full = profile_config_mapping(MAINTAINER_PROFILE, inputs)
    check(
        "the full profile enables only the authorization guard",
        full["plugins"] == {"enabled": [GUARD_PLUGIN]},
    )
    check(
        "the full profile keeps the normal tool inventory",
        full["platform_toolsets"] == {"discord": ["*"]},
    )
    check(
        "the full profile config carries no bounded Scotty identity",
        "scotty-business" not in json.dumps(full),
    )
    for name in CLIENT_PROFILES.values():
        bounded = profile_config_mapping(name, inputs)
        check(
            f"{name} enables only the bounded Scotty toolset",
            bounded["plugins"] == {"enabled": ["scotty-business"]}
            and bounded["platform_toolsets"] == {"discord": ["scotty"]},
        )
    check(
        "the base configuration is bounded so a failed override stays bounded",
        mapping["platform_toolsets"] == {"discord": ["scotty"]},
    )
    for name in SERVED_PROFILES:
        check(
            f"{name} keeps the setup-selected provider and model",
            profile_config_mapping(name, inputs)["model"] == mapping["model"],
        )

    environment = runtime_environment(inputs)
    allowed = discord_allowed_users(inputs)
    check(
        "the gateway sender allowlist is exactly the three configured users",
        environment[DISCORD_ALLOWED_USERS_ENV] == ",".join(allowed) and len(allowed) == 3,
    )
    rendered_env = "\n".join(f"{key}={value}" for key, value in environment.items())
    for forbidden in ("DISCORD_ALLOW_ALL_USERS", "DISCORD_ALLOWED_ROLES", "everyone"):
        check(f"no open sender policy is generated: {forbidden}", forbidden not in rendered_env)


def check_routing(runtime_config: RuntimeConfig) -> None:
    route = runtime_config.maintainer_route
    for role in (Role.MAIN_OPERATOR, Role.EMPLOYEE):
        principal = principal_for(runtime_config, role)
        resolved = resolve_route(
            runtime_config, source(principal.guild_id, principal.channel_id, principal.user_id)
        )
        check(
            f"client tuple for {role.value} routes to a bounded profile",
            resolved is not None
            and resolved.kind is RouteKind.CLIENT
            and resolved.toolsets == CLIENT_TOOLSETS,
        )
    exact = resolve_route(runtime_config, source(route.guild_id, route.channel_id, route.user_id))
    check(
        "the exact private tuple reaches the full profile",
        exact is not None and exact.kind is RouteKind.MAINTAINER and exact.toolsets == ALL_TOOLSETS,
    )

    operator = principal_for(runtime_config, Role.MAIN_OPERATOR)
    employee = principal_for(runtime_config, Role.EMPLOYEE)
    rejects = [
        ("wrong guild", source("999000000000000001", operator.channel_id, operator.user_id)),
        ("wrong channel", source(operator.guild_id, employee.channel_id, operator.user_id)),
        ("wrong user", source(operator.guild_id, operator.channel_id, employee.user_id)),
        (
            "client user in the private channel",
            source(route.guild_id, route.channel_id, employee.user_id),
        ),
        (
            "private user in a client channel",
            source(operator.guild_id, operator.channel_id, route.user_id),
        ),
        (
            "private channel in the client guild",
            source(operator.guild_id, route.channel_id, route.user_id),
        ),
        (
            "client channel in the private guild",
            source(route.guild_id, operator.channel_id, route.user_id),
        ),
        (
            "thread under the wrong parent",
            source(
                operator.guild_id,
                "900000000000000001",
                operator.user_id,
                parent=employee.channel_id,
            ),
        ),
    ]
    for label, candidate in rejects:
        check(
            f"rejected before dispatch: {label}", resolve_route(runtime_config, candidate) is None
        )

    bot = source(operator.guild_id, operator.channel_id, operator.user_id)
    bot.is_bot = True
    check("rejected before dispatch: bot author", resolve_route(runtime_config, bot) is None)


def check_maintainer_guard(runtime_config: RuntimeConfig) -> None:
    """The profile-local gate that supplies the user match native routing lacks."""

    route = runtime_config.maintainer_route
    operator = principal_for(runtime_config, Role.MAIN_OPERATOR)
    employee = principal_for(runtime_config, Role.EMPLOYEE)
    with tempfile.TemporaryDirectory(prefix="scotty-guard-acceptance-") as directory:
        sent: list[tuple[str, str]] = []
        guard = MaintainerGuard(
            GuardConfig(
                guild_id=route.guild_id,
                channel_id=route.channel_id,
                user_id=route.user_id,
                operator_channel_id=operator.channel_id,
                state_dir=Path(directory),
            ),
            send=lambda channel, text: sent.append((channel, text)),
        )

        def guard_event(
            guild: str, channel: str, user: str, text: str = "hello"
        ) -> SimpleNamespace:
            return SimpleNamespace(
                text=text,
                message_id="800000000000000001",
                source=source(guild, channel, user),
            )

        check(
            "the guard admits the exact maintainer tuple",
            guard(guard_event(route.guild_id, route.channel_id, route.user_id))
            == {"action": "allow"},
        )
        denials = {
            "unknown sender": ("999000000000000001",),
            "operator in the private channel": (operator.user_id,),
            "employee in the private channel": (employee.user_id,),
        }
        for label, (user,) in denials.items():
            check(
                f"the guard denies {label}",
                guard(guard_event(route.guild_id, route.channel_id, user))
                == {"action": "skip", "reason": "unauthorized"},
            )
        for label, candidate in (
            (
                "the maintainer in a client channel",
                guard_event(operator.guild_id, operator.channel_id, route.user_id),
            ),
            (
                "a wrong-parent thread",
                SimpleNamespace(
                    text="hello",
                    message_id="800000000000000002",
                    source=source(
                        route.guild_id,
                        "900000000000000001",
                        route.user_id,
                    ),
                ),
            ),
        ):
            check(
                f"the guard denies {label}",
                guard(candidate) == {"action": "skip", "reason": "unauthorized"},
            )
        check("no guard denial ever replies", sent == [])

        wizard = guard_event(route.guild_id, route.channel_id, route.user_id, FIXED_WIZARD_COMMAND)
        check(
            "the exact trigger is handled before model execution",
            guard(wizard) == {"action": "skip", "reason": "fixed-wizard"},
        )
        check(
            "the fixed wizard reaches only the main-operator channel",
            sent == [(operator.channel_id, setup_wizard("Scotty"))],
        )
        guard(wizard)
        check("one inbound message delivers the wizard exactly once", len(sent) == 1)


def check_fixed_paths(runtime_config: RuntimeConfig) -> None:
    outbound: list[tuple[str, str]] = []
    guard = IngressGuard(runtime_config, lambda channel, text: outbound.append((channel, text)))
    operator = principal_for(runtime_config, Role.MAIN_OPERATOR)
    employee = principal_for(runtime_config, Role.EMPLOYEE)

    maintainer_source = source(
        runtime_config.maintainer_route.guild_id,
        runtime_config.maintainer_route.channel_id,
        runtime_config.maintainer_route.user_id,
    )
    guard(SimpleNamespace(text=FIXED_WIZARD_COMMAND, source=maintainer_source))
    check(
        "the root hook also routes the fixed wizard only to the main operator",
        outbound == [(operator.channel_id, setup_wizard("Scotty"))],
    )
    outbound.clear()
    for wrong in (
        SimpleNamespace(
            text=FIXED_WIZARD_COMMAND,
            source=source(operator.guild_id, operator.channel_id, operator.user_id),
        ),
        SimpleNamespace(
            text=FIXED_WIZARD_COMMAND,
            source=source(employee.guild_id, employee.channel_id, employee.user_id),
        ),
    ):
        guard(wrong)
    check("no client principal can trigger the wizard", outbound == [])

    guard(
        SimpleNamespace(
            text=EMPLOYEE_SUMMARY_COMMAND,
            source=source(operator.guild_id, operator.channel_id, operator.user_id),
        )
    )
    check(
        "the fixed employee summary reaches only the employee channel",
        outbound == [(employee.channel_id, employee_summary("Assistant"))],
    )

    outbound.clear()
    guard(
        SimpleNamespace(
            text="Please build an integration for me",
            source=source(employee.guild_id, employee.channel_id, employee.user_id),
        )
    )
    check(
        "coding requests get the fixed refusal in the requesting channel",
        outbound == [(employee.channel_id, CODING_REFUSAL)],
    )

    route = runtime_config.maintainer_route
    destinations = {channel for channel, _ in outbound}
    check(
        "no fixed path ever targets the private route",
        route.channel_id not in destinations
        and route.channel_id not in runtime_config.client_discord_destinations(),
    )


def check_employee_denial(runtime_config: RuntimeConfig) -> None:
    employee = principal_for(runtime_config, Role.EMPLOYEE)
    operator = principal_for(runtime_config, Role.MAIN_OPERATOR)
    check("an employee cannot approve a Trello write", not can_approve(employee, "trello_write"))
    check("an employee cannot approve an SMS send", not can_approve(employee, "ghl_sms"))
    check("the main operator can approve a Trello write", can_approve(operator, "trello_write"))

    trello = runtime_config.trello
    assert trello is not None
    with tempfile.TemporaryDirectory(prefix="scotty-acceptance-") as directory:
        store = ApprovalStore(Path(directory) / "approvals.db")
        store.initialize()
        from datetime import UTC, datetime, timedelta

        proposal = store.propose(
            requester=employee,
            approver=operator,
            action_class="trello_write",
            target_ids=(trello.board_id, trello.list_ids[0]),
            payload={"operation": "create", "fields": {"name": "Synthetic acceptance card"}},
            source_revision="configured-board-v1",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        denied = False
        try:
            store.approve(proposal.proposal_id, employee, proposal.version)
        except Exception:
            denied = True
        check("an employee self-approval is refused by the approval store", denied)
        approved = store.approve(proposal.proposal_id, operator, proposal.version)
        check("the bound approver can approve", approved.status.value == "approved")


def check_provider_guidance() -> None:
    for name in PROVIDERS:
        text = provider_guidance(name).as_text()
        check(f"{name} states not connected with fixed steps", NOT_CONNECTED in text)
        check(f"{name} points at the local setup command", "local setup command" in text)
    discord = provider_guidance("discord")
    check(
        "Discord guidance asks for Manage Channels, never Administrator",
        any("Manage Channels" in item for item in discord.required_scopes)
        and not any("Administrator" in item for item in discord.required_scopes),
    )
    google = provider_guidance("google_workspace")
    check(
        "Google Workspace is a bounded installed capability",
        "bounded release capability" in google.as_text().lower(),
    )


def check_google_action_bounds() -> None:
    """Bulk, destructive and sharing writes reach the approval ledger."""

    from assistant.scotty_business.google_policy import (
        MAX_DOCS_REQUESTS,
        MAX_SHEETS_RANGES,
        GoogleActionClass,
        classify_google_action,
    )

    bounded: dict[str, object] = {
        "requests": [{"insertText": {}} for _ in range(MAX_DOCS_REQUESTS)]
    }
    check(
        "a small bounded document edit stays routine",
        classify_google_action("docs_batch_update", bounded) is GoogleActionClass.ROUTINE,
    )
    for label, operation, payload in (
        (
            "an oversized document batch",
            "docs_batch_update",
            {"requests": [{"insertText": {}} for _ in range(MAX_DOCS_REQUESTS + 1)]},
        ),
        (
            "an oversized values update",
            "sheets_update_values",
            {"data": [{"range": f"A{n}"} for n in range(MAX_SHEETS_RANGES + 1)]},
        ),
        ("a destructive batch request", "sheets_batch_update", {"requests": [{"deleteSheet": {}}]}),
        ("a sharing change", "drive_update_file", {"permissions": [{"type": "anyone"}]}),
    ):
        check(
            f"{label} requires approval",
            classify_google_action(operation, payload) is GoogleActionClass.CONSEQUENCE,
        )
    for label, operation in (
        ("an admin action", "admin_change_user"),
        ("a credential action", "credential_rotate"),
        ("an unknown action", "drive_delete"),
    ):
        check(
            f"{label} fails closed",
            classify_google_action(operation, {}) is GoogleActionClass.FORBIDDEN,
        )


def check_google_read_bounds() -> None:
    """Bounded reads validate their arguments before any provider call."""

    from assistant.scotty_business.adapters.google_workspace import (
        MAX_SHEETS_READ_RANGES,
        GoogleWorkspaceAdapter,
    )
    from assistant.scotty_business.adapters.http import (
        Attachment,
        HttpResponse,
        ProviderError,
    )
    from assistant.scotty_business.config import RuntimeConfig

    class RefusingTransport:
        def __init__(self) -> None:
            self.calls = 0

        def request(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str] | None = None,
            query: Mapping[str, object] | None = None,
            json_body: Mapping[str, object] | None = None,
            attachment: Attachment | None = None,
            text: bool = False,
        ) -> HttpResponse:
            self.calls += 1
            raise AssertionError("a bounded read must validate before calling out")

    fixture = json.loads(
        (ROOT / "fixtures" / "scotty.private.example.json").read_text(encoding="utf-8")
    )
    scope = RuntimeConfig.from_mapping(fixture).google_for(Role.MAIN_OPERATOR)
    assert scope is not None
    transport = RefusingTransport()
    adapter = GoogleWorkspaceAdapter(transport, "synthetic-access-token", scope)

    calls: tuple[tuple[str, Callable[[], object]], ...] = (
        ("a malformed spreadsheet range", lambda: adapter.get_sheet_values("sheet-1", "A1; DROP")),
        ("an empty range batch", lambda: adapter.batch_get_sheet_values("sheet-1", [])),
        (
            "an oversized range batch",
            lambda: adapter.batch_get_sheet_values(
                "sheet-1", [f"A{n}" for n in range(MAX_SHEETS_READ_RANGES + 1)]
            ),
        ),
    )
    for label, call in calls:
        refused = False
        try:
            call()
        except ProviderError:
            refused = True
        check(f"{label} is refused", refused)
    check("no bounded read reached the transport", transport.calls == 0)


class SyntheticDiscord:
    """Synthetic Discord REST double. No socket is ever opened."""

    def __init__(self, guild_id: str, bot_id: str) -> None:
        self.guild_id = guild_id
        self.bot_id = bot_id
        self.channels: list[dict[str, object]] = []
        self.creates = 0
        self._next = 700000000000000001

    def get(self, path: str) -> object:
        if path == "/users/@me":
            return {"id": self.bot_id}
        if path == f"/guilds/{self.guild_id}":
            return {"id": self.guild_id}
        if path == f"/guilds/{self.guild_id}/members/@me":
            return {"user": {"id": self.bot_id}, "roles": ["400000000000000001"]}
        if path == f"/guilds/{self.guild_id}/roles":
            return [
                {"id": self.guild_id, "permissions": "0"},
                {"id": "400000000000000001", "permissions": str(1 << 4)},
            ]
        if path == f"/guilds/{self.guild_id}/channels":
            return [dict(item) for item in self.channels]
        channel_id = path.rsplit("/", 1)[-1]
        for channel in self.channels:
            if channel["id"] == channel_id:
                return dict(channel)
        raise RuntimeError("channel readback unavailable")

    def post(self, path: str, json_body: Mapping[str, object]) -> object:
        self.creates += 1
        channel = {
            "id": str(self._next),
            "guild_id": self.guild_id,
            "type": 0,
            "name": json_body["name"],
            "permission_overwrites": json_body["permission_overwrites"],
        }
        self._next += 1
        self.channels.append(channel)
        return dict(channel)


def check_provisioning(runtime_config: RuntimeConfig) -> None:
    operator = principal_for(runtime_config, Role.MAIN_OPERATOR)
    employee = principal_for(runtime_config, Role.EMPLOYEE)
    bot_id = "600000000000000001"
    plans = (
        ChannelPlan(
            key="main_operator",
            name="scotty-operator",
            guild_id=operator.guild_id,
            user_id=operator.user_id,
        ),
        ChannelPlan(
            key="employee",
            name="scotty-employee",
            guild_id=employee.guild_id,
            user_id=employee.user_id,
        ),
    )
    client = SyntheticDiscord(operator.guild_id, bot_id)

    declined = ensure_private_channels(plans, client, confirm=lambda _: False)
    check("nothing is created without a local confirmation", client.creates == 0)
    check("a declined preview reports an error", declined.error is not None)

    first = ensure_private_channels(plans, client, confirm=lambda _: True)
    check("both private channels are created after confirmation", first.error is None)
    check("exactly two channels were created", client.creates == 2)
    for plan in plans:
        created = first.channels[plan.key]
        check(
            f"{plan.key} channel was created and read back",
            created.status is ProvisionStatus.CREATED and created.channel_id is not None,
        )

    second = ensure_private_channels(plans, client, confirm=lambda _: True)
    check("a rerun reuses both channels", second.error is None and client.creates == 2)
    check(
        "a rerun returns the same channel IDs",
        all(
            second.channels[plan.key].channel_id == first.channels[plan.key].channel_id
            for plan in plans
        ),
    )

    overwrites = intended_overwrites(operator.guild_id, operator.user_id, bot_id)
    everyone = next(item for item in overwrites if item["id"] == operator.guild_id)
    check("@everyone is denied View Channel", int(str(everyone["deny"])) & (1 << 10) != 0)
    check(
        "no in-channel overwrite grants Administrator or Manage Channels",
        all(int(str(item["allow"])) & ((1 << 3) | (1 << 4)) == 0 for item in overwrites),
    )


def check_codex_and_optional_providers(runtime_config: RuntimeConfig) -> None:
    inputs = setup_inputs(discord_only_config())
    check("Codex setup needs no model API key", set(inputs.secrets) == {"DISCORD_BOT_TOKEN"})
    steps = "\n".join(next_steps(inputs))
    check("Codex setup names the runtime's own OAuth command", CODEX_AUTH_COMMAND in steps)
    check("no OAuth material is ever collected", "refresh_token" not in steps)
    mapping = private_mapping(inputs)
    for absent in ("trello", "ghl", "rentcast"):
        check(f"{absent} stays absent rather than a placeholder", absent not in mapping)
    bare = discord_only_config()
    check("a Discord-only deployment still loads", bare.trello is None)
    check("a Discord-only deployment reports no RentCast scope", bare.rentcast_endpoints == ())
    del runtime_config


#: What a client must never be told they are talking to.
_UPSTREAM_BRANDS = (
    "hermes",
    "nous research",
    "nousresearch",
    "openclaw",
    "openrouter",
    "anthropic",
    "claude",
    "openai",
    "gpt-",
    "codex",
    "docker",
    "systemd",
)


def check_white_label(runtime_config: RuntimeConfig) -> None:
    """No client-visible surface advertises what this assistant runs on."""

    del runtime_config
    surfaces = {
        "identity prompt": identity_prompt("Assistant"),
        "setup wizard": setup_wizard("Scotty"),
        "employee summary": employee_summary("Assistant"),
        "coding refusal": CODING_REFUSAL,
        "tool schemas": json.dumps(client_tool_schemas()),
        **{f"{name} guidance": provider_guidance(name).as_text() for name in PROVIDERS},
    }
    for label, text in surfaces.items():
        lowered = text.casefold()
        check(
            f"the {label} names no framework or model provider",
            not any(brand in lowered for brand in _UPSTREAM_BRANDS),
        )
    check(
        "each client user's assistant name is their own",
        setup_wizard("Scotty") != setup_wizard("Nova") and "Scotty" not in employee_summary("Nova"),
    )


def check_secrecy(runtime_config: RuntimeConfig) -> None:
    route = runtime_config.maintainer_route
    identifiers = (route.guild_id, route.channel_id, route.user_id)
    client_text = "\n".join(
        [
            employee_summary("Assistant"),
            CODING_REFUSAL,
            setup_wizard("Scotty"),
            FIXED_WIZARD_COMMAND,
            *(provider_guidance(name).as_text() for name in PROVIDERS),
            *(
                json.dumps(profile_config_mapping(name, setup_inputs(runtime_config)))
                for name in CLIENT_PROFILES.values()
            ),
        ]
    )
    for identifier in identifiers:
        check(
            "no client-facing string carries a private route identifier",
            identifier not in client_text,
        )
    lowered = client_text.lower()
    for phrase in ("maintainer server", "maintainer channel", "hidden route", "admin route"):
        check(f"no client-facing string mentions a {phrase}", phrase not in lowered)


def main() -> int:
    runtime_config = config()
    check_native_configuration(runtime_config)
    check_routing(runtime_config)
    check_maintainer_guard(runtime_config)
    check_fixed_paths(runtime_config)
    check_employee_denial(runtime_config)
    check_provider_guidance()
    check_google_action_bounds()
    check_google_read_bounds()
    check_codex_and_optional_providers(runtime_config)
    check_provisioning(runtime_config)
    check_white_label(runtime_config)
    check_secrecy(runtime_config)
    for label in CHECKS:
        print(f"  ok  {label}")
    print(f"synthetic acceptance: PASS ({len(CHECKS)} checks, no credentials, no live calls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
