from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast

from .adapters.http import HttpTransport, ProviderError, RedactedMapping
from .config import GOOGLE_OAUTH_SCOPES
from .google_oauth import (
    GoogleOAuthError,
    GoogleTokenStore,
    authorize_installed_app,
)
from .policy import Role
from .provisioning import (
    BOT_ALLOW,
    MEMBER_ALLOW,
    ChannelPlan,
    DiscordProvisioningApi,
    DiscordProvisioningClient,
    ProvisionStatus,
    ensure_private_channels,
)
from .routing import (
    CLIENT_PROFILES,
    MAINTAINER_PROFILE,
    SERVED_PROFILES,
    ProfileRouteError,
    parse_profile_routes,
)
from .setup_flow import SetupStagingStore

_DATA_DIR = Path("/srv/Scotty/data")
#: The unprivileged account the container runtime, and its state, belong to.
_RUNTIME_UID = 10000
_PROFILES_DIRNAME = "profiles"
#: Registers only a pre-dispatch authorization hook. No tools, no prompt.
GUARD_PLUGIN = "scotty-guard"
_VIEW_CHANNEL = 1 << 10
_SEND_MESSAGES = 1 << 11
_READ_HISTORY = 1 << 16

#: Codex authenticates natively through the pinned runtime's own OAuth flow.
CODEX_PROVIDER = "openai-codex"
CODEX_AUTH_COMMAND = "hermes auth add openai-codex"
_MODEL_SECRET_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
_MODEL_PROVIDERS = (CODEX_PROVIDER, *sorted(_MODEL_SECRET_ENV))

#: The pinned gateway admits a Discord sender only when their ID appears here.
#: It is generated deterministically from the three configured principals; it is
#: never a wildcard, a role, an open policy, or a manual post-install pairing.
DISCORD_ALLOWED_USERS_ENV = "DISCORD_ALLOWED_USERS"

#: Only Discord is required on day one. Every other provider connects later.
REQUIRED_SECRETS = ("DISCORD_BOT_TOKEN",)
OPTIONAL_SECRETS = (
    "SCOTTY_TRELLO_API_KEY",
    "SCOTTY_TRELLO_TOKEN",
    "SCOTTY_GHL_PRIVATE_TOKEN",
    "SCOTTY_RENTCAST_API_KEY",
)
_SAFE_SECRET = re.compile(r"[A-Za-z0-9._:/+\-=]+")
_SAFE_VALUE = re.compile(r"[A-Za-z0-9._:/+\-]+")
_ACCOUNT_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_SAFE_ENV_VALUE = re.compile(r"[A-Za-z0-9._:/+,\-=]+")
_PREFILL_FIELDS = frozenset(
    {
        "model_provider",
        "model_name",
        "guild_id",
        "operator_channel_id",
        "operator_user_id",
        "employee_channel_id",
        "employee_user_id",
        "announcement_channel_ids",
        "route_guild_id",
        "route_channel_id",
        "route_user_id",
        "trello",
        "ghl_location_id",
        "rentcast_endpoints",
        "google_workspace",
    }
)
_SECRET_FIELD = re.compile(
    r"(?:secret|token|password|credential|api[_-]?key|authorization|code)", re.I
)


class SetupError(RuntimeError):
    pass


def load_prefill(path: Path, *, owner_uid: int = 0) -> Mapping[str, object]:
    """Load an owner-only, non-secret setup prefill document."""

    if path.is_symlink() or not path.is_file():
        raise SetupError("setup prefill is absent or unsafe")
    metadata = path.stat()
    if metadata.st_uid != owner_uid or metadata.st_mode & 0o077:
        raise SetupError("setup prefill must be owner-only")
    try:
        raw_bytes = path.read_bytes()
        if len(raw_bytes) > 65_536:
            raise ValueError
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SetupError("setup prefill is malformed") from exc
    if not isinstance(raw, dict) or set(raw) - _PREFILL_FIELDS:
        raise SetupError("setup prefill contains unsupported fields")

    def reject_secrets(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if type(key) is not str or _SECRET_FIELD.search(key):
                    raise SetupError("setup prefill must never contain credentials")
                reject_secrets(item)
        elif isinstance(value, list):
            for item in value:
                reject_secrets(item)
        elif type(value) not in {str, bool, int} and value is not None:
            raise SetupError("setup prefill contains an unsupported value")

    reject_secrets(raw)
    return raw


def _prefill_text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > 256:
        raise SetupError(f"{field} must be a bounded non-empty string")
    return value.strip()


def _prefill_texts(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SetupError(f"{field} must be a list")
    result = tuple(_prefill_text(item, f"{field}[]") for item in value)
    if (not result and not allow_empty) or len(result) != len(set(result)):
        raise SetupError(f"{field} is empty or contains duplicates")
    return result


def _prefill_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SetupError(f"{field} must be an object")
    return value


def apply_prefill(inputs: SetupInputs, prefill: Mapping[str, object]) -> SetupInputs:
    """Apply only validated non-secret setup values to collected answers."""

    scalar_fields = {
        "model_provider",
        "model_name",
        "guild_id",
        "operator_channel_id",
        "operator_user_id",
        "employee_channel_id",
        "employee_user_id",
        "route_guild_id",
        "route_channel_id",
        "route_user_id",
        "ghl_location_id",
    }
    changes: dict[str, object] = {}
    for field in scalar_fields:
        if field in prefill:
            changes[field] = _prefill_text(prefill[field], f"prefill.{field}")
    list_fields = {
        "announcement_channel_ids": "announcement_channel_ids",
        "rentcast_endpoints": "rentcast_endpoints",
    }
    for source, target in list_fields.items():
        if source in prefill:
            changes[target] = _prefill_texts(prefill[source], f"prefill.{source}", allow_empty=True)
    trello = prefill.get("trello")
    if trello is not None:
        raw = _prefill_mapping(trello, "prefill.trello")
        if set(raw) != {"board_id", "list_ids", "label_ids", "custom_field_ids"}:
            raise SetupError("setup prefill Trello scope is malformed")
        changes.update(
            trello_board_id=_prefill_text(raw["board_id"], "prefill.trello.board_id"),
            trello_list_ids=_prefill_texts(raw["list_ids"], "prefill.trello.list_ids"),
            trello_label_ids=_prefill_texts(
                raw["label_ids"], "prefill.trello.label_ids", allow_empty=True
            ),
            trello_custom_field_ids=_prefill_texts(
                raw["custom_field_ids"], "prefill.trello.custom_field_ids", allow_empty=True
            ),
        )
    google = prefill.get("google_workspace")
    if google is not None:
        raw = _prefill_mapping(google, "prefill.google_workspace")
        if set(raw) != {"account_email"}:
            raise SetupError("setup prefill Google Workspace account is malformed")
        changes.update(
            google_account_email=_google_account_email(
                _prefill_text(raw["account_email"], "prefill.google.account_email")
            ),
        )
    return replace(inputs, **cast(Any, changes))


@dataclass(frozen=True, slots=True)
class SetupInputs:
    model_provider: str
    model_name: str
    guild_id: str
    operator_channel_id: str
    operator_user_id: str
    employee_channel_id: str
    employee_user_id: str
    route_guild_id: str
    route_channel_id: str
    route_user_id: str
    secrets: Mapping[str, str]
    announcement_channel_ids: tuple[str, ...] = ()
    trello_board_id: str = ""
    trello_list_ids: tuple[str, ...] = ()
    trello_label_ids: tuple[str, ...] = ()
    trello_custom_field_ids: tuple[str, ...] = ()
    ghl_location_id: str = ""
    google_account_email: str = ""
    provision_channel_names: Mapping[str, str] | None = None

    @property
    def client_user_ids(self) -> tuple[str, ...]:
        return (self.operator_user_id, self.employee_user_id)


class DiscordSetupReader(Protocol):
    def get(self, path: str) -> object: ...

    def status_get(self, path: str) -> tuple[int, object]: ...


def _visible(input_fn: Callable[[str], str], prompt: str) -> str:
    value = input_fn(prompt).strip()
    if not value or len(value) > 256 or any(ord(char) < 32 for char in value):
        raise SetupError("setup value is missing or malformed")
    return value


def _optional(input_fn: Callable[[str], str], prompt: str) -> str:
    value = input_fn(prompt).strip()
    if not value:
        return ""
    if len(value) > 256 or any(ord(char) < 32 for char in value):
        raise SetupError("setup value is malformed")
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


def _google_account_email(value: str) -> str:
    """A Workspace account is a bounded email address or an explicit blank."""

    if not value:
        return ""
    if len(value) > 254 or not _ACCOUNT_EMAIL.fullmatch(value):
        raise SetupError("Google Workspace account must be an email address")
    return value


def _hidden(hidden_fn: Callable[[str], str], prompt: str) -> str:
    value = hidden_fn(prompt)
    if not value or len(value) > 4096 or not _SAFE_SECRET.fullmatch(value):
        raise SetupError("hidden credential is missing or contains unsupported characters")
    return value


def _hidden_optional(hidden_fn: Callable[[str], str], prompt: str) -> str:
    value = hidden_fn(prompt)
    if not value:
        return ""
    if len(value) > 4096 or not _SAFE_SECRET.fullmatch(value):
        raise SetupError("hidden credential contains unsupported characters")
    return value


def _environment_secret(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if value and not _SAFE_SECRET.fullmatch(value):
        raise SetupError("an environment credential contains unsupported characters")
    return value


def apply_staged_identifiers(
    inputs: SetupInputs, staged: Mapping[str, Mapping[str, str]]
) -> SetupInputs:
    """Apply only the non-secret identifiers Scotty collected conversationally.

    A staged value is used only where the interactive answer was left blank, so
    a value the operator typed at the terminal is never overwritten.
    """

    changes: dict[str, object] = {}
    google = staged.get("google_workspace", {}).get("account_email")
    if google and not inputs.google_account_email:
        changes["google_account_email"] = _google_account_email(google)
    location = staged.get("ghl", {}).get("location_id")
    if location and not inputs.ghl_location_id:
        changes["ghl_location_id"] = location
    board = staged.get("trello", {}).get("board_id")
    if board and not inputs.trello_board_id:
        changes["trello_board_id"] = board
    if not changes:
        return inputs
    return replace(inputs, **cast(Any, changes))


def collect_inputs(
    *,
    input_fn: Callable[[str], str] = input,
    hidden_fn: Callable[[str], str] = getpass.getpass,
    environ: Mapping[str, str] | None = None,
) -> SetupInputs:
    """Collect setup answers. Credentials never come from argv."""

    environ = os.environ if environ is None else environ
    provider = _visible(input_fn, f"Model provider ({', '.join(_MODEL_PROVIDERS)}): ").lower()
    if provider not in _MODEL_PROVIDERS:
        raise SetupError("model provider is not supported by bounded setup")
    model_name = _visible(input_fn, "Model name: ")

    guild_id = _visible(input_fn, "Discord guild ID: ")
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
        input_fn,
        "Configured Discord announcement channel IDs (comma-separated, blank for none): ",
        allow_empty=True,
    )

    route_guild = _visible(input_fn, "Private route guild ID: ")
    route_channel = _visible(input_fn, "Private route channel ID: ")
    route_user = _visible(input_fn, "Private route Discord user ID: ")

    board_id = _optional(input_fn, "Trello board ID (blank to connect later): ")
    list_ids = _csv(input_fn, "Trello list IDs (comma-separated): ") if board_id else ()
    label_ids = (
        _csv(input_fn, "Trello label IDs (comma-separated, blank for none): ", allow_empty=True)
        if board_id
        else ()
    )
    custom_fields = (
        _csv(
            input_fn,
            "Trello custom-field IDs (comma-separated, blank for none): ",
            allow_empty=True,
        )
        if board_id
        else ()
    )
    location_id = _optional(input_fn, "GoHighLevel location ID (blank to connect later): ")
    google_account = _google_account_email(
        _optional(input_fn, "Google Workspace account email (blank to connect later): ")
    )

    secrets: dict[str, str] = {}
    if provider != CODEX_PROVIDER:
        name = _MODEL_SECRET_ENV[provider]
        secrets[name] = _environment_secret(environ, name) or _hidden(
            hidden_fn, "Model API credential (hidden): "
        )
    for name, prompt in (
        ("DISCORD_BOT_TOKEN", "Discord bot token (hidden): "),
        ("SCOTTY_TRELLO_API_KEY", "Trello API key (hidden, blank to connect later): "),
        ("SCOTTY_TRELLO_TOKEN", "Trello token (hidden, blank to connect later): "),
        (
            "SCOTTY_GHL_PRIVATE_TOKEN",
            "GoHighLevel Private Integration Token (hidden, blank to connect later): ",
        ),
        ("SCOTTY_RENTCAST_API_KEY", "RentCast API key (hidden, blank to connect later): "),
    ):
        # A credential is read from hidden terminal input, or from the process
        # environment when the operator exported it. It is never read from argv.
        value = _environment_secret(environ, name)
        if not value:
            value = (
                _hidden(hidden_fn, prompt)
                if name in REQUIRED_SECRETS
                else _hidden_optional(hidden_fn, prompt)
            )
        if value:
            secrets[name] = value
    for name in REQUIRED_SECRETS:
        if not secrets.get(name):
            raise SetupError("the Discord bot token is required for initial setup")

    return SetupInputs(
        model_provider=provider,
        model_name=model_name,
        guild_id=guild_id,
        operator_channel_id=operator_channel,
        operator_user_id=operator_user,
        employee_channel_id=employee_channel,
        employee_user_id=employee_user,
        announcement_channel_ids=announcements,
        route_guild_id=route_guild,
        route_channel_id=route_channel,
        route_user_id=route_user,
        trello_board_id=board_id,
        trello_list_ids=list_ids,
        trello_label_ids=label_ids,
        trello_custom_field_ids=custom_fields,
        ghl_location_id=location_id,
        google_account_email=google_account,
        secrets=secrets,
        provision_channel_names=provision_names,
    )


def collect_inputs_from_prefill(
    prefill: Mapping[str, object],
    *,
    hidden_fn: Callable[[str], str] = getpass.getpass,
    environ: Mapping[str, str] | None = None,
) -> SetupInputs:
    """Collect only hidden credentials when a complete owner-only prefill exists."""

    environ = os.environ if environ is None else environ
    required = {
        "model_provider",
        "model_name",
        "guild_id",
        "operator_channel_id",
        "operator_user_id",
        "employee_channel_id",
        "employee_user_id",
        "announcement_channel_ids",
        "route_guild_id",
        "route_channel_id",
        "route_user_id",
    }
    if not required.issubset(prefill):
        raise SetupError("setup prefill is incomplete; add the missing non-secret fields")
    provider = _prefill_text(prefill["model_provider"], "prefill.model_provider").lower()
    if provider not in _MODEL_PROVIDERS:
        raise SetupError("prefilled model provider is unsupported")
    base = SetupInputs(
        model_provider=provider,
        model_name=_prefill_text(prefill["model_name"], "prefill.model_name"),
        guild_id=_prefill_text(prefill["guild_id"], "prefill.guild_id"),
        operator_channel_id=_prefill_text(
            prefill["operator_channel_id"], "prefill.operator_channel_id"
        ),
        operator_user_id=_prefill_text(prefill["operator_user_id"], "prefill.operator_user_id"),
        employee_channel_id=_prefill_text(
            prefill["employee_channel_id"], "prefill.employee_channel_id"
        ),
        employee_user_id=_prefill_text(prefill["employee_user_id"], "prefill.employee_user_id"),
        route_guild_id=_prefill_text(prefill["route_guild_id"], "prefill.route_guild_id"),
        route_channel_id=_prefill_text(prefill["route_channel_id"], "prefill.route_channel_id"),
        route_user_id=_prefill_text(prefill["route_user_id"], "prefill.route_user_id"),
        announcement_channel_ids=_prefill_texts(
            prefill["announcement_channel_ids"],
            "prefill.announcement_channel_ids",
            allow_empty=True,
        ),
        secrets={},
    )
    configured = apply_prefill(base, prefill)
    secrets_map: dict[str, str] = {}
    if provider != CODEX_PROVIDER:
        name = _MODEL_SECRET_ENV[provider]
        secrets_map[name] = _environment_secret(environ, name) or _hidden(
            hidden_fn, "Model API credential (hidden): "
        )
    prompts = (
        ("DISCORD_BOT_TOKEN", "Discord bot token (hidden): "),
        ("SCOTTY_TRELLO_API_KEY", "Trello API key (hidden, blank to connect later): "),
        ("SCOTTY_TRELLO_TOKEN", "Trello token (hidden, blank to connect later): "),
        ("SCOTTY_GHL_PRIVATE_TOKEN", "GoHighLevel token (hidden, blank to connect later): "),
        ("SCOTTY_RENTCAST_API_KEY", "RentCast API key (hidden, blank to connect later): "),
    )
    for name, prompt in prompts:
        value = _environment_secret(environ, name)
        if not value:
            value = (
                _hidden(hidden_fn, prompt)
                if name in REQUIRED_SECRETS
                else _hidden_optional(hidden_fn, prompt)
            )
        if value:
            secrets_map[name] = value
    if not secrets_map.get("DISCORD_BOT_TOKEN"):
        raise SetupError("the Discord bot token is required for initial setup")
    return replace(configured, secrets=secrets_map)


class DiscordSetupClient:
    def __init__(self, token: str):
        self.transport = HttpTransport(timeout=20.0, max_response_bytes=262_144)
        self.headers = RedactedMapping(Authorization=f"Bot {token}")

    def _url(self, path: str) -> str:
        if not path.startswith("/") or ".." in path or "?" in path or "#" in path:
            raise SetupError("Discord setup path is invalid")
        return f"https://discord.com/api/v10{path}"

    def status_get(self, path: str) -> tuple[int, object]:
        try:
            response = self.transport.request("GET", self._url(path), headers=self.headers)
        except ProviderError as exc:
            raise SetupError("Discord setup validation failed") from exc
        return response.status, response.body

    def get(self, path: str) -> object:
        status, body = self.status_get(path)
        if status != 200:
            raise SetupError("Discord setup validation failed")
        return body


def _snowflake(value: object, field: str) -> str:
    if type(value) is not str or not value.isdigit() or not 1 <= len(value) <= 20:
        raise SetupError(f"{field} must be a Discord numeric ID")
    return value


def _overwrite(channel: Mapping[str, object], identifier: str) -> Mapping[str, object] | None:
    overwrites = channel.get("permission_overwrites")
    if not isinstance(overwrites, list):
        return None
    for overwrite in overwrites:
        if isinstance(overwrite, Mapping) and overwrite.get("id") == identifier:
            return overwrite
    return None


def _bits(overwrite: Mapping[str, object] | None, key: str) -> int:
    if overwrite is None:
        return 0
    value = overwrite.get(key)
    if type(value) is not str or not value.isdigit():
        raise SetupError("Discord permission overwrite is malformed")
    return int(value)


def _private_channel(channel: Mapping[str, object], guild_id: str) -> bool:
    everyone = _overwrite(channel, guild_id)
    if everyone is None or everyone.get("type") != 0:
        return False
    return bool(_bits(everyone, "deny") & _VIEW_CHANNEL)


def validate_discord_scope(inputs: SetupInputs, client: DiscordSetupReader) -> None:
    """Verify the client guild, the bot's membership, and channel privacy."""

    guild = _snowflake(inputs.guild_id, "guild_id")
    bot = client.get("/users/@me")
    if not isinstance(bot, Mapping) or type(bot.get("id")) is not str:
        raise SetupError("Discord bot identity is malformed")
    member = client.get(f"/guilds/{guild}/members/@me")
    if (
        not isinstance(member, Mapping)
        or not isinstance(member.get("user"), Mapping)
        or member["user"].get("id") != bot["id"]
    ):
        raise SetupError("Discord bot is not a verified member of the configured guild")
    channel_ids = tuple(
        dict.fromkeys(
            (
                inputs.operator_channel_id,
                inputs.employee_channel_id,
                *inputs.announcement_channel_ids,
            )
        )
    )
    if len(channel_ids) != 2 + len(inputs.announcement_channel_ids):
        raise SetupError("Discord principal and announcement channels must be distinct")
    for raw_channel_id in channel_ids:
        channel_id = _snowflake(raw_channel_id, "channel_id")
        channel = client.get(f"/channels/{channel_id}")
        if (
            not isinstance(channel, Mapping)
            or channel.get("id") != channel_id
            or channel.get("guild_id") != guild
        ):
            raise SetupError("Discord channel identity or guild mismatch")
        private = _private_channel(channel, guild)
        parent_id = channel.get("parent_id")
        if not private and type(parent_id) is str and parent_id:
            parent = client.get(f"/channels/{_snowflake(parent_id, 'parent_id')}")
            private = (
                isinstance(parent, Mapping)
                and parent.get("guild_id") == guild
                and _private_channel(parent, guild)
            )
        if not private:
            raise SetupError("every configured Discord channel must deny View Channel to @everyone")


def validate_maintainer_route(inputs: SetupInputs, client: DiscordSetupReader) -> None:
    """Read the private route back from Discord before it is ever configured.

    Every failure message here stays generic. Nothing that reaches a client
    surface names the guild, the channel, the user, or the profile.
    """

    guild = _snowflake(inputs.route_guild_id, "route guild_id")
    channel_id = _snowflake(inputs.route_channel_id, "route channel_id")
    user_id = _snowflake(inputs.route_user_id, "route user_id")
    if guild == inputs.guild_id:
        raise SetupError("the private route must not share the client guild")

    identity = client.get("/users/@me")
    if not isinstance(identity, Mapping) or type(identity.get("id")) is not str:
        raise SetupError("Discord bot identity is malformed")
    bot_id = _snowflake(identity["id"], "bot id")
    member = client.get(f"/guilds/{guild}/members/@me")
    if (
        not isinstance(member, Mapping)
        or not isinstance(member.get("user"), Mapping)
        or member["user"].get("id") != bot_id
    ):
        raise SetupError("the assistant is not a verified member of the private route guild")

    channel = client.get(f"/channels/{channel_id}")
    if not isinstance(channel, Mapping) or channel.get("id") != channel_id:
        raise SetupError("the private route channel does not exist")
    if channel.get("guild_id") != guild:
        raise SetupError("the private route channel is not in its configured guild")
    if channel.get("type") != 0:
        raise SetupError("the private route channel must be a text channel")
    if not _private_channel(channel, guild):
        raise SetupError("the private route channel must deny View Channel to @everyone")

    if not _bits(_overwrite(channel, user_id), "allow") & _VIEW_CHANNEL:
        raise SetupError("the configured private route user cannot view the route channel")
    if _bits(_overwrite(channel, user_id), "deny") & _VIEW_CHANNEL:
        raise SetupError("the configured private route user is denied the route channel")

    required = _VIEW_CHANNEL | _SEND_MESSAGES | _READ_HISTORY
    bot_allow = _bits(_overwrite(channel, bot_id), "allow")
    if bot_allow & required != required:
        raise SetupError("the assistant cannot view, send, and read history in the route channel")
    if _bits(_overwrite(channel, bot_id), "deny") & required:
        raise SetupError("the assistant is denied access in the route channel")

    for client_user in inputs.client_user_ids:
        identifier = _snowflake(client_user, "client user_id")
        if _bits(_overwrite(channel, identifier), "allow") & _VIEW_CHANNEL:
            raise SetupError("a client principal can view the private route channel")
        status, _ = client.status_get(f"/guilds/{guild}/members/{identifier}")
        if status == 200:
            raise SetupError("a client principal belongs to the private route guild")
        if status != 404:
            raise SetupError("private route membership could not be verified")


def _yaml_scalar(value: object) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) is int:
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _emit(value: object, indent: int) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, Mapping):
                lines.append(f"{pad}{key}:")
                lines.extend(_emit(item, indent + 1))
            elif isinstance(item, list):
                if not item:
                    lines.append(f"{pad}{key}: []")
                elif all(isinstance(entry, Mapping) for entry in item):
                    lines.append(f"{pad}{key}:")
                    for entry in item:
                        rendered = _emit(entry, indent + 2)
                        lines.append(f"{'  ' * (indent + 1)}- {rendered[0].strip()}")
                        lines.extend(rendered[1:])
                else:
                    inline = ", ".join(_yaml_scalar(entry) for entry in item)
                    lines.append(f"{pad}{key}: [{inline}]")
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(item)}")
    return lines


def render_mapping(mapping: Mapping[str, object]) -> str:
    return "\n".join(_emit(mapping, 0)) + "\n"


def _require_provisioned(inputs: SetupInputs) -> None:
    if not inputs.operator_channel_id or not inputs.employee_channel_id:
        raise SetupError("the operator and employee private channels are not provisioned yet")


def discord_allowed_users(inputs: SetupInputs) -> tuple[str, ...]:
    """The exact senders the gateway may admit, in a deterministic order."""

    users = (inputs.route_user_id, inputs.operator_user_id, inputs.employee_user_id)
    if not all(users):
        raise SetupError("every configured principal needs a Discord user ID")
    if len(set(users)) != len(users):
        raise SetupError("the configured principals must be distinct Discord users")
    for user in users:
        if not user.isdigit() or not 1 <= len(user) <= 20:
            raise SetupError("a configured principal is not a Discord numeric ID")
    return users


def runtime_environment(inputs: SetupInputs) -> dict[str, str]:
    """Every value written to the owner-only runtime environment file."""

    environment = dict(inputs.secrets)
    environment[DISCORD_ALLOWED_USERS_ENV] = ",".join(discord_allowed_users(inputs))
    environment["SCOTTY_PRIVATE_CONFIG"] = "/opt/data/scotty/private.json"
    return environment


def profile_routes(inputs: SetupInputs) -> list[dict[str, str]]:
    """The three native `gateway.profile_routes` entries this deployment serves."""

    _require_provisioned(inputs)
    return [
        {
            "name": "maintainer-private-channel",
            "platform": "discord",
            "guild_id": inputs.route_guild_id,
            "chat_id": inputs.route_channel_id,
            "profile": MAINTAINER_PROFILE,
        },
        {
            "name": "main-operator-private-channel",
            "platform": "discord",
            "guild_id": inputs.guild_id,
            "chat_id": inputs.operator_channel_id,
            "profile": CLIENT_PROFILES[Role.MAIN_OPERATOR],
        },
        {
            "name": "employee-private-channel",
            "platform": "discord",
            "guild_id": inputs.guild_id,
            "chat_id": inputs.employee_channel_id,
            "profile": CLIENT_PROFILES[Role.EMPLOYEE],
        },
    ]


def hermes_config_mapping(inputs: SetupInputs) -> dict[str, object]:
    """Owner-only gateway configuration, including native profile routing.

    The base configuration is bounded. A profile widens its own surface only by
    overriding these values, so a profile whose configuration fails to apply is
    bounded rather than unbounded.
    """

    _require_provisioned(inputs)
    routes = profile_routes(inputs)
    channels = [str(route["chat_id"]) for route in routes]
    model: dict[str, object] = {
        "provider": inputs.model_provider,
        "default": inputs.model_name,
    }
    return {
        "model": model,
        "platform_toolsets": {"discord": ["scotty"]},
        "tools": {"tool_search": {"enabled": False}},
        "plugins": {"enabled": ["scotty-business"]},
        "discord": {
            "slash_commands": False,
            "auto_thread": False,
            "history_backfill": False,
            "require_mention": False,
            "group_sessions_per_user": True,
            "allowed_channels": channels,
            "free_response_channels": channels,
        },
        "approvals": {"mode": "manual", "cron_mode": "deny"},
        "security": {"tirith_enabled": True},
        "gateway": {
            "multiplex_profiles": True,
            "multiplex_profile_allowlist": list(SERVED_PROFILES),
            "profile_routes": routes,
            "platforms": {"discord": {"enabled": True}},
        },
    }


def render_hermes_config(inputs: SetupInputs) -> str:
    mapping = hermes_config_mapping(inputs)
    parse_profile_routes(mapping)
    return render_mapping(mapping)


def profile_config_mapping(profile: str, inputs: SetupInputs) -> dict[str, object]:
    """Per-profile configuration for one served profile.

    The pinned runtime scopes configuration to each profile home, so the model
    the operator selected during setup is restated here. Nothing is hard-coded
    and no provider default is relied on.
    """

    if profile not in SERVED_PROFILES:
        raise ProfileRouteError("profile is not served by this deployment")
    model = {"provider": inputs.model_provider, "default": inputs.model_name}
    if profile == MAINTAINER_PROFILE:
        # A normal full profile: the bounded business plugin is never staged or
        # enabled here, so it carries no bounded toolset and no client identity.
        # Only the profile-local authorization guard runs, and it registers no
        # model tools and no prompt section.
        return {
            "model": model,
            "plugins": {"enabled": [GUARD_PLUGIN]},
            "platform_toolsets": {"discord": ["*"]},
            "tools": {"tool_search": {"enabled": True}},
        }
    return {
        "model": model,
        "plugins": {"enabled": ["scotty-business"]},
        "platform_toolsets": {"discord": ["scotty"]},
        "tools": {"tool_search": {"enabled": False}},
    }


def render_profile_config(profile: str, inputs: SetupInputs) -> str:
    return render_mapping(profile_config_mapping(profile, inputs))


def channel_plans(inputs: SetupInputs) -> tuple[ChannelPlan, ...]:
    """Plans for the private channels this run must create or reuse."""

    names = inputs.provision_channel_names
    if not names:
        return ()
    if set(names) != {"main_operator", "employee"}:
        raise SetupError("provisioning covers exactly the main-operator and employee channels")
    users = {"main_operator": inputs.operator_user_id, "employee": inputs.employee_user_id}
    return tuple(
        ChannelPlan(key=key, name=names[key], guild_id=inputs.guild_id, user_id=users[key])
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
    mapping: dict[str, object] = {
        "version": 1,
        "addons": ["discord", "trello", "ghl", "rentcast", "google_workspace"],
        "principals": {
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
        "maintainer_route": {
            "guild_id": inputs.route_guild_id,
            "channel_id": inputs.route_channel_id,
            "user_id": inputs.route_user_id,
            "profile": MAINTAINER_PROFILE,
        },
    }
    # An unconfigured provider is absent, never a placeholder that looks connected.
    if inputs.trello_board_id:
        mapping["trello"] = {
            "board_id": inputs.trello_board_id,
            "list_ids": list(inputs.trello_list_ids),
            "label_ids": list(inputs.trello_label_ids),
            "custom_field_ids": list(inputs.trello_custom_field_ids),
        }
    if inputs.ghl_location_id:
        mapping["ghl"] = {"location_id": inputs.ghl_location_id}
    if inputs.secrets.get("SCOTTY_RENTCAST_API_KEY"):
        mapping["rentcast"] = {
            "endpoints": ["/v1/properties", "/v1/avm/value", "/v1/avm/rent/long-term"]
        }
    if inputs.google_account_email:
        mapping["google_workspace"] = {
            "account_email": inputs.google_account_email,
            "oauth_scopes": list(GOOGLE_OAUTH_SCOPES),
        }
    return mapping


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


def profile_home(root: Path, profile: str) -> Path:
    if profile not in SERVED_PROFILES:
        raise ProfileRouteError("profile is not served by this deployment")
    return root / _PROFILES_DIRNAME / profile


def ensure_profile_homes(
    root: Path,
    inputs: SetupInputs,
    profiles: Sequence[str] = SERVED_PROFILES,
    *,
    owner_uid: int = 10000,
    owner_gid: int = 10000,
) -> dict[str, Path]:
    """Create or idempotently verify one home per served profile.

    A routed profile without a home is a fail-closed setup error, never a silent
    fall back to the default profile. The bounded plugin must be staged in each
    client profile home and absent from the full profile home.
    """

    homes: dict[str, Path] = {}
    _ensure_directory(root / _PROFILES_DIRNAME, owner_uid, owner_gid)
    for profile in profiles:
        home = profile_home(root, profile)
        _ensure_directory(home, owner_uid, owner_gid)
        _atomic_private_write(
            home / "config.yaml",
            render_profile_config(profile, inputs).encode("utf-8"),
            owner_uid,
            owner_gid,
        )
        staged = home / "plugins" / "scotty_business" / "plugin.yaml"
        guard = home / "plugins" / "scotty_guard" / "plugin.yaml"
        if profile == MAINTAINER_PROFILE:
            if staged.exists():
                raise SetupError("the full profile must not carry the bounded Scotty plugin")
            if not guard.is_file() or guard.is_symlink():
                raise SetupError(
                    "the full profile is missing its staged authorization guard; "
                    "reinstall before setup"
                )
        else:
            if not staged.is_file() or staged.is_symlink():
                raise SetupError(
                    "a client profile is missing its staged Scotty plugin; reinstall before setup"
                )
            if guard.exists():
                raise SetupError("a client profile must not carry the maintainer guard")
        if not home.is_dir():
            raise SetupError("a served profile home is missing")
        homes[profile] = home
    return homes


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
    environment = runtime_environment(inputs)
    env_lines = []
    for name in sorted(environment):
        value = environment[name]
        if not _SAFE_SECRET.fullmatch(value) and not _SAFE_ENV_VALUE.fullmatch(value):
            raise SetupError("a runtime environment value contains unsupported characters")
        env_lines.append(f"{name}={value}")
    ensure_profile_homes(root, inputs, owner_uid=owner_uid, owner_gid=owner_gid)
    _atomic_private_write(scotty_dir / "private.json", private_json, owner_uid, owner_gid)
    _atomic_private_write(
        root / "config.yaml", render_hermes_config(inputs).encode("utf-8"), owner_uid, owner_gid
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
        inputs, {key: channel.channel_id or "" for key, channel in outcome.channels.items()}
    )


def next_steps(inputs: SetupInputs) -> tuple[str, ...]:
    """Fixed, non-secret instructions printed after a successful setup."""

    steps = ["Scotty private setup completed. The container remains stopped."]
    if inputs.model_provider == CODEX_PROVIDER:
        steps.append(
            "Authenticate the model locally with the runtime's own OAuth flow: "
            f"{CODEX_AUTH_COMMAND}. Complete the browser or device-code prompt as "
            "the maintainer. Scotty never handles, stores, or logs that token."
        )
    missing = [
        name
        for name, connected in (
            ("Trello", bool(inputs.trello_board_id)),
            ("GoHighLevel", bool(inputs.ghl_location_id)),
            ("RentCast", bool(inputs.secrets.get("SCOTTY_RENTCAST_API_KEY"))),
        )
        if not connected
    ]
    if missing:
        steps.append(
            f"{', '.join(missing)} stayed unconfigured. Scotty reports each as not "
            "connected and explains its setup steps. Rerun local setup to connect one."
        )
    return tuple(steps)


def main() -> int:
    if os.geteuid() != 0:
        raise SetupError("run the local setup command as root")
    _require_stopped_container()
    inputs = collect_inputs()
    # Identifiers Trent supplied conversationally are applied first, so the
    # operator's own prefill still wins on any field they both name.
    inputs = apply_staged_identifiers(
        inputs,
        SetupStagingStore(
            _DATA_DIR / "scotty" / "setup-staging.json", owner_uid=_RUNTIME_UID
        ).read(),
    )
    prefill_path = Path("/srv/Scotty/operator/setup-prefill.json")
    if prefill_path.exists():
        inputs = apply_prefill(inputs, load_prefill(prefill_path))
    token = inputs.secrets["DISCORD_BOT_TOKEN"]
    inputs = provision_private_channels(inputs, token=token)
    reader = DiscordSetupClient(token)
    validate_discord_scope(inputs, reader)
    validate_maintainer_route(inputs, reader)
    write_private_state(inputs)
    if inputs.google_account_email:
        store = GoogleTokenStore(_DATA_DIR / "scotty" / "google-oauth.json")
        if not store.ready(GOOGLE_OAUTH_SCOPES, inputs.google_account_email):
            try:
                authorize_installed_app(
                    Path("/srv/Scotty/operator/google-oauth-client.json"),
                    store,
                    GOOGLE_OAUTH_SCOPES,
                )
            except GoogleOAuthError as exc:
                raise SetupError(
                    "Google OAuth is incomplete; Scotty remains stopped until local browser consent succeeds"
                ) from exc
        if not store.ready(GOOGLE_OAUTH_SCOPES, inputs.google_account_email):
            raise SetupError("Google OAuth account does not match the configured Workspace account")
    for step in next_steps(inputs):
        print(step)
    return 0


__all__ = [
    "BOT_ALLOW",
    "CODEX_AUTH_COMMAND",
    "CODEX_PROVIDER",
    "MEMBER_ALLOW",
    "SetupError",
    "SetupInputs",
    "DISCORD_ALLOWED_USERS_ENV",
    "GUARD_PLUGIN",
    "channel_plans",
    "collect_inputs",
    "discord_allowed_users",
    "ensure_profile_homes",
    "hermes_config_mapping",
    "main",
    "next_steps",
    "private_mapping",
    "profile_config_mapping",
    "profile_home",
    "profile_routes",
    "provision_private_channels",
    "render_hermes_config",
    "render_mapping",
    "render_profile_config",
    "resolve_provisioned_channels",
    "runtime_environment",
    "validate_discord_scope",
    "validate_maintainer_route",
    "write_private_state",
]
