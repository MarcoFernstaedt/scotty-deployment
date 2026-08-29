from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from .adapters.http import HttpTransport, ProviderError, RedactedMapping, require_success
from .policy import Role
from .provisioning import (
    ChannelPlan,
    DiscordProvisioningApi,
    DiscordProvisioningClient,
    ProvisionStatus,
    ensure_private_channels,
)
from .routing import client_profile

_DATA_DIR = Path("/srv/Scotty/data")
_VIEW_CHANNEL = 1 << 10
_MODEL_SECRET_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
_SECRET_NAMES = (
    "DISCORD_BOT_TOKEN",
    "SCOTTY_TRELLO_API_KEY",
    "SCOTTY_TRELLO_TOKEN",
    "SCOTTY_GHL_PRIVATE_TOKEN",
    "SCOTTY_RENTCAST_API_KEY",
)
_SAFE_SECRET = re.compile(r"[A-Za-z0-9._:/+\-=]+")
_SAFE_VALUE = re.compile(r"[A-Za-z0-9._:/+\-]+")


class SetupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SetupInputs:
    model_provider: str
    model_name: str
    guild_id: str
    maintainer_channel_id: str
    maintainer_user_id: str
    operator_channel_id: str
    operator_user_id: str
    employee_channel_id: str
    employee_user_id: str
    announcement_channel_ids: tuple[str, ...]
    trello_board_id: str
    trello_list_ids: tuple[str, ...]
    trello_label_ids: tuple[str, ...]
    trello_custom_field_ids: tuple[str, ...]
    ghl_location_id: str
    secrets: Mapping[str, str]
    maintainer_route_guild_id: str = ""
    maintainer_route_channel_id: str = ""
    maintainer_route_user_id: str = ""
    maintainer_route_profile: str = ""
    provision_channel_names: Mapping[str, str] | None = None

    def route_fields(self) -> tuple[str, str, str, str]:
        return (
            self.maintainer_route_guild_id,
            self.maintainer_route_channel_id,
            self.maintainer_route_user_id,
            self.maintainer_route_profile,
        )


class DiscordSetupReader(Protocol):
    def get(self, path: str) -> object: ...


def _visible(input_fn: Callable[[str], str], prompt: str) -> str:
    value = input_fn(prompt).strip()
    if not value or len(value) > 256 or any(ord(char) < 32 for char in value):
        raise SetupError("setup value is missing or malformed")
    return value


def _csv(
    input_fn: Callable[[str], str], prompt: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    raw = input_fn(prompt).strip()
    if not raw and allow_empty:
        return ()
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values or len(values) != len(set(values)):
        raise SetupError("setup list is empty or contains duplicates")
    for value in values:
        if len(value) > 128 or not _SAFE_VALUE.fullmatch(value):
            raise SetupError("setup list contains a malformed value")
    return values


def _hidden(hidden_fn: Callable[[str], str], prompt: str) -> str:
    value = hidden_fn(prompt)
    if not value or len(value) > 4096 or not _SAFE_SECRET.fullmatch(value):
        raise SetupError("hidden credential is missing or contains unsupported characters")
    return value


def collect_inputs(
    *,
    input_fn: Callable[[str], str] = input,
    hidden_fn: Callable[[str], str] = getpass.getpass,
    environ: Mapping[str, str] | None = None,
) -> SetupInputs:
    environ = os.environ if environ is None else environ
    provider = _visible(input_fn, "Model provider (openrouter, openai, anthropic): ").lower()
    if provider not in _MODEL_SECRET_ENV:
        raise SetupError("model provider is not supported by bounded setup")
    model_name = _visible(input_fn, "Model name: ")
    guild_id = _visible(input_fn, "Discord guild ID: ")
    maintainer_channel = _visible(input_fn, "Maintainer private channel ID: ")
    maintainer_user = _visible(input_fn, "Maintainer Discord user ID: ")
    provision = _visible(
        input_fn, "Create the operator and employee private channels now? (yes/no): "
    ).lower()
    if provision not in {"yes", "no"}:
        raise SetupError("answer the provisioning question with yes or no")
    provision_names: dict[str, str] | None = None
    if provision == "yes":
        provision_names = {
            "main_operator": _visible(input_fn, "Main-operator private channel name: ").lower(),
            "employee": _visible(input_fn, "Employee private channel name: ").lower(),
        }
        operator_channel = ""
        employee_channel = ""
    else:
        operator_channel = _visible(input_fn, "Main-operator private channel ID: ")
        employee_channel = _visible(input_fn, "Employee private channel ID: ")
    operator_user = _visible(input_fn, "Main-operator Discord user ID: ")
    employee_user = _visible(input_fn, "Employee Discord user ID: ")
    announcements = _csv(
        input_fn, "Configured Discord announcement channel IDs (comma-separated): "
    )
    board_id = _visible(input_fn, "Trello board ID: ")
    list_ids = _csv(input_fn, "Trello list IDs (comma-separated): ")
    label_ids = _csv(
        input_fn, "Trello label IDs (comma-separated, blank for none): ", allow_empty=True
    )
    custom_fields = _csv(
        input_fn,
        "Trello custom-field IDs (comma-separated, blank for none): ",
        allow_empty=True,
    )
    location_id = _visible(input_fn, "GoHighLevel location ID: ")
    route = _visible(input_fn, "Configure the private full-profile route? (yes/no): ").lower()
    if route not in {"yes", "no"}:
        raise SetupError("answer the route question with yes or no")
    route_guild = route_channel = route_user = route_profile = ""
    if route == "yes":
        route_guild = _visible(input_fn, "Route guild ID: ")
        route_channel = _visible(input_fn, "Route private channel ID: ")
        route_user = _visible(input_fn, "Route Discord user ID: ")
        route_profile = _visible(input_fn, "Route profile name: ").lower()
    secrets: dict[str, str] = {}
    secrets[_MODEL_SECRET_ENV[provider]] = _hidden(hidden_fn, "Model API credential (hidden): ")
    for name, prompt in (
        ("DISCORD_BOT_TOKEN", "Discord bot token (hidden): "),
        ("SCOTTY_TRELLO_API_KEY", "Trello API key (hidden): "),
        ("SCOTTY_TRELLO_TOKEN", "Trello token (hidden): "),
        ("SCOTTY_GHL_PRIVATE_TOKEN", "GoHighLevel Private Integration Token (hidden): "),
        ("SCOTTY_RENTCAST_API_KEY", "RentCast API key (hidden): "),
    ):
        # A credential is read from hidden terminal input, or from the process
        # environment when the operator exported it. It is never read from argv.
        environment = environ.get(name)
        if environment:
            if not _SAFE_SECRET.fullmatch(environment):
                raise SetupError("an environment credential contains unsupported characters")
            secrets[name] = environment
            continue
        secrets[name] = _hidden(hidden_fn, prompt)
    return SetupInputs(
        model_provider=provider,
        model_name=model_name,
        guild_id=guild_id,
        maintainer_channel_id=maintainer_channel,
        maintainer_user_id=maintainer_user,
        operator_channel_id=operator_channel,
        operator_user_id=operator_user,
        employee_channel_id=employee_channel,
        employee_user_id=employee_user,
        announcement_channel_ids=announcements,
        trello_board_id=board_id,
        trello_list_ids=list_ids,
        trello_label_ids=label_ids,
        trello_custom_field_ids=custom_fields,
        ghl_location_id=location_id,
        secrets=secrets,
        maintainer_route_guild_id=route_guild,
        maintainer_route_channel_id=route_channel,
        maintainer_route_user_id=route_user,
        maintainer_route_profile=route_profile,
        provision_channel_names=provision_names,
    )


class DiscordSetupClient:
    def __init__(self, token: str):
        self.transport = HttpTransport(timeout=20.0, max_response_bytes=262_144)
        self.headers = RedactedMapping(Authorization=f"Bot {token}")

    def get(self, path: str) -> object:
        if not path.startswith("/") or ".." in path or "?" in path or "#" in path:
            raise SetupError("Discord setup path is invalid")
        try:
            response = self.transport.request(
                "GET", f"https://discord.com/api/v10{path}", headers=self.headers
            )
            return require_success(response)
        except ProviderError as exc:
            raise SetupError("Discord setup validation failed") from exc


def _snowflake(value: object, field: str) -> str:
    if type(value) is not str or not value.isdigit() or not 1 <= len(value) <= 20:
        raise SetupError(f"{field} must be a Discord numeric ID")
    return value


def _private_channel(channel: Mapping[str, object], guild_id: str) -> bool:
    overwrites = channel.get("permission_overwrites")
    if not isinstance(overwrites, list):
        return False
    for overwrite in overwrites:
        if not isinstance(overwrite, dict):
            continue
        if overwrite.get("id") != guild_id or overwrite.get("type") != 0:
            continue
        deny = overwrite.get("deny")
        if type(deny) is not str or not deny.isdigit():
            return False
        return bool(int(deny) & _VIEW_CHANNEL)
    return False


def validate_discord_scope(inputs: SetupInputs, client: DiscordSetupReader) -> None:
    guild = _snowflake(inputs.guild_id, "guild_id")
    bot = client.get("/users/@me")
    if not isinstance(bot, dict) or type(bot.get("id")) is not str:
        raise SetupError("Discord bot identity is malformed")
    member = client.get(f"/guilds/{guild}/members/@me")
    if (
        not isinstance(member, dict)
        or not isinstance(member.get("user"), dict)
        or member["user"].get("id") != bot["id"]
    ):
        raise SetupError("Discord bot is not a verified member of the configured guild")
    channel_ids = tuple(
        dict.fromkeys(
            (
                inputs.maintainer_channel_id,
                inputs.operator_channel_id,
                inputs.employee_channel_id,
                *inputs.announcement_channel_ids,
            )
        )
    )
    if len(channel_ids) != 3 + len(inputs.announcement_channel_ids):
        raise SetupError("Discord principal and announcement channels must be distinct")
    for raw_channel_id in channel_ids:
        channel_id = _snowflake(raw_channel_id, "channel_id")
        channel = client.get(f"/channels/{channel_id}")
        if (
            not isinstance(channel, dict)
            or channel.get("id") != channel_id
            or channel.get("guild_id") != guild
        ):
            raise SetupError("Discord channel identity or guild mismatch")
        private = _private_channel(channel, guild)
        parent_id = channel.get("parent_id")
        if not private and type(parent_id) is str and parent_id:
            parent = client.get(f"/channels/{_snowflake(parent_id, 'parent_id')}")
            private = (
                isinstance(parent, dict)
                and parent.get("guild_id") == guild
                and _private_channel(parent, guild)
            )
        if not private:
            raise SetupError("every configured Discord channel must deny View Channel to @everyone")


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _yaml_list(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_yaml_string(value) for value in values) + "]"


def render_hermes_config(inputs: SetupInputs) -> str:
    """Render owner-only gateway configuration using proven pinned-runtime keys.

    The private route channel is admitted here so the gateway delivers its
    messages at all. Which profile and toolset that source receives is decided
    by the plugin before dispatch, never by this file.
    """

    _require_provisioned(inputs)
    channels = tuple(
        dict.fromkeys(
            item
            for item in (
                inputs.maintainer_channel_id,
                inputs.operator_channel_id,
                inputs.employee_channel_id,
                inputs.maintainer_route_channel_id,
            )
            if item
        )
    )
    return "\n".join(
        (
            "model:",
            f"  provider: {_yaml_string(inputs.model_provider)}",
            f"  default: {_yaml_string(inputs.model_name)}",
            "platform_toolsets:",
            "  discord: [scotty]",
            "tools:",
            "  tool_search:",
            "    enabled: off",
            "plugins:",
            "  enabled: [scotty-business]",
            "discord:",
            "  slash_commands: false",
            "  auto_thread: false",
            "  history_backfill: false",
            "  require_mention: false",
            "  group_sessions_per_user: true",
            f"  allowed_channels: {_yaml_list(channels)}",
            f"  free_response_channels: {_yaml_list(channels)}",
            "approvals:",
            "  mode: manual",
            "  cron_mode: deny",
            "security:",
            "  tirith_enabled: true",
            "gateway:",
            "  platforms:",
            "    discord:",
            "      enabled: true",
            "",
        )
    )


def _require_provisioned(inputs: SetupInputs) -> None:
    if not inputs.operator_channel_id or not inputs.employee_channel_id:
        raise SetupError("the operator and employee private channels are not provisioned yet")


def channel_plans(inputs: SetupInputs) -> tuple[ChannelPlan, ...]:
    """Plans for the private channels this run must create or reuse."""

    names = inputs.provision_channel_names
    if not names:
        return ()
    if set(names) != {"main_operator", "employee"}:
        raise SetupError("provisioning covers exactly the main-operator and employee channels")
    users = {"main_operator": inputs.operator_user_id, "employee": inputs.employee_user_id}
    return tuple(
        ChannelPlan(
            key=key,
            name=names[key],
            guild_id=inputs.guild_id,
            user_id=users[key],
        )
        for key in ("main_operator", "employee")
    )


def resolve_provisioned_channels(
    inputs: SetupInputs, channel_ids: Mapping[str, str]
) -> SetupInputs:
    """Bind provisioned channel IDs into the inputs, or fail closed."""

    operator = channel_ids.get("main_operator")
    employee = channel_ids.get("employee")
    if not operator or not employee:
        raise SetupError("private channel provisioning did not complete")
    return replace(inputs, operator_channel_id=operator, employee_channel_id=employee)


def private_mapping(inputs: SetupInputs) -> dict[str, object]:
    _require_provisioned(inputs)
    route = _route_mapping(inputs)
    mapping: dict[str, object] = {
        "version": 1,
        "addons": ["discord", "trello", "ghl", "rentcast"],
        "principals": {
            "maintainer": {
                "guild_id": inputs.guild_id,
                "channel_id": inputs.maintainer_channel_id,
                "user_id": inputs.maintainer_user_id,
            },
            "main_operator": {
                "guild_id": inputs.guild_id,
                "channel_id": inputs.operator_channel_id,
                "user_id": inputs.operator_user_id,
            },
            "employee": {
                "guild_id": inputs.guild_id,
                "channel_id": inputs.employee_channel_id,
                "user_id": inputs.employee_user_id,
            },
        },
        "discord": {"announcement_channel_ids": list(inputs.announcement_channel_ids)},
        "trello": {
            "board_id": inputs.trello_board_id,
            "list_ids": list(inputs.trello_list_ids),
            "label_ids": list(inputs.trello_label_ids),
            "custom_field_ids": list(inputs.trello_custom_field_ids),
        },
        "ghl": {"location_id": inputs.ghl_location_id},
        "rentcast": {
            "endpoints": [
                "/v1/properties",
                "/v1/avm/value",
                "/v1/avm/rent/long-term",
            ]
        },
    }
    if route is not None:
        mapping["maintainer_route"] = route
    return mapping


def _route_mapping(inputs: SetupInputs) -> dict[str, str] | None:
    fields = inputs.route_fields()
    if not any(fields):
        return None
    if not all(fields):
        raise SetupError("the private route needs a guild, channel, user, and profile")
    guild_id, channel_id, user_id, profile = fields
    return {
        "guild_id": guild_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "profile": profile,
    }


_OVERLAY_HEADER = (
    "# Native multiplexed profile routing overlay.\n"
    "# This file is NOT merged automatically. Verify these keys against the pinned\n"
    "# Hermes 0.20.6 gateway profile-routing contract before merging any of it into\n"
    "# config.yaml. Until then the plugin decides every profile and toolset before\n"
    "# dispatch and fails closed. Owner-only: never copy this file into a client\n"
    "# channel, a public repository, or any client-facing text.\n"
)


def render_profile_routing_overlay(inputs: SetupInputs) -> str:
    """Render the reviewed-but-unmerged native profile-routing overlay."""

    route = _route_mapping(inputs)
    if route is None:
        return ""
    client_channels = (
        (client_profile(Role.MAINTAINER), inputs.maintainer_channel_id),
        (client_profile(Role.MAIN_OPERATOR), inputs.operator_channel_id),
        (client_profile(Role.EMPLOYEE), inputs.employee_channel_id),
    )
    lines = [_OVERLAY_HEADER, "profiles:"]
    for name, _ in client_channels:
        lines.append(f"  {name}:")
        lines.append("    toolsets: [scotty]")
    lines.append(f"  {route['profile']}:")
    lines.append("    toolsets: [__all__]")
    lines.append("routing:")
    lines.append("  discord:")
    for name, channel_id in client_channels:
        lines.append(f"    - guild_id: {_yaml_string(inputs.guild_id)}")
        lines.append(f"      channel_id: {_yaml_string(channel_id)}")
        lines.append(f"      profile: {_yaml_string(name)}")
    lines.append(f"    - guild_id: {_yaml_string(route['guild_id'])}")
    lines.append(f"      channel_id: {_yaml_string(route['channel_id'])}")
    lines.append(f"      profile: {_yaml_string(route['profile'])}")
    return "\n".join(lines) + "\n"


def _ensure_directory(path: Path, uid: int, gid: int) -> None:
    if path.is_symlink():
        raise SetupError("private state directory cannot be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise SetupError("private state directory is unsafe")
    os.chmod(path, 0o700)
    os.chown(path, uid, gid)


def _atomic_private_write(path: Path, content: bytes, uid: int, gid: int) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SetupError("private state target is unsafe")
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = None
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temp, uid, gid)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise SetupError("private state publication failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(OSError):
            temp.unlink(missing_ok=True)


def write_private_state(
    inputs: SetupInputs,
    root: Path = _DATA_DIR,
    *,
    owner_uid: int = 10000,
    owner_gid: int = 10000,
) -> None:
    if root.is_symlink() or not root.is_dir():
        raise SetupError("Scotty data directory is absent or unsafe")
    scotty_dir = root / "scotty"
    _ensure_directory(scotty_dir, owner_uid, owner_gid)
    private_json = (
        json.dumps(private_mapping(inputs), sort_keys=True, indent=2, ensure_ascii=False).encode(
            "utf-8"
        )
        + b"\n"
    )
    env_lines = []
    for name in sorted(inputs.secrets):
        value = inputs.secrets[name]
        if not _SAFE_SECRET.fullmatch(value):
            raise SetupError("credential contains unsupported characters")
        env_lines.append(f"{name}={value}")
    _atomic_private_write(scotty_dir / "private.json", private_json, owner_uid, owner_gid)
    overlay = render_profile_routing_overlay(inputs)
    if overlay:
        _atomic_private_write(
            scotty_dir / "profile-routing.overlay.yaml",
            overlay.encode("utf-8"),
            owner_uid,
            owner_gid,
        )
    _atomic_private_write(
        root / "config.yaml",
        render_hermes_config(inputs).encode("utf-8"),
        owner_uid,
        owner_gid,
    )
    _atomic_private_write(
        root / ".env", ("\n".join(env_lines) + "\n").encode("utf-8"), owner_uid, owner_gid
    )


def _require_stopped_container() -> None:
    docker = shutil.which("docker")
    if not docker:
        raise SetupError("Docker is unavailable")
    try:
        result = subprocess.run(  # noqa: S603 - executable resolved by shutil.which
            [docker, "inspect", "--format", "{{.State.Running}}", "scotty"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError("unable to verify the stopped Scotty container") from exc
    if result.returncode != 0 or result.stdout.strip() != "false":
        raise SetupError("Scotty container must exist and remain stopped during setup")


def _console_confirm(preview: str) -> bool:
    print(preview)
    return input("Create this private channel now? (yes/no): ").strip().lower() == "yes"


def provision_private_channels(
    inputs: SetupInputs,
    *,
    token: str,
    confirm: Callable[[str], bool] = _console_confirm,
    recorded: Mapping[str, str] | None = None,
    client: DiscordProvisioningClient | None = None,
) -> SetupInputs:
    """Create or reuse the two private client channels, then bind their IDs."""

    plans = channel_plans(inputs)
    if not plans:
        return inputs
    outcome = ensure_private_channels(
        plans,
        client if client is not None else DiscordProvisioningApi(token),
        confirm=confirm,
        recorded=recorded,
    )
    if not outcome.is_complete(plans):
        unknown = sorted(
            key
            for key, channel in outcome.channels.items()
            if channel.status is ProvisionStatus.UNKNOWN
        )
        detail = outcome.error or "private channel provisioning did not complete"
        if unknown:
            detail = f"{detail} (unknown: {', '.join(unknown)})"
        raise SetupError(detail)
    return resolve_provisioned_channels(
        inputs,
        {key: channel.channel_id or "" for key, channel in outcome.channels.items()},
    )


def main() -> int:
    if os.geteuid() != 0:
        raise SetupError("run the local setup command as root")
    _require_stopped_container()
    inputs = collect_inputs()
    token = inputs.secrets["DISCORD_BOT_TOKEN"]
    inputs = provision_private_channels(inputs, token=token)
    validate_discord_scope(inputs, DiscordSetupClient(token))
    write_private_state(inputs)
    print("Scotty private setup completed. The container remains stopped.")
    return 0
