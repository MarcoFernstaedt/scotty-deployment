"""Guided, conversational integration setup for the authorized client channel.

Scotty walks Trent through connecting Discord, Trello, Google Workspace,
GoHighLevel and RentCast from his own private channel. It explains what each
integration enables, names the provider-console steps, the APIs to enable, the
scopes to grant, the identifiers to collect and the callback behaviour; it
validates the non-secret identifiers he gives, shows what is still missing,
diagnoses a failure with the next correction, and resumes at the first
unfinished step.

Two boundaries hold throughout. No credential is ever collected here: secrets
travel only through the protected intake or local hidden input. And nothing here
edits live configuration; validated identifiers are staged into Scotty-owned
owner-only state that the root-owned local setup command consumes as prefill.
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

from .config import RuntimeConfig
from .guidance import CONNECTED, NOT_CONNECTED, PROVIDERS, provider_guidance

LOCAL_SETUP_COMMAND = "sudo /usr/local/sbin/scotty-start"


class SetupFlowError(ValueError):
    """An identifier is malformed, or the named setup field does not exist."""


@dataclass(frozen=True, slots=True)
class IdentifierField:
    """One non-secret value Trent can supply conversationally."""

    provider: str
    field: str
    label: str
    how_to_find: str

    def as_text(self) -> str:
        return f"{self.label}: {self.how_to_find}"


_SNOWFLAKE = re.compile(r"[0-9]{17,20}")
_PROVIDER_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_ENDPOINT = re.compile(r"/v1/[A-Za-z0-9/_\-]{1,80}")

REQUIRED_IDENTIFIERS: Mapping[str, tuple[IdentifierField, ...]] = {
    "discord": (
        IdentifierField(
            "discord",
            "guild_id",
            "Discord server ID",
            "Turn on Developer Mode, right-click the server name, and Copy Server ID.",
        ),
        IdentifierField(
            "discord",
            "operator_channel_id",
            "your private channel ID",
            "Right-click your private channel and Copy Channel ID.",
        ),
        IdentifierField(
            "discord",
            "employee_channel_id",
            "the employee private channel ID",
            "Right-click that channel and Copy Channel ID.",
        ),
    ),
    "trello": (
        IdentifierField(
            "trello",
            "board_id",
            "Trello board ID",
            "Open the board and add .json to the URL; the id field at the top is the board ID.",
        ),
        IdentifierField(
            "trello",
            "list_id",
            "each Trello list ID Scotty may use",
            "In that same .json, each entry under lists has its own id.",
        ),
    ),
    "ghl": (
        IdentifierField(
            "ghl",
            "location_id",
            "GoHighLevel location ID",
            "Open the sub-account settings; the location ID is on the business profile page.",
        ),
    ),
    "rentcast": (
        IdentifierField(
            "rentcast",
            "endpoint",
            "each RentCast endpoint path in scope",
            "Use the exact v1 paths from the RentCast docs, such as /v1/properties.",
        ),
    ),
    "google_workspace": (
        IdentifierField(
            "google_workspace",
            "account_email",
            "the Google Workspace account email",
            "The account you want Scotty to work in; consent must be completed as that account.",
        ),
    ),
}

#: Fixed diagnoses. Each names the likely cause and the next correction, so a
#: setup failure is never answered with a generic refusal or a documentation
#: pointer alone.
_DIAGNOSES: Mapping[str, str] = {
    "authentication": (
        "The provider rejected the credential itself. It is missing, mistyped, revoked, "
        "or issued from the wrong account. Issue a fresh one and hand it over through the "
        "protected intake or local setup."
    ),
    "authorization": (
        "The credential is valid but is not permitted on this resource. Check that it was "
        "issued from the account that owns the resource, then re-grant the listed scopes."
    ),
    "scope": (
        "The credential is missing a required scope. Re-grant exactly the scopes listed for "
        "this provider and reconnect; a partial grant is refused rather than used."
    ),
    "identifier": (
        "The identifier does not match anything on the provider. Recheck it against the "
        "source listed for that field, then give it to Scotty again."
    ),
    "callback": (
        "Browser consent did not return to the local loopback address. Complete consent in a "
        "browser on the server itself, allow the loopback redirect, and do not add a public "
        "redirect URI."
    ),
    "rate_limit": (
        "The provider is rate limiting this deployment. Nothing was lost; wait for the window "
        "to reset before retrying, and avoid bulk operations until it clears."
    ),
    "schema": (
        "The provider returned a response Scotty does not recognise, so nothing was applied. "
        "This usually means an API version or plan change on the provider side."
    ),
    "stale_configuration": (
        "The stored configuration no longer matches the provider. Recheck the identifiers for "
        "this provider, then rerun local setup so the corrected values take effect."
    ),
    "unknown": (
        "The failure is not one Scotty recognises, so nothing was applied and nothing was "
        "retried. Ask for setup status to see the exact remaining step."
    ),
}

FAILURE_KINDS: tuple[str, ...] = tuple(_DIAGNOSES)


def _normalize(value: object) -> str:
    if type(value) is not str:
        raise SetupFlowError("that value is not text")
    stripped = value.strip()
    if not stripped or len(stripped) > 256 or any(ord(char) < 32 for char in stripped):
        raise SetupFlowError("that value is empty, too long, or contains control characters")
    return stripped


def identifier_field(provider: object, field: object) -> IdentifierField:
    if type(provider) is not str or provider not in REQUIRED_IDENTIFIERS:
        raise SetupFlowError("that provider is not part of this deployment")
    for candidate in REQUIRED_IDENTIFIERS[provider]:
        if candidate.field == field:
            return candidate
    raise SetupFlowError("that setup field is not one Scotty collects")


def validate_identifier(provider: object, field: object, value: object) -> str:
    """Validate one non-secret identifier and return it, or say what is wrong."""

    known = identifier_field(provider, field)
    candidate = _normalize(value)
    if known.provider == "discord":
        if not _SNOWFLAKE.fullmatch(candidate):
            raise SetupFlowError(
                "a Discord ID is 17 to 20 digits with nothing else; "
                "copy it with Developer Mode rather than typing the name"
            )
    elif known.provider == "google_workspace":
        if not _EMAIL.fullmatch(candidate) or len(candidate) > 254:
            raise SetupFlowError("that is not a Google Workspace account email address")
    elif known.provider == "rentcast":
        if not _ENDPOINT.fullmatch(candidate) or ".." in candidate:
            raise SetupFlowError("a RentCast endpoint is a fixed path such as /v1/properties")
    elif not _PROVIDER_ID.fullmatch(candidate):
        raise SetupFlowError(
            "that does not look like a provider ID; it should be 8 to 64 characters of "
            "letters, digits, hyphens, or underscores, copied from the provider"
        )
    return candidate


def diagnose(provider: object, failure: object) -> str:
    """Return a fixed diagnosis and the next correction for one failure kind."""

    if type(provider) is not str or provider not in PROVIDERS:
        raise SetupFlowError("that provider is not part of this deployment")
    kind = failure if type(failure) is str and failure in _DIAGNOSES else "unknown"
    guidance = provider_guidance(provider)
    lines = [f"{guidance.display_name}: {_DIAGNOSES[kind]}"]
    if kind in {"scope", "authorization"}:
        lines.extend(f"  - {item}" for item in guidance.required_scopes)
    if kind == "identifier":
        lines.extend(f"  - {item.as_text()}" for item in REQUIRED_IDENTIFIERS.get(provider, ()))
    if kind == "callback" and guidance.callback:
        lines.append(f"  - {guidance.callback}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ProviderProgress:
    """What is done, what is missing, and the single next action."""

    provider: str
    display_name: str
    status: str
    configured: bool
    credential_present: bool
    missing: tuple[str, ...]
    next_action: str

    @property
    def finished(self) -> bool:
        return self.status == CONNECTED and not self.missing


def _configured_identifiers(config: RuntimeConfig) -> dict[str, tuple[str, ...]]:
    """Which identifier fields the private configuration already carries."""

    present: dict[str, tuple[str, ...]] = {"discord": ("guild_id",)}
    channels = tuple(
        field
        for field, value in (
            ("operator_channel_id", config.principals[0].channel_id),
            ("employee_channel_id", config.principals[1].channel_id),
        )
        if value
    )
    present["discord"] = (*present["discord"], *channels)
    present["trello"] = ("board_id", "list_id") if config.trello is not None else ()
    present["ghl"] = ("location_id",) if config.ghl_location_id else ()
    present["rentcast"] = ("endpoint",) if config.rentcast_endpoints else ()
    present["google_workspace"] = ("account_email",) if config.google_workspace is not None else ()
    return present


def setup_progress(
    config: RuntimeConfig,
    connected: Mapping[str, bool],
    staged: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[ProviderProgress, ...]:
    """Report every provider's setup state in one fixed order."""

    configured = _configured_identifiers(config)
    staged = staged or {}
    result: list[ProviderProgress] = []
    for provider in PROVIDERS:
        have = set(configured.get(provider, ()))
        have.update(staged.get(provider, {}))
        missing = tuple(
            item.label for item in REQUIRED_IDENTIFIERS[provider] if item.field not in have
        )
        is_connected = bool(connected.get(provider, False))
        guidance = provider_guidance(provider, connected=is_connected)
        result.append(
            ProviderProgress(
                provider=provider,
                display_name=guidance.display_name,
                status=CONNECTED if is_connected else NOT_CONNECTED,
                configured=not missing,
                credential_present=is_connected,
                missing=missing,
                next_action=_next_action(provider, is_connected, missing),
            )
        )
    return tuple(result)


def _next_action(provider: str, connected: bool, missing: tuple[str, ...]) -> str:
    if connected and not missing:
        return "Nothing further. This integration is connected."
    if missing:
        fields = REQUIRED_IDENTIFIERS[provider]
        pending = next(item for item in fields if item.label == missing[0])
        return f"Send Scotty {pending.label}. {pending.how_to_find}"
    if provider == "google_workspace":
        return (
            "Complete Google's browser consent on the server as the configured account, "
            f"then run {LOCAL_SETUP_COMMAND}."
        )
    if provider == "discord":
        return f"Run {LOCAL_SETUP_COMMAND} so the bot token is entered through hidden input."
    return (
        "Hand Scotty the credential through the protected intake, or enter it through the "
        f"local hidden-input setup command, then run {LOCAL_SETUP_COMMAND}."
    )


def first_unfinished(progress: tuple[ProviderProgress, ...]) -> ProviderProgress | None:
    """Resume point: the first provider that is not fully connected."""

    for item in progress:
        if not item.finished:
            return item
    return None


class SetupStagingStore:
    """Owner-only staging of validated non-secret identifiers.

    The local setup command reads this as prefill. Nothing secret is ever
    accepted, and only fields Scotty actually collects can be written.
    """

    def __init__(self, path: str | os.PathLike[str], *, owner_uid: int = 10000):
        self.path = Path(path)
        self.owner_uid = owner_uid

    def __repr__(self) -> str:
        return f"SetupStagingStore(path={self.path!s})"

    def read(self) -> dict[str, dict[str, str]]:
        if self.path.is_symlink() or not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, Mapping):
            return {}
        staged: dict[str, dict[str, str]] = {}
        for provider, values in raw.items():
            if provider not in REQUIRED_IDENTIFIERS or not isinstance(values, Mapping):
                continue
            for field, value in values.items():
                try:
                    checked = validate_identifier(provider, field, value)
                except SetupFlowError:
                    continue
                staged.setdefault(provider, {})[field] = checked
        return staged

    def stage(self, provider: object, field: object, value: object) -> dict[str, dict[str, str]]:
        """Validate one identifier and persist it atomically, owner-only."""

        checked = validate_identifier(provider, field, value)
        staged = self.read()
        staged.setdefault(str(provider), {})[str(field)] = checked
        payload = json.dumps(staged, sort_keys=True, separators=(",", ":")).encode()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise SetupFlowError("the setup staging path is unsafe")
        temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise SetupFlowError("the setup staging file could not be written") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        return staged
