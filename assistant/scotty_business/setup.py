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
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .adapters.http import HttpTransport, ProviderError, RedactedMapping, require_success

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
) -> SetupInputs:
    provider = _visible(input_fn, "Model provider (openrouter, openai, anthropic): ").lower()
    if provider not in _MODEL_SECRET_ENV:
        raise SetupError("model provider is not supported by bounded setup")
    model_name = _visible(input_fn, "Model name: ")
    guild_id = _visible(input_fn, "Discord guild ID: ")
    maintainer_channel = _visible(input_fn, "Maintainer private channel ID: ")
    maintainer_user = _visible(input_fn, "Maintainer Discord user ID: ")
    operator_channel = _visible(input_fn, "Main-operator private channel ID: ")
    operator_user = _visible(input_fn, "Main-operator Discord user ID: ")
    employee_channel = _visible(input_fn, "Employee private channel ID: ")
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
    secrets: dict[str, str] = {}
    secrets[_MODEL_SECRET_ENV[provider]] = _hidden(hidden_fn, "Model API credential (hidden): ")
    for name, prompt in (
        ("DISCORD_BOT_TOKEN", "Discord bot token (hidden): "),
        ("SCOTTY_TRELLO_API_KEY", "Trello API key (hidden): "),
        ("SCOTTY_TRELLO_TOKEN", "Trello token (hidden): "),
        ("SCOTTY_GHL_PRIVATE_TOKEN", "GoHighLevel Private Integration Token (hidden): "),
        ("SCOTTY_RENTCAST_API_KEY", "RentCast API key (hidden): "),
    ):
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
    channels = (
        inputs.maintainer_channel_id,
        inputs.operator_channel_id,
        inputs.employee_channel_id,
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


def _private_mapping(inputs: SetupInputs) -> dict[str, object]:
    return {
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
        json.dumps(_private_mapping(inputs), sort_keys=True, indent=2, ensure_ascii=False).encode(
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


def main() -> int:
    if os.geteuid() != 0:
        raise SetupError("run the local setup command as root")
    _require_stopped_container()
    inputs = collect_inputs()
    validate_discord_scope(inputs, DiscordSetupClient(inputs.secrets["DISCORD_BOT_TOKEN"]))
    write_private_state(inputs)
    print("Scotty private setup completed. The container remains stopped.")
    return 0
