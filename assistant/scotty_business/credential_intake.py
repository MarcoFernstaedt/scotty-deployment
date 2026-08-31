"""Protected one-time credential intake, ahead of every model-visible path.

A normal Discord message containing a credential is never acceptable. This
module gives Trent one purpose-built alternative: from his exact authorized
private tuple he opens a single-use intake window with a fixed phrase, and the
*next* message in that exact channel is intercepted here, inside the
`pre_gateway_dispatch` hook, before event construction, batching, persistence,
queues, sessions, model dispatch, tools, logs, or ordinary chat history.

The sequence is prepare, confirmed exact source-message deletion, then commit
through a privilege-separated broker of fixed operations. Delete, validation,
provenance, timeout, replay, conflict, or commit failure aborts without
persistence. Nothing here returns, renders, or records credential material; the
only outputs are fixed redacted states and the next setup step.

If the platform cannot confirm deletion, or the installed privilege boundary is
unavailable, the intake fails closed and directs Trent to the approved hidden
local operator entry path instead.
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

    def __init__(self, socket_path: str | os.PathLike[str], *, timeout: float = 10.0) -> None:
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

    def _call(self, operation: str, provider: str, credential_class: str, material: str) -> bool:
        if not self.available():
            return False
        request = (
            json.dumps(
                {
                    "op": operation,
                    "provider": provider,
                    "credential_class": credential_class,
                    "material": material,
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(str(self.socket_path))
                client.sendall(request.encode("utf-8"))
                client.shutdown(socket.SHUT_WR)
                raw = client.recv(4096)
        except (OSError, ValueError):
            return False
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(reply, dict) and reply.get("ok") is True

    def validate(self, provider: str, credential_class: str, material: str) -> bool:
        return self._call("validate", provider, credential_class, material)

    def commit(self, provider: str, credential_class: str, material: str) -> bool:
        return self._call("commit", provider, credential_class, material)


class SourceMessageDeleter(Protocol):
    """Deletes one exact source message and confirms the platform accepted it."""

    def delete_message(self, channel_id: str, message_id: str) -> bool: ...


def _message_id(event: object) -> str | None:
    source = getattr(event, "source", None)
    for holder in (event, source):
        for attribute in ("message_id", "id"):
            value = getattr(holder, attribute, None)
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
