"""Credential intake, and why the Discord half of it is switched off.

A normal Discord message containing a credential is never acceptable. The
intended alternative was a purpose-built window: Trent names a credential class
with a fixed phrase, and his *next* message in that exact channel is intercepted
before event construction, batching, persistence, queues, sessions, model
dispatch, tools, or ordinary chat history, then deleted and committed through a
privilege-separated broker.

That design depends on a boundary the runtime must provide. `pre_gateway_dispatch`
receives an event that has already been constructed, so a hook there cannot
prove it ran before construction or persistence. The earliest raw-message
boundary would have to come from the pinned Hermes 0.20.6 Discord adapter, and
this repository has not been able to inspect that image to confirm one exists.

Rather than claim a guarantee that has not been verified, Discord intake is off.
`DISCORD_INTAKE_ENABLED` is False and the module refuses to open a window on any
route. The intake phrases are still recognised, deterministically and before the
model, so Trent gets a specific answer pointing at the local hidden-input setup
command instead of silence. Every credential-shaped message continues to be
stopped by the ingress leak scan before model dispatch.

The mechanism below is retained, tested, and inert. It becomes reachable only
when a verified pre-event boundary is installed and attested, which is a change
to this constant plus the evidence that justifies it — never a runtime toggle,
an environment variable, or a configuration value a message could influence.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .config import RuntimeConfig
from .policy import Role
from .routing import Route, RouteKind

#: Exact phrases that open one intake window. Fixed text, matched before any
#: model sees the message, so no inference decides that an intake is starting.
INTAKE_COMMANDS: Mapping[str, tuple[str, str]] = {
    "Scotty, accept my Trello API key.": ("trello", "api_key"),
    "Scotty, accept my Trello token.": ("trello", "token"),
    "Scotty, accept my GoHighLevel private token.": ("ghl", "private_token"),
    "Scotty, accept my RentCast API key.": ("rentcast", "api_key"),
}

#: Google Workspace is deliberately absent above: it uses provider-owned browser
#: consent, so no Google credential is ever handed to Scotty through Discord.

#: Discord intake stays off until the pinned runtime's earliest raw-message
#: boundary is inspected and attested. This is a source constant on purpose: no
#: environment variable, configuration value, or message can turn it on.
DISCORD_INTAKE_ENABLED = False

DISCORD_INTAKE_DISABLED_INSTRUCTION = (
    "Scotty cannot accept a credential through Discord at all. Deleting a "
    "message afterwards is not the same as never storing it, and that guarantee "
    "is not currently proven for this runtime. Enter the credential through the "
    "local hidden-input setup command on the server instead, and if you already "
    "pasted one anywhere, rotate it."
)

#: The one fixed path of the installed root-owned privilege boundary.
BROKER_SOCKET = "/run/scotty/credential-broker.sock"

WINDOW_SECONDS = 300

#: A credential is one opaque token: no whitespace, no control characters.
_MATERIAL = re.compile(r"[A-Za-z0-9._:/+\-=]{8,4096}")

OPEN_INSTRUCTION = (
    "Send only the credential as your very next message in this channel. Scotty "
    "intercepts it before anything else sees it, deletes your message, and stores "
    "it outside chat. Nothing is echoed back. Send anything else and the window "
    "closes. The window expires in five minutes."
)
BOUNDARY_UNAVAILABLE_INSTRUCTION = (
    "Scotty cannot accept a credential here right now: the protected intake path "
    "is not available on this server. Use the local hidden-input setup command "
    "instead, and never paste the credential into this channel."
)
LOCAL_FALLBACK_INSTRUCTION = (
    "Scotty could not confirm that your message was deleted, so nothing was "
    "stored. Treat that credential as exposed and rotate it now, then enter the "
    "replacement through the local hidden-input setup command on the server."
)


class IntakeStatus(StrEnum):
    """Fixed redacted outcomes. No status ever carries credential material."""

    STORED = "credential present"
    VALIDATION_FAILED = "validation failed"
    DELETE_UNAVAILABLE = "source deletion unavailable"
    COMMIT_FAILED = "commit failed"
    EXPIRED = "window expired"
    MALFORMED = "credential malformed"


@dataclass(frozen=True, slots=True)
class IntakeOutcome:
    """What Scotty may say about an intake: a state and the next setup step."""

    status: IntakeStatus
    provider: str
    credential_class: str
    next_step: str

    def as_text(self) -> str:
        return f"{self.provider}: {self.status.value}. {self.next_step}"


@dataclass(frozen=True, slots=True)
class IntakeWindow:
    """One single-use window bound to every part of its authorized origin."""

    profile: str
    guild_id: str
    channel_id: str
    user_id: str
    role: Role
    provider: str
    credential_class: str
    expires_at: int
    nonce: str = field(default_factory=lambda: secrets.token_hex(16))

    def matches(self, route: Route, source: object) -> bool:
        principal = route.principal
        if principal is None or route.kind is not RouteKind.CLIENT:
            return False
        return (
            route.profile == self.profile
            and principal.guild_id == self.guild_id
            and principal.channel_id == self.channel_id
            and principal.user_id == self.user_id
            and principal.role is self.role
            and getattr(source, "chat_id", None) == self.channel_id
            and getattr(source, "user_id", None) == self.user_id
        )


class CredentialBroker(Protocol):
    """Privilege-separated broker of exactly two fixed operations.

    Neither operation returns credential material, and no other operation is
    reachable from this module. `available` reports whether the installed
    privilege boundary is actually present; when it is not, intake fails closed.
    """

    def available(self) -> bool: ...

    def validate(self, provider: str, credential_class: str, material: str) -> bool: ...

    def commit(self, provider: str, credential_class: str, material: str) -> bool: ...


class UnixSocketBroker:
    """Client for the root-owned fixed-operation credential broker.

    The broker runs outside this process behind a socket that only root owns, so
    the model-visible runtime can ask it to validate and store a credential but
    can never read one back. Only two operations exist on the wire, the reply is
    a single boolean, and no response is ever logged or rendered.
    """

    def __init__(self, socket_path: str | os.PathLike[str], *, timeout: float = 2.0) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"UnixSocketBroker(socket_path={self.socket_path!s})"

    def available(self) -> bool:
        try:
            if self.socket_path.is_symlink():
                return False
            return stat.S_ISSOCK(self.socket_path.stat().st_mode)
        except OSError:
            return False

    def _request(self, frame: Mapping[str, object]) -> Mapping[str, object] | None:
        """Send one frame and return the broker's reply, or None if unreachable."""

        if not self.available():
            return None
        request = json.dumps(dict(frame), separators=(",", ":")) + "\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(str(self.socket_path))
                client.sendall(request.encode("utf-8"))
                client.shutdown(socket.SHUT_WR)
                raw = client.recv(4096)
        except (OSError, ValueError):
            return None
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return reply if isinstance(reply, Mapping) else None

    def _call(
        self, operation: str, provider: str, credential_class: str, material: str | None
    ) -> bool:
        frame: dict[str, object] = {
            "op": operation,
            "provider": provider,
            "credential_class": credential_class,
        }
        if material is not None:
            frame["material"] = material
        reply = self._request(frame)
        return reply is not None and reply.get("ok") is True

    def validate(self, provider: str, credential_class: str, material: str) -> bool:
        return self._call("validate", provider, credential_class, material)

    def commit(self, provider: str, credential_class: str, material: str) -> bool:
        """Open a single-use window and commit into it, as the broker requires.

        Only a privileged caller can do this; the runtime account is permitted
        `status` alone, so a commit attempted from the container is refused by
        the broker rather than half-completed here.
        """

        opened = self._request(
            {"op": "open", "provider": provider, "credential_class": credential_class}
        )
        if opened is None or opened.get("ok") is not True:
            return False
        window = opened.get("window")
        if type(window) is not str or not window:
            return False
        reply = self._request(
            {
                "op": "commit",
                "provider": provider,
                "credential_class": credential_class,
                "window": window,
                "material": material,
            }
        )
        return reply is not None and reply.get("ok") is True

    def status(self, provider: str, credential_class: str, actor: str = "shared") -> bool:
        """Whether the broker holds this credential. Never returns the value."""

        reply = self._request(
            {
                "op": "status",
                "provider": provider,
                "credential_class": credential_class,
                "actor": actor,
            }
        )
        return bool(reply and reply.get("ok") is True)

    def execute(
        self, operation: str, arguments: Mapping[str, object], *, actor: str = "shared"
    ) -> Mapping[str, object] | None:
        """Ask the broker to run one declared provider operation.

        The reply carries what the provider said, bounded. It never carries the
        credential the broker used, and this side never had one to begin with.
        None means the broker did not answer, which is not the same as a
        refusal: the provider may or may not have acted.
        """

        return self._request(
            {
                "op": "execute",
                "operation": operation,
                "actor": actor,
                "arguments": dict(arguments),
            }
        )


class SourceMessageDeleter(Protocol):
    """Deletes one exact source message and confirms the platform accepted it."""

    def delete_message(self, channel_id: str, message_id: str) -> bool: ...


def _message_id(event: object) -> str | None:
    """The exact source message id, or None.

    Only an explicitly named message id counts. A generic `id` could be a
    session or event identifier, and deleting by it would either fail or delete
    the wrong thing while the credential stayed in the channel.
    """

    source = getattr(event, "source", None)
    for holder in (event, source):
        value = getattr(holder, "message_id", None)
        if type(value) is str and value:
            return value
    return None


class CredentialIntake:
    """Hold at most one open intake window and resolve it exactly once."""

    def __init__(
        self,
        config: RuntimeConfig,
        enqueue: Callable[[str, str], object],
        *,
        broker: CredentialBroker,
        deleter: SourceMessageDeleter,
        clock: Callable[[], int],
    ) -> None:
        self.config = config
        self.enqueue = enqueue
        self.broker = broker
        self.deleter = deleter
        self.clock = clock
        self._window: IntakeWindow | None = None

    # -- opening --------------------------------------------------------

    def open_window(self, route: Route, phrase: object) -> bool:
        """Open one window for an exact phrase from the exact operator tuple."""

        if type(phrase) is not str:
            return False
        target = INTAKE_COMMANDS.get(phrase)
        if target is None:
            return False
        principal = route.principal
        if not DISCORD_INTAKE_ENABLED:
            # Answer the exact phrase with the safe path rather than silence.
            if principal is not None:
                self.enqueue(principal.channel_id, DISCORD_INTAKE_DISABLED_INSTRUCTION)
            return False
        if (
            route.kind is not RouteKind.CLIENT
            or principal is None
            or principal.role is not Role.MAIN_OPERATOR
        ):
            return False
        if not self._broker_available():
            # No installed privilege boundary means no protected intake at all.
            self.enqueue(principal.channel_id, BOUNDARY_UNAVAILABLE_INSTRUCTION)
            return False
        if self._window is not None and not self._expired(self._window):
            # A conflicting second window is refused rather than replacing the
            # one Trent is already answering.
            return False
        provider, credential_class = target
        self._window = IntakeWindow(
            profile=route.profile,
            guild_id=principal.guild_id,
            channel_id=principal.channel_id,
            user_id=principal.user_id,
            role=principal.role,
            provider=provider,
            credential_class=credential_class,
            expires_at=int(self.clock()) + WINDOW_SECONDS,
        )
        self.enqueue(principal.channel_id, OPEN_INSTRUCTION)
        return True

    def _broker_available(self) -> bool:
        try:
            return bool(self.broker.available())
        except Exception:
            return False

    def has_open_window(self) -> bool:
        window = self._window
        return window is not None and not self._expired(window)

    def _expired(self, window: IntakeWindow) -> bool:
        return int(self.clock()) >= window.expires_at

    # -- interception ---------------------------------------------------

    def intercept(self, event: object, route: Route | None) -> IntakeOutcome | None:
        """Consume the next expected credential, or return None to pass through.

        Returning an outcome means the message was an intake attempt and must go
        no further: it is never queued, dispatched, persisted, or logged.
        """

        if not DISCORD_INTAKE_ENABLED:
            # No window can exist, so nothing is ever consumed from Discord.
            return None
        window = self._window
        if window is None or route is None:
            return None
        source = getattr(event, "source", None)
        if not window.matches(route, source):
            # Another tuple never consumes or satisfies Trent's window.
            return None
        self._window = None

        message_id = _message_id(event)
        if message_id is None:
            # Without an exact source message there is no confirmed deletion.
            return self._fail_closed(window, IntakeStatus.DELETE_UNAVAILABLE)
        if self._expired(window):
            self._delete(window, message_id)
            return self._outcome(window, IntakeStatus.EXPIRED)

        text = getattr(event, "text", None)
        material = text.strip() if type(text) is str else ""
        if not _MATERIAL.fullmatch(material):
            self._delete(window, message_id)
            return self._outcome(window, IntakeStatus.MALFORMED)

        try:
            valid = bool(self.broker.validate(window.provider, window.credential_class, material))
        except Exception:
            valid = False
        if not self._delete(window, message_id):
            return self._fail_closed(window, IntakeStatus.DELETE_UNAVAILABLE)
        if not valid:
            return self._outcome(window, IntakeStatus.VALIDATION_FAILED)
        try:
            committed = bool(self.broker.commit(window.provider, window.credential_class, material))
        except Exception:
            committed = False
        if not committed:
            return self._outcome(window, IntakeStatus.COMMIT_FAILED)
        return self._outcome(window, IntakeStatus.STORED)

    def _delete(self, window: IntakeWindow, message_id: str) -> bool:
        try:
            return bool(self.deleter.delete_message(window.channel_id, message_id))
        except Exception:
            return False

    def _fail_closed(self, window: IntakeWindow, status: IntakeStatus) -> IntakeOutcome:
        self.enqueue(window.channel_id, LOCAL_FALLBACK_INSTRUCTION)
        return self._outcome(window, status)

    def _outcome(self, window: IntakeWindow, status: IntakeStatus) -> IntakeOutcome:
        outcome = IntakeOutcome(
            status=status,
            provider=window.provider,
            credential_class=window.credential_class,
            next_step=_NEXT_STEP[status],
        )
        self.enqueue(window.channel_id, outcome.as_text())
        return outcome


_NEXT_STEP: Mapping[IntakeStatus, str] = {
    IntakeStatus.STORED: "Ask Scotty for setup status to see what remains.",
    IntakeStatus.VALIDATION_FAILED: (
        "The provider rejected it and nothing was stored. Issue a new credential "
        "and open a fresh intake window."
    ),
    IntakeStatus.DELETE_UNAVAILABLE: LOCAL_FALLBACK_INSTRUCTION,
    IntakeStatus.COMMIT_FAILED: (
        "Nothing was stored. Open a fresh intake window, or use the local "
        "hidden-input setup command on the server."
    ),
    IntakeStatus.EXPIRED: "The window closed before that arrived. Open a fresh one.",
    IntakeStatus.MALFORMED: (
        "That did not look like a single credential, so nothing was stored. Open a "
        "fresh window and send only the credential."
    ),
}
