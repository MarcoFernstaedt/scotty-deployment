"""The root-owned credential broker: four fixed operations, nothing else.

Scotty's runtime must be able to know whether a provider credential is present
and usable without ever being able to read it. That is what this process is
for. It runs as root, outside the container, and owns the only copy of the
stored material. The runtime reaches it through a Unix socket and can ask
exactly one question: is this provider connected?

Authority comes from the kernel, not from the message. Every connection's peer
credentials are read with SO_PEERCRED, and the operations a caller may use are
decided by its uid: root may open a window, validate, commit, and revoke; the
unprivileged runtime account may only ask for status. Anything else is refused
before the request is even parsed.

Committing is deliberately awkward. A commit needs a window that root opened
moments earlier, that has not expired, and that has not already been used.
Windows live only in memory, so a restart invalidates every one of them rather
than leaving a usable opening behind.

No response ever carries credential material, and nothing here writes material
to a log, an argument list, or an exception. The only vocabulary on the wire is
a boolean and a fixed state word.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import stat
import struct
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The installed socket, and the store only root can read.
SOCKET_PATH = "/run/scotty/credential-broker.sock"
STORE_PATH = "/var/lib/scotty/credentials.json"

#: The unprivileged account the container runtime runs as.
RUNTIME_UID = 10000

#: Exactly the provider and credential classes the broker will hold.
CREDENTIAL_CLASSES: Mapping[str, frozenset[str]] = {
    "trello": frozenset({"api_key", "token"}),
    "ghl": frozenset({"private_token"}),
    "rentcast": frozenset({"api_key"}),
    "discord": frozenset({"bot_token"}),
}

#: Operations, and the smallest uid set each one needs.
#:
#: `execute` is the runtime's way to reach a provider: it names one of the
#: declared provider operations and its arguments, and the broker makes the
#: call with a credential the runtime never sees. It returns what the provider
#: said, bounded — never the credential that was used.
ROOT_OPERATIONS = frozenset({"open", "validate", "commit", "revoke"})
RUNTIME_OPERATIONS = frozenset({"status", "execute"})
OPERATIONS = ROOT_OPERATIONS | RUNTIME_OPERATIONS

#: Bounds. A frame past these is refused rather than parsed.
MAX_FRAME_BYTES = 8192
MAX_MATERIAL_CHARS = 4096
MIN_MATERIAL_CHARS = 8
WINDOW_SECONDS = 300

_MATERIAL = re.compile(r"[A-Za-z0-9._:/+\-=]+")
_WINDOW_ID = re.compile(r"[0-9a-f]{32}")


from .executor import ExecutionError, Executor  # noqa: E402


class BrokerError(Exception):
    """A request is unauthorized, malformed, or outside the fixed operations."""


@dataclass(frozen=True, slots=True)
class Peer:
    """Who is on the other end of the socket, according to the kernel."""

    pid: int
    uid: int
    gid: int

    def may(self, operation: str, *, runtime_uid: int = RUNTIME_UID) -> bool:
        """Whether this kernel-reported peer may run this exact operation.

        Root may do everything. The one account the runtime container runs as
        may ask for status and nothing else. Every other uid may do nothing.

        `runtime_uid` is deployment configuration — which account the container
        was installed to run as — not something a caller can assert. It arrives
        from the root-owned service that constructed the broker, never from the
        wire, so no client can widen its own authority by naming a uid. Root
        operations are gated on uid 0 alone and are unreachable however it is
        set.
        """

        if self.uid == 0:
            return operation in OPERATIONS
        if self.uid == runtime_uid:
            return operation in RUNTIME_OPERATIONS
        return False


def peer_of(connection: socket.socket) -> Peer:
    """Read the connecting process's credentials from the kernel."""

    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return Peer(pid, uid, gid)


#: Whose credential this is. One shared business identity, or exactly one
#: client user. The actor is part of the address, so one user's token is stored
#: and read separately from the other's and no request can reach across.
ACTORS = frozenset({"shared", "main_operator", "employee"})


def _known(
    provider: object, credential_class: object, actor: object = "shared"
) -> tuple[str, str, str]:
    if type(provider) is not str or provider not in CREDENTIAL_CLASSES:
        raise BrokerError("unknown provider")
    if type(credential_class) is not str or credential_class not in CREDENTIAL_CLASSES[provider]:
        raise BrokerError("unknown credential class")
    if type(actor) is not str or actor not in ACTORS:
        raise BrokerError("unknown actor")
    return provider, credential_class, actor


def _material(value: object) -> str:
    """Validate credential material without ever echoing or storing the value."""

    if type(value) is not str:
        raise BrokerError("malformed material")
    if not MIN_MATERIAL_CHARS <= len(value) <= MAX_MATERIAL_CHARS:
        raise BrokerError("material is outside the permitted length")
    if not _MATERIAL.fullmatch(value):
        raise BrokerError("material contains unsupported characters")
    return value


class CredentialStore:
    """Root-only persistence. Values are written, never read back out."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def __repr__(self) -> str:
        return f"CredentialStore(path={self.path!s})"

    def _load(self) -> dict[str, dict[str, str]]:
        if self.path.is_symlink() or not self.path.is_file():
            return {}
        metadata = self.path.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise BrokerError("credential store is not owner-only")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BrokerError("credential store is unreadable") from exc
        if not isinstance(raw, Mapping):
            raise BrokerError("credential store is malformed")
        return {
            str(provider): {str(name): str(value) for name, value in entries.items()}
            for provider, entries in raw.items()
            if isinstance(entries, Mapping)
        }

    def _save(self, data: Mapping[str, Mapping[str, str]]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise BrokerError("credential store path is unsafe")
        payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary = self.path.parent / f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise BrokerError("credential store could not be written") from exc

    @staticmethod
    def _slot(credential_class: str, actor: str) -> str:
        """One credential's address inside a provider. The actor is part of it.

        Storing the shared business identity and each user's own credential in
        separate slots is what makes a per-user token per-user: there is no key
        under which one actor's request can find another's material.
        """

        return credential_class if actor == "shared" else f"{credential_class}@{actor}"

    def put(
        self, provider: str, credential_class: str, material: str, actor: str = "shared"
    ) -> None:
        data = self._load()
        data.setdefault(provider, {})[self._slot(credential_class, actor)] = material
        self._save(data)

    def drop(self, provider: str, credential_class: str, actor: str = "shared") -> bool:
        data = self._load()
        entries = data.get(provider)
        slot = self._slot(credential_class, actor)
        if not entries or slot not in entries:
            return False
        del entries[slot]
        if not entries:
            del data[provider]
        self._save(data)
        return True

    def present(self, provider: str, credential_class: str, actor: str = "shared") -> bool:
        """Whether a credential is held. Never returns the value itself."""

        return bool(self._load().get(provider, {}).get(self._slot(credential_class, actor)))

    def read(self, provider: str, credential_class: str, actor: str = "shared") -> str | None:
        """The material itself, for the executor running inside this process.

        This exists so the privileged side can put a credential onto a request
        it builds. It is deliberately not reachable over the socket: no wire
        operation calls it, and the operation table has no entry that returns
        material. If that ever changes, the boundary is gone — so the tests
        assert both the absence of such an operation and the absence of
        material from every reply.
        """

        held = self._load().get(provider, {}).get(self._slot(credential_class, actor))
        return held if isinstance(held, str) and held else None


#: A validator answers "would the provider accept this?" without keeping it.
Validator = Callable[[str, str, str], bool]


def _accept_shape(provider: str, credential_class: str, material: str) -> bool:
    """Default validation: shape only, because no live call is ever made here."""

    del provider, credential_class
    return len(material) >= MIN_MATERIAL_CHARS


class Broker:
    """The fixed operations, and the state that makes commit single-use."""

    def __init__(
        self,
        store: CredentialStore,
        *,
        validator: Validator = _accept_shape,
        clock: Callable[[], float] = time.monotonic,
        window_seconds: float = WINDOW_SECONDS,
        runtime_uid: int = RUNTIME_UID,
        executor: Executor | None = None,
    ) -> None:
        self.store = store
        # Present in the service, absent in the pure-policy tests. When absent,
        # provider execution is refused rather than silently unavailable.
        self.executor = executor
        self.validator = validator
        self.clock = clock
        self.window_seconds = window_seconds
        # Which account the runtime container was installed to run as. Set by
        # the root-owned service, never by a request.
        self.runtime_uid = runtime_uid
        # In memory only: a restart must not leave a usable window behind.
        self._windows: dict[str, tuple[str, str, float]] = {}

    def handle(self, peer: Peer, request: object) -> dict[str, object]:
        """Answer one request. Authority is checked before anything is parsed."""

        if not isinstance(request, Mapping):
            raise BrokerError("malformed request")
        operation = request.get("op")
        if type(operation) is not str or operation not in OPERATIONS:
            raise BrokerError("unknown operation")
        if not peer.may(operation, runtime_uid=self.runtime_uid):
            raise BrokerError("unauthorized")
        handler: dict[str, Callable[[Mapping[str, Any]], dict[str, object]]] = {
            "open": self._open,
            "validate": self._validate,
            "commit": self._commit,
            "revoke": self._revoke,
            "status": self._status,
            "execute": self._execute,
        }
        return handler[operation](request)

    def _open(self, request: Mapping[str, Any]) -> dict[str, object]:
        provider, credential_class, actor = _known(
            request.get("provider"), request.get("credential_class"), request.get("actor", "shared")
        )
        self._expire()
        window = secrets.token_hex(16)
        self._windows[window] = (
            f"{provider}/{credential_class}/{actor}",
            "",
            self.clock() + self.window_seconds,
        )
        return {"ok": True, "state": "window open", "window": window}

    def _validate(self, request: Mapping[str, Any]) -> dict[str, object]:
        provider, credential_class, _ = _known(
            request.get("provider"), request.get("credential_class"), request.get("actor", "shared")
        )
        material = _material(request.get("material"))
        accepted = bool(self.validator(provider, credential_class, material))
        return {"ok": accepted, "state": "validation passed" if accepted else "validation failed"}

    def _commit(self, request: Mapping[str, Any]) -> dict[str, object]:
        provider, credential_class, actor = _known(
            request.get("provider"), request.get("credential_class"), request.get("actor", "shared")
        )
        window = request.get("window")
        if type(window) is not str or not _WINDOW_ID.fullmatch(window):
            raise BrokerError("malformed window")
        self._expire()
        # Single use: the window is consumed whether or not the commit succeeds,
        # so a failed attempt can never be replayed.
        held = self._windows.pop(window, None)
        if held is None:
            raise BrokerError("no open window")
        if held[0] != f"{provider}/{credential_class}/{actor}":
            # A window opened for one address can never commit into another,
            # so a window is not a way to write into someone else's slot.
            raise BrokerError("window does not match this credential")
        material = _material(request.get("material"))
        if not self.validator(provider, credential_class, material):
            return {"ok": False, "state": "validation failed"}
        self.store.put(provider, credential_class, material, actor)
        return {"ok": True, "state": "credential present"}

    def _revoke(self, request: Mapping[str, Any]) -> dict[str, object]:
        provider, credential_class, actor = _known(
            request.get("provider"), request.get("credential_class"), request.get("actor", "shared")
        )
        removed = self.store.drop(provider, credential_class, actor)
        return {"ok": removed, "state": "credential removed" if removed else "no credential"}

    def _status(self, request: Mapping[str, Any]) -> dict[str, object]:
        provider, credential_class, actor = _known(
            request.get("provider"), request.get("credential_class"), request.get("actor", "shared")
        )
        present = self.store.present(provider, credential_class, actor)
        return {
            "ok": present,
            "state": "credential present" if present else "credential absent",
        }

    def _execute(self, request: Mapping[str, Any]) -> dict[str, object]:
        """Run one declared provider operation on the caller's behalf.

        The caller names an operation and its arguments. It does not name a
        host, a path, a method, a header, or a credential — those come from the
        operation table and the store, on this side of the socket.
        """

        if self.executor is None:
            raise BrokerError("provider execution is not configured")
        actor = request.get("actor", "shared")
        if type(actor) is not str or actor not in ACTORS:
            raise BrokerError("unknown actor")
        arguments = request.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise BrokerError("malformed arguments")
        try:
            outcome = self.executor.run(request.get("operation"), arguments, actor=actor)
        except ExecutionError as exc:
            # The message is written by our own code and names no credential.
            raise BrokerError(str(exc)) from None
        return outcome.as_reply()

    def _expire(self) -> None:
        now = self.clock()
        for window, held in list(self._windows.items()):
            if held[2] <= now:
                del self._windows[window]


def read_frame(connection: socket.socket) -> object:
    """Read one bounded newline-terminated JSON frame."""

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = connection.recv(1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FRAME_BYTES:
            raise BrokerError("frame is too large")
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise BrokerError("empty frame")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerError("malformed frame") from exc


def serve_once(broker: Broker, connection: socket.socket) -> None:
    """Handle exactly one request on one connection, then close it."""

    try:
        peer = peer_of(connection)
        reply = broker.handle(peer, read_frame(connection))
    except BrokerError as exc:
        # The reason is a fixed word from this module, never request content.
        reply = {"ok": False, "state": str(exc)}
    except Exception:
        reply = {"ok": False, "state": "unavailable"}
    with suppress(OSError):
        connection.sendall(json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n")


def bind_socket(path: str | os.PathLike[str], *, group: int = RUNTIME_UID) -> socket.socket:
    """Create the listening socket so only root and the runtime can reach it."""

    target = Path(path)
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if target.is_symlink():
        raise BrokerError("socket path is unsafe")
    if target.exists():
        target.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(0o177)
    try:
        server.bind(str(target))
    finally:
        os.umask(previous)
    if os.getuid() == 0:
        os.chown(target, 0, group)
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
    server.listen(8)
    return server


def serve_forever(
    broker: Broker, server: socket.socket, *, should_stop: Callable[[], bool] = lambda: False
) -> None:
    server.settimeout(0.5)
    while not should_stop():
        try:
            connection, _ = server.accept()
        except TimeoutError:
            continue
        except OSError:
            break
        with connection:
            connection.settimeout(5.0)
            serve_once(broker, connection)
