"""Who the assistant is, for the one person it is talking to.

"Scotty" is Trent's assistant, not the product; Mikey names his own; the
maintainer route is separate from both. So an assistant name is per client
user, chosen by that user, bounded, and never allowed to advertise the
framework or model provider underneath.

This is the whole of what this release lets an operator rename. The product's
own identifiers -- packages, tools, profiles, paths, commands, container,
network, environment variables -- are constants; see
`docs/white-label-rename-plan.md`, which is a plan and not a feature.

A name is only presentation. It carries no authority: renaming an assistant
never changes a role, a route, a credential, or an approval.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .policy import Role

if TYPE_CHECKING:  # pragma: no cover - config imports this module at runtime
    from .config import RuntimeConfig

#: The two roles that have a personal assistant. Named here rather than
#: imported so that configuration, which parses personas, stays importable
#: before routing is.
CLIENT_ROLES: tuple[Role, ...] = (Role.MAIN_OPERATOR, Role.EMPLOYEE)


class PersonaError(ValueError):
    """A requested assistant name is unusable, so nothing is stored."""


#: What an assistant is called before its user chooses. Deliberately neutral:
#: the product has no client-visible brand of its own.
DEFAULT_ASSISTANT_NAME = "Assistant"

MAX_NAME_CHARS = 40

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9 .'\-]{0,39}")

#: An assistant may not be named after what it runs on, or after anything that
#: would let it pass as a different product or a person with authority here.
_FORBIDDEN_FRAGMENTS = (
    "anthropic",
    "claude",
    "codex",
    "docker",
    "everyone",
    "gemini",
    "gpt",
    "grok",
    "here",
    "hermes",
    "imperator",
    "llama",
    "maintainer",
    "mistral",
    "nous",
    "openai",
    "openclaw",
    "openrouter",
    "systemd",
    "vaultwarden",
)


@dataclass(frozen=True, slots=True)
class Persona:
    """One client user's assistant, as that user sees it."""

    role: Role
    profile: str
    assistant_name: str

    def as_json(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "assistant_name": self.assistant_name,
            "changeable": True,
        }


def validate_assistant_name(value: object) -> str:
    """Return a usable assistant name, or refuse it with a fixed reason."""

    if type(value) is not str:
        raise PersonaError("an assistant name must be text")
    name = value.strip()
    if not name or len(name) > MAX_NAME_CHARS:
        raise PersonaError(f"an assistant name must be 1 to {MAX_NAME_CHARS} characters")
    if not _NAME.fullmatch(name):
        raise PersonaError("an assistant name may use only letters, digits, spaces, . ' and -")
    lowered = name.casefold()
    if any(fragment in lowered for fragment in _FORBIDDEN_FRAGMENTS):
        raise PersonaError("that name is reserved; please choose another")
    return name


class PersonaStore:
    """Per-user assistant names, kept beside the other private runtime state."""

    def __init__(self, path: Path, *, owner_uid: int | None = 10000):
        self.path = path
        self.owner_uid = owner_uid

    def read(self) -> dict[Role, str]:
        """Stored names, ignoring anything unusable rather than trusting it."""

        if self.path.is_symlink() or not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, Mapping):
            return {}
        names: dict[Role, str] = {}
        for role in CLIENT_ROLES:
            value = raw.get(role.value)
            if value is None:
                continue
            try:
                names[role] = validate_assistant_name(value)
            except PersonaError:
                # A tampered or stale file never renames an assistant into
                # something the product refuses to be called.
                continue
        return names

    def set(self, role: Role, name: object) -> str:
        """Store one client user's own choice. Validated before it is written."""

        if role not in CLIENT_ROLES:
            raise PersonaError("only a client user has a selectable assistant name")
        chosen = validate_assistant_name(name)
        names = {existing.value: value for existing, value in self.read().items()}
        names[role.value] = chosen
        self._write(names)
        return chosen

    def _write(self, names: Mapping[str, str]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise PersonaError("the persona state path is unsafe")
        payload = json.dumps(dict(names), sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if self.owner_uid is not None:
                with suppress(OSError, PermissionError):
                    os.chown(temporary, self.owner_uid, self.owner_uid)
            os.replace(temporary, self.path)
        except OSError as exc:
            raise PersonaError("the assistant name could not be stored") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def configured_personas(value: object) -> dict[Role, str]:
    """Parse the optional per-role persona defaults from private config."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PersonaError("personas must be an object keyed by client role")
    roles = {role.value: role for role in CLIENT_ROLES}
    unknown = set(value) - set(roles)
    if unknown:
        raise PersonaError("personas may name only the client roles")
    return {roles[name]: validate_assistant_name(entry) for name, entry in value.items()}


def resolve_persona(
    config: RuntimeConfig,
    role: Role,
    stored: Mapping[Role, str] | None = None,
) -> Persona:
    """This user's assistant: their own choice, else the configured default.

    A role with neither falls back to the neutral default rather than to the
    other user's assistant, so one person's name never leaks into another
    person's conversation.
    """

    if role not in CLIENT_ROLES:
        raise PersonaError("only a client user has an assistant persona")
    from .routing import CLIENT_PROFILES  # imported here: routing reads config

    chosen = (stored or {}).get(role) or config.personas.get(role) or DEFAULT_ASSISTANT_NAME
    return Persona(role=role, profile=CLIENT_PROFILES[role], assistant_name=chosen)


__all__ = [
    "CLIENT_ROLES",
    "DEFAULT_ASSISTANT_NAME",
    "MAX_NAME_CHARS",
    "Persona",
    "PersonaError",
    "PersonaStore",
    "configured_personas",
    "resolve_persona",
    "validate_assistant_name",
]
