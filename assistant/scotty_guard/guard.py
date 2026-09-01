"""Profile-local pre-dispatch authorization for the full maintainer profile.

Native Hermes profile routing matches platform, guild, and channel. It does not
match the acting user. This guard supplies that missing check inside the full
profile itself, so exact-tuple enforcement does not depend on whether a hook
registered at the gateway root also runs for a multiplexed profile turn.

It is deliberately self-contained. The bounded Scotty package is never staged in
the full profile home, so this module duplicates the small amount of private
configuration reading it needs rather than importing it. It registers no model
tools, no system-prompt section, and no client identity.

Everything fails closed. If private configuration cannot be read, every message
in this profile is skipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SKIP_UNAUTHORIZED: Mapping[str, str] = {"action": "skip", "reason": "unauthorized"}
ALLOW: Mapping[str, str] = {"action": "allow"}
SKIP_WIZARD: Mapping[str, str] = {"action": "skip", "reason": "fixed-wizard"}

FIXED_WIZARD_COMMAND = "Scotty, send Trent the setup wizard."
#: The guard is standalone by design, so it carries its own copy of the fixed
#: wizard. Its only reader is the main operator, whose assistant is named in
#: configuration, so the name is a literal here and a test holds the two texts
#: identical.
ASSISTANT_NAME = "Scotty"
SETUP_WIZARD = (
    f"Welcome. This is {ASSISTANT_NAME}, your private assistant.\n"
    "1. Use this private channel for your own requests and non-secret preferences.\n"
    "2. Confirm which configured Discord channels, Trello board, Google Workspace "
    "resources, GoHighLevel location, and RentCast reads you expect to use.\n"
    "3. Google Workspace access uses provider-owned browser consent; credentials and "
    "OAuth codes are entered only through local setup, never here.\n"
    f"4. {ASSISTANT_NAME} shows external sends and writes for approval before execution.\n"
    f"5. Never paste credentials here. {ASSISTANT_NAME} cannot accept them in this chat. "
    "If one appears, rotate it and use local setup.\n"
    "6. Property and financial analysis is preliminary; verify it with the appropriate "
    "qualified professional."
)

_MAX_CONFIG_BYTES = 65_536
_MARKER_DIRNAME = "wizard"
_MAX_MARKERS = 512
_HEX = frozenset("0123456789abcdef")
_DISCORD_API = "https://discord.com/api/v10"


@dataclass(frozen=True, slots=True)
class GuardConfig:
    """The exact tuple this profile serves, plus the one fixed destination."""

    guild_id: str
    channel_id: str
    user_id: str
    operator_channel_id: str
    state_dir: Path


class GuardUnavailable(RuntimeError):
    """Private configuration is absent or unusable, so nothing is authorized."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise GuardUnavailable(f"{field} is missing or malformed")
    return value.strip()


def private_config_paths() -> tuple[Path, ...]:
    """Candidate locations for the shared owner-only private configuration."""

    candidates: list[Path] = []
    explicit = os.environ.get("SCOTTY_PRIVATE_CONFIG")
    if explicit:
        candidates.append(Path(explicit))
    home = os.environ.get("HERMES_HOME")
    if home:
        candidates.append(Path(home) / "scotty" / "private.json")
        # A profile home lives under <data>/profiles/<profile>.
        candidates.append(Path(home).parent.parent / "scotty" / "private.json")
    candidates.append(Path("/opt/data/scotty/private.json"))
    return tuple(dict.fromkeys(candidates))


def load_config(paths: tuple[Path, ...] | None = None) -> GuardConfig:
    """Read the exact maintainer tuple and the fixed wizard destination."""

    for path in paths if paths is not None else private_config_paths():
        if path.is_symlink() or not path.is_file():
            continue
        try:
            raw_bytes = path.read_bytes()
        except OSError:
            continue
        if len(raw_bytes) > _MAX_CONFIG_BYTES:
            raise GuardUnavailable("private configuration is oversized")
        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GuardUnavailable("private configuration is malformed") from exc
        if not isinstance(raw, Mapping):
            raise GuardUnavailable("private configuration is malformed")
        route = raw.get("maintainer_route")
        principals = raw.get("principals")
        if not isinstance(route, Mapping) or not isinstance(principals, Mapping):
            raise GuardUnavailable("private configuration is incomplete")
        operator = principals.get("main_operator")
        if not isinstance(operator, Mapping):
            raise GuardUnavailable("private configuration is incomplete")
        return GuardConfig(
            guild_id=_text(route.get("guild_id"), "route guild_id"),
            channel_id=_text(route.get("channel_id"), "route channel_id"),
            user_id=_text(route.get("user_id"), "route user_id"),
            operator_channel_id=_text(operator.get("channel_id"), "operator channel_id"),
            state_dir=path.parent,
        )
    raise GuardUnavailable("private configuration is unavailable")


def source_tuple(source: object) -> tuple[str, str, str] | None:
    """Immutable gateway provenance, or None when it cannot be trusted."""

    if getattr(getattr(source, "platform", None), "value", None) != "discord":
        return None
    if getattr(source, "is_bot", None) is not False:
        return None
    scope_id = getattr(source, "scope_id", None)
    guild_id = getattr(source, "guild_id", None)
    if scope_id is not None and guild_id is not None and scope_id != guild_id:
        return None
    guild = scope_id if scope_id is not None else guild_id
    channel = getattr(source, "chat_id", None)
    user = getattr(source, "user_id", None)
    parent = getattr(source, "parent_chat_id", None)
    if not all(type(value) is str and value for value in (guild, channel, user)):
        return None
    if parent is not None and (type(parent) is not str or not parent):
        return None
    # A thread is authorized only under its configured parent channel.
    return (str(guild), str(parent or channel), str(user))


def message_key(event: object, text: str) -> str:
    source = getattr(event, "source", None)
    for holder in (event, source):
        for attribute in ("message_id", "id"):
            value = getattr(holder, attribute, None)
            if type(value) is str and value:
                return hashlib.sha256(value.encode("utf-8")).hexdigest()
    parts = (
        str(getattr(source, "user_id", "")),
        str(getattr(source, "chat_id", "")),
        text,
        str(int(time.time())),
    )
    return hashlib.sha256(" ".join(parts).encode("utf-8")).hexdigest()


def claim_delivery(state_dir: Path, key: str) -> bool:
    if not key or any(character not in _HEX for character in key):
        return False
    directory = state_dir / _MARKER_DIRNAME
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            return False
        marker = directory / key
        if marker.is_symlink():
            return False
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError:
        return False
    os.close(descriptor)
    _trim(directory)
    return True


def _trim(directory: Path) -> None:
    try:
        markers = sorted(directory.iterdir(), key=lambda item: item.stat().st_mtime)
    except OSError:
        return
    for stale in markers[:-_MAX_MARKERS]:
        try:
            stale.unlink()
        except OSError:
            return


def send_fixed_message(channel_id: str, content: str) -> None:
    """POST one fixed message to one configured channel. No other capability."""

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token or not channel_id.isdigit():
        return
    body = json.dumps(
        {"content": content, "allowed_mentions": {"parse": []}}, separators=(",", ":")
    ).encode("utf-8")
    url = f"{_DISCORD_API}/channels/{urllib.parse.quote(channel_id)}/messages"
    request = urllib.request.Request(  # noqa: S310 - fixed HTTPS Discord endpoint
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20.0) as response:  # noqa: S310
            response.read(65_536)
    except (OSError, urllib.error.URLError):
        # An ambiguous outcome is never retried automatically.
        return


class MaintainerGuard:
    """Pre-dispatch gate for the full profile. Denies every other tuple."""

    def __init__(
        self,
        config: GuardConfig | None = None,
        send: object | None = None,
    ) -> None:
        self._config = config
        self._send = send

    def config(self) -> GuardConfig:
        if self._config is None:
            self._config = load_config()
        return self._config

    def __call__(self, event: object, **_: object) -> Mapping[str, str]:
        try:
            config = self.config()
        except GuardUnavailable:
            return SKIP_UNAUTHORIZED
        observed = source_tuple(getattr(event, "source", None))
        if observed != (config.guild_id, config.channel_id, config.user_id):
            return SKIP_UNAUTHORIZED
        text = getattr(event, "text", None)
        if type(text) is not str:
            return {"action": "skip", "reason": "malformed"}
        stripped = text.strip()
        if stripped == FIXED_WIZARD_COMMAND:
            if claim_delivery(config.state_dir, message_key(event, stripped)):
                send = self._send if self._send is not None else send_fixed_message
                send(config.operator_channel_id, SETUP_WIZARD)  # type: ignore[operator]
            return SKIP_WIZARD
        return ALLOW
