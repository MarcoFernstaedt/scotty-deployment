"""The root-owned credential broker: fixed operations, kernel-decided identity.

Scotty's runtime must be able to spend a provider credential without ever being
able to read one. That is what this process is for. It runs as root, outside
every container, and owns the only copy of the stored material.

The authority model is the part worth reading. An earlier version took the
actor out of the request: a caller said `"actor": "employee"` and the broker
believed it. Every process running as the one runtime account -- a compromised
plugin, a maintainer shell, anything sharing that uid -- could therefore act as
either client user. A boundary whose authority comes from the message it is
protecting is not a boundary.

So identity comes from the kernel, twice. Each actor has its own listening
socket, owned by that actor's own group and unreachable by anyone else, and the
actor *is* whichever socket the connection arrived on. `SO_PEERCRED` then
confirms the connecting process really runs as that actor's account. A request
that names an actor is refused outright rather than ignored, because a caller
that can ask and be quietly overruled is a caller somebody will eventually
trust.

What that buys is worth stating plainly: the three runtime workers run as three
different accounts, so "act as the other user" is not a check one of them can
fail to make -- it is a socket they cannot open.

On top of that the privileged side proves two more things before it spends
anything. A shared business identity needs an explicit root-written grant
naming the actor, provider, operations and resources. Anything with a
consequence needs an approval bound to this exact actor, operation, payload and
resource, spent once, inside its deadline. Neither is reachable from an actor
socket.

No response ever carries credential material, and nothing here writes material
to a log, an argument list, or an exception.
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: The root-only control socket, and the store only root can read. Opening a
#: window, committing, revoking, granting and approving all happen here, and
#: this socket is mode 0600 so nothing but root can even connect.
SOCKET_PATH = "/run/scotty/credential-broker.sock"
STORE_PATH = "/var/lib/scotty/credentials.json"

#: One account per actor, and one socket per actor owned by that account's
#: group. These are the whole authority model: a worker running as the employee
#: account cannot open the main operator's socket, so it cannot ask as them.
#:
#: 10000 -- the single account every profile used to share -- is deliberately
#: not among them. Nothing runs as it any more, and nothing may reach an actor
#: socket as it.
ACTOR_UIDS: Mapping[str, int] = {
    "main_operator": 10001,
    "employee": 10002,
    "maintainer": 10003,
}

#: One directory per actor, holding that actor's socket and nothing else. The
#: directory rather than the file is what gets bind-mounted into that actor's
#: worker, so a worker cannot see another actor's socket at all -- not because
#: it is refused, but because it is not in its filesystem.
ACTOR_SOCKET_DIRS: Mapping[str, str] = {
    "main_operator": "/run/scotty/broker/main_operator",
    "employee": "/run/scotty/broker/employee",
    "maintainer": "/run/scotty/broker/maintainer",
}

#: What every worker sees, at the same path, because each one has a different
#: directory mounted there.
WORKER_SOCKET = "/run/scotty/broker/broker.sock"

ACTOR_SOCKETS: Mapping[str, str] = {
    actor: f"{directory}/broker.sock" for actor, directory in ACTOR_SOCKET_DIRS.items()
}

#: Kept for the installer and the supervisor, which still speak of "the runtime
#: account" when they mean the set of them.
RUNTIME_UIDS = frozenset(ACTOR_UIDS.values())

#: The account the pinned single-gateway runtime container runs as.
#:
#: That container is not an actor and never can be: it is one process serving
#: three profiles, so no uid it could have would say which person is asking. It
#: gets its own socket, on which nothing is answered until the request cites a
#: Discord message and the broker asks Discord who wrote it.
RUNTIME_UID = 10000
RUNTIME_ACTOR = "runtime"

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
ROOT_OPERATIONS = frozenset({"open", "validate", "commit", "revoke", "grant", "approve"})

#: What a worker may ask on its own actor socket. Nothing here writes a
#: credential, a grant, or an approval.
ACTOR_OPERATIONS = frozenset({"status", "execute"})
OPERATIONS = ROOT_OPERATIONS | ACTOR_OPERATIONS

#: Bounds. A frame past these is refused rather than parsed.
MAX_FRAME_BYTES = 8192
MAX_MATERIAL_CHARS = 4096
MIN_MATERIAL_CHARS = 8
WINDOW_SECONDS = 300

_MATERIAL = re.compile(r"[A-Za-z0-9._:/+\-=]+")
_WINDOW_ID = re.compile(r"[0-9a-f]{32}")
_PAYLOAD_HASH = re.compile(r"[0-9a-f]{64}")
_IDEMPOTENCY = re.compile(r"[A-Za-z0-9._:\-]{1,128}")


from .effects import (  # noqa: E402
    FAILED,
    UNKNOWN,
    VERIFIED,
    EffectError,
    EffectLedger,
)
from .executor import ExecutionError, Executor  # noqa: E402
from .grants import Grant, GrantStore  # noqa: E402
from .operations import APPLICATION_CREDENTIALS, known  # noqa: E402
from .provenance import ProvenanceError, ProvenanceResolver  # noqa: E402


class BrokerError(Exception):
    """A request is unauthorized, malformed, or outside the fixed operations."""


@dataclass(frozen=True, slots=True)
class Peer:
    """Who is on the other end of the socket, according to the kernel."""

    pid: int
    uid: int
    gid: int

    def may(
        self,
        operation: str,
        *,
        actor: str = "",
        actor_uids: Mapping[str, int] | None = None,
    ) -> bool:
        """Whether this kernel-reported peer may run this exact operation.

        Root, on the control socket, may do everything. On an actor socket, the
        peer must actually be running as that actor's own account -- the socket
        mode keeps everyone else from connecting, and this keeps a misconfigured
        socket from being the only thing standing in the way.

        No caller can widen this. The actor comes from which socket accepted the
        connection, and the uid from the kernel; neither is anywhere in the
        request.
        """

        if not actor:
            # The control socket. Root only, and root only ever reaches it
            # because the socket itself is mode 0600.
            return self.uid == 0 and operation in OPERATIONS
        mapping = dict(actor_uids or ACTOR_UIDS)
        mapping.setdefault(RUNTIME_ACTOR, RUNTIME_UID)
        expected = mapping.get(actor)
        if expected is None or self.uid != expected:
            return False
        return operation in ACTOR_OPERATIONS


def peer_of(connection: socket.socket) -> Peer:
    """Read the connecting process's credentials from the kernel."""

    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return Peer(pid, uid, gid)


#: Whose credential this is. One shared business identity, or exactly one
#: person. The actor is part of the address, so one user's token is stored and
#: read separately from the other's and no request can reach across.
ACTORS = frozenset({"shared", *ACTOR_UIDS})


def _known(provider: object, credential_class: object) -> tuple[str, str]:
    if type(provider) is not str or provider not in CREDENTIAL_CLASSES:
        raise BrokerError("unknown provider")
    if type(credential_class) is not str or credential_class not in CREDENTIAL_CLASSES[provider]:
        raise BrokerError("unknown credential class")
    return provider, credential_class


def _actor(value: object) -> str:
    """One actor name, from root's own request on the control socket."""

    if type(value) is not str or value not in ACTORS:
        raise BrokerError("unknown actor")
    return value


def _expiry(value: object) -> datetime:
    """One timezone-aware moment from root's own request."""

    if type(value) is not str:
        raise BrokerError("malformed expiry")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise BrokerError("malformed expiry") from None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _resource(operation: Any, arguments: Mapping[str, object]) -> str:
    """What this call acts on, taken from the operation's own declared shape.

    An approval that named no resource would authorize the operation against
    anything; taking the resource from the operation table rather than from a
    caller-chosen key is what keeps "send to this contact" from becoming "send
    to any contact".
    """

    for name in operation.path_args:
        value = arguments.get(name)
        if type(value) is str and value:
            return value
    for name in ("contactId", "id", "card_id", "channel_id"):
        value = arguments.get(name)
        if type(value) is str and value:
            return value
    return ""


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
        clock: Callable[[], float] | Callable[[], datetime] = time.monotonic,
        window_seconds: float = WINDOW_SECONDS,
        executor: Executor | None = None,
        grants: GrantStore | None = None,
        effects: EffectLedger | None = None,
        provenance: ProvenanceResolver | None = None,
        actor_uids: Mapping[str, int] | None = None,
    ) -> None:
        self.store = store
        # Present in the service, absent in the pure-policy tests. When absent,
        # provider execution is refused rather than silently unavailable.
        self.executor = executor
        self.grants = grants
        self.effects = effects
        # Who is asking, established from Discord rather than from the request.
        # Absent a resolver the socket's own actor stands alone, which is the
        # right answer on a topology that really does give each actor its own
        # account and its own socket.
        self.provenance = provenance
        self.validator = validator
        # Which account each actor's worker was installed to run as. Set by the
        # root-owned service from the host's own accounts, never by a request,
        # so no caller can widen its own authority by naming a uid.
        self.actor_uids: Mapping[str, int] = dict(actor_uids or ACTOR_UIDS)
        self.clock = clock
        self.window_seconds = window_seconds
        # In memory only: a restart must not leave a usable window behind.
        self._windows: dict[str, tuple[str, str, float]] = {}

    def _monotonic(self) -> float:
        moment = self.clock()
        return moment.timestamp() if isinstance(moment, datetime) else moment

    def _wall(self) -> datetime:
        moment = self.clock()
        if isinstance(moment, datetime):
            return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
        return datetime.now(UTC)

    def handle(self, peer: Peer, request: object, *, actor: str = "") -> dict[str, object]:
        """Answer one request. Authority is settled before anything is parsed.

        `actor` is supplied by the socket layer -- it is the actor whose socket
        accepted this connection -- and never by the request.
        """

        if not isinstance(request, Mapping):
            raise BrokerError("malformed request")
        if actor and actor not in self.actor_uids and actor != RUNTIME_ACTOR:
            raise BrokerError("unknown actor")
        operation = request.get("op")
        if type(operation) is not str or operation not in OPERATIONS:
            raise BrokerError("unknown operation")
        if not peer.may(operation, actor=actor, actor_uids=self.actor_uids):
            raise BrokerError("unauthorized")
        if actor and "actor" in request:
            # Refused rather than ignored. A caller that can name an actor and
            # be silently overruled is one somebody will eventually trust.
            raise BrokerError("a request may not name an actor")
        if actor:
            handlers: dict[str, Callable[[Mapping[str, Any], str], dict[str, object]]] = {
                "status": self._status,
                "execute": self._execute,
            }
            if operation == "status" and actor == RUNTIME_ACTOR and "provenance" not in request:
                # "Does this deployment hold anything for that provider?" is a
                # fact about the deployment, not about a person, and it cannot
                # be used to act. Readiness at startup is asked before anybody
                # has said anything, so there is nothing to cite yet.
                return self._deployment_status(request)
            # On the shared runtime socket there is no actor until Discord says
            # so, and `status` is per-person, so it is settled the same way.
            resolved = self._attested(request, "" if actor == RUNTIME_ACTOR else actor)
            return handlers[operation](request, resolved)
        root_handlers: dict[str, Callable[[Mapping[str, Any]], dict[str, object]]] = {
            "open": self._open,
            "validate": self._validate,
            "commit": self._commit,
            "revoke": self._revoke,
            "grant": self._grant,
            "approve": self._approve,
            # Root asks "is this integration set up" during setup and repair.
            # It is the same deployment-level answer the runtime gets, and it
            # authorizes nothing.
            "status": self._deployment_status,
        }
        if operation not in root_handlers:
            raise BrokerError("that operation is not available on this socket")
        return root_handlers[operation](request)

    def _open(self, request: Mapping[str, Any]) -> dict[str, object]:
        provider, credential_class = _known(
            request.get("provider"), request.get("credential_class")
        )
        actor = _actor(request.get("actor", "shared"))
        self._expire()
        window = secrets.token_hex(16)
        self._windows[window] = (
            f"{provider}/{credential_class}/{actor}",
            "",
            self._monotonic() + self.window_seconds,
        )
        return {"ok": True, "state": "window open", "window": window}

    def _validate(self, request: Mapping[str, Any]) -> dict[str, object]:
        provider, credential_class = _known(
            request.get("provider"), request.get("credential_class")
        )
        material = _material(request.get("material"))
        accepted = bool(self.validator(provider, credential_class, material))
        return {"ok": accepted, "state": "validation passed" if accepted else "validation failed"}

    def _commit(self, request: Mapping[str, Any]) -> dict[str, object]:
        provider, credential_class = _known(
            request.get("provider"), request.get("credential_class")
        )
        actor = _actor(request.get("actor", "shared"))
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
        provider, credential_class = _known(
            request.get("provider"), request.get("credential_class")
        )
        actor = _actor(request.get("actor", "shared"))
        removed = self.store.drop(provider, credential_class, actor)
        return {"ok": removed, "state": "credential removed" if removed else "no credential"}

    def _grant(self, request: Mapping[str, Any]) -> dict[str, object]:
        """Write down that one actor may spend the shared business identity.

        Root only, on the control socket. Nothing on an actor socket can reach
        this, which is what makes a grant mean anything.
        """

        if self.grants is None:
            raise BrokerError("grants are not configured")
        actor = _actor(request.get("actor"))
        if actor == "shared":
            raise BrokerError("a grant is held by a person, not by the shared identity")
        provider = request.get("provider")
        if type(provider) is not str or provider not in CREDENTIAL_CLASSES:
            raise BrokerError("unknown provider")
        operations = request.get("operations")
        resources = request.get("resources", [])
        if not isinstance(operations, list) or not operations:
            raise BrokerError("a grant must name the operations it covers")
        if not isinstance(resources, list):
            raise BrokerError("malformed resources")
        expires = _expiry(request.get("expires_at"))
        grant = self.grants.put(
            Grant(
                actor=actor,
                provider=provider,
                operations=tuple(str(item) for item in operations),
                resources=tuple(str(item) for item in resources),
                expires_at=expires,
            )
        )
        return {"ok": True, "state": "granted", "grant_id": grant.grant_id}

    def _approve(self, request: Mapping[str, Any]) -> dict[str, object]:
        """Record one human approval for one exact consequential call.

        Root only. The runtime can propose all day; it cannot approve, because
        this operation is not reachable from the socket it can open.
        """

        if self.effects is None:
            raise BrokerError("the effect ledger is not configured")
        actor = _actor(request.get("actor"))
        operation = request.get("operation")
        payload_hash = request.get("payload_hash")
        if type(operation) is not str or not operation:
            raise BrokerError("an approval names one operation")
        if type(payload_hash) is not str or not _PAYLOAD_HASH.fullmatch(payload_hash):
            raise BrokerError("an approval names one payload")
        resource = request.get("resource", "")
        if type(resource) is not str:
            raise BrokerError("malformed resource")
        approval = self.effects.approve(
            actor=actor,
            operation=operation,
            payload_hash=payload_hash,
            resource=resource,
            expires_at=_expiry(request.get("expires_at")),
        )
        return {"ok": True, "state": "approved", "approval_id": approval.approval_id}

    def _deployment_status(self, request: Mapping[str, Any]) -> dict[str, object]:
        """Whether this deployment holds any credential for that provider.

        Deliberately says nothing about whose. It is the answer to "is this
        integration set up at all", which is what a startup readiness check is
        actually asking, and it authorizes nothing.
        """

        provider, credential_class = _known(
            request.get("provider"), request.get("credential_class")
        )
        for actor in sorted(ACTORS):
            if self.store.present(provider, credential_class, actor):
                return {"ok": True, "state": "credential present"}
        return {"ok": False, "state": "credential absent"}

    def _status(self, request: Mapping[str, Any], actor: str) -> dict[str, object]:
        """Whether this caller has a usable route to that provider.

        Three answers, and the difference between the last two matters to
        whoever has to fix it: this actor holds their own credential; there is
        a shared identity but nobody granted them the use of it; or there is
        nothing here at all.
        """

        provider, credential_class = _known(
            request.get("provider"), request.get("credential_class")
        )
        if self.store.present(provider, credential_class, actor):
            return {"ok": True, "state": "credential present"}
        if credential_class in APPLICATION_CREDENTIALS.get(provider, frozenset()):
            # The application key says which product is calling, not who. There
            # is no per-person version of it and no permission to grant.
            present = self.store.present(provider, credential_class, "shared")
            return {
                "ok": present,
                "state": "credential present" if present else "credential absent",
            }
        if request.get("own_only") is True:
            # "Is this one mine?" rather than "can I use one?". A shared
            # identity, granted or not, is not this person's own.
            return {"ok": False, "state": "credential absent"}
        if not self.store.present(provider, credential_class, "shared"):
            return {"ok": False, "state": "credential absent"}
        if self.grants is not None and self.grants.any_for(actor, provider) is not None:
            return {"ok": True, "state": "credential present"}
        # The shared credential exists. Its existing is not permission.
        return {"ok": False, "state": "not authorized"}

    def _execute(self, request: Mapping[str, Any], actor: str) -> dict[str, object]:
        """Run one declared provider operation as the actor on this socket.

        The caller names an operation and its arguments. It does not name a
        host, a path, a method, a header, a credential, or a person -- those
        come from the operation table, the store, and the socket.
        """

        if self.executor is None:
            raise BrokerError("provider execution is not configured")
        arguments = request.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise BrokerError("malformed arguments")
        operation_id = request.get("operation")
        try:
            operation = known(operation_id)
        except KeyError:
            raise BrokerError("unknown operation") from None

        if not operation.consequence:
            try:
                outcome = self.executor.run(operation_id, arguments, actor=actor)
            except ExecutionError as exc:
                raise BrokerError(str(exc)) from None
            return outcome.as_reply()
        return self._consequence(operation_id, operation, arguments, actor, request)

    def _attested(self, request: Mapping[str, Any], actor: str) -> str:
        """Who is really asking, when the socket alone cannot say.

        On a topology where each actor runs as its own account, the socket has
        already answered and there is nothing to add. On the pinned single
        gateway, where three profiles share one process, the socket can only
        say "the runtime" -- so the caller must cite the Discord message it is
        acting on, and this asks Discord who wrote it.

        The two answers must agree. A runtime that has its own socket and cites
        somebody else's message gets neither identity.
        """

        if self.provenance is None:
            if not actor:
                raise BrokerError("this deployment cannot confirm who is asking")
            return actor
        if actor and "provenance" not in request:
            # A worker with its own account and its own socket has already been
            # identified by the kernel; a citation is welcome but not required.
            return actor
        try:
            attested = self.provenance.resolve(request.get("provenance"))
        except ProvenanceError as exc:
            raise BrokerError(str(exc)) from None
        if actor and attested.actor != actor:
            raise BrokerError("that message is not from the user this socket belongs to")
        return attested.actor

    def _consequence(
        self,
        operation_id: object,
        operation: Any,
        arguments: Mapping[str, object],
        actor: str,
        request: Mapping[str, Any],
    ) -> dict[str, object]:
        """A call somebody has to have approved, made at most once.

        Everything is settled before the provider is touched: the deadline has
        not passed, an approval exists for this exact actor, operation, payload
        and resource, and this idempotency key has not already been spent. The
        effect row is written first, so a process that dies mid-flight leaves
        `unknown` rather than leaving nothing.
        """

        if self.effects is None:
            raise BrokerError("the effect ledger is not configured")
        now = self._wall()
        deadline = _expiry(request.get("deadline"))
        if deadline <= now:
            raise BrokerError("this request is past its deadline")
        idempotency_key = request.get("idempotency_key")
        if type(idempotency_key) is not str or not _IDEMPOTENCY.fullmatch(idempotency_key):
            raise BrokerError("a consequence needs an idempotency key")
        approval_id = request.get("approval_id")
        if type(approval_id) is not str or not approval_id:
            raise BrokerError("this operation needs an approval")
        payload_hash = self.effects.payload_hash(arguments)
        resource = _resource(operation, arguments)

        held = self.effects.by_idempotency(actor, idempotency_key)
        if held is not None:
            if held.state == UNKNOWN:
                # Nobody could see what became of the first attempt. Repeating
                # it is how one message becomes two.
                raise BrokerError("an earlier attempt is unresolved; reconcile before retrying")
            return {"ok": held.state == VERIFIED, "state": held.state, "effect_id": held.effect_id}

        try:
            self.effects.claim(
                approval_id,
                actor=actor,
                operation=str(operation_id),
                payload_hash=payload_hash,
                resource=resource,
            )
        except EffectError as exc:
            raise BrokerError(str(exc)) from None

        effect, mine = self.effects.begin(
            actor=actor,
            operation=str(operation_id),
            payload_hash=payload_hash,
            resource=resource,
            idempotency_key=idempotency_key,
            approval_id=approval_id,
        )
        if not mine:  # pragma: no cover - the lookup above already returned
            return {
                "ok": effect.state == VERIFIED,
                "state": effect.state,
                "effect_id": effect.effect_id,
            }
        try:
            outcome = self.executor.run(  # type: ignore[union-attr]
                operation_id, arguments, actor=actor
            )
        except ExecutionError as exc:
            # Left as unknown: a transport that failed may still have delivered.
            self.effects.settle(effect.effect_id, UNKNOWN, str(exc))
            raise
        state = VERIFIED if outcome.ok else FAILED
        self.effects.settle(effect.effect_id, state, "")
        return {**outcome.as_reply(), "effect_id": effect.effect_id, "state": state}

    def _expire(self) -> None:
        now = self._monotonic()
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


def serve_once(broker: Broker, connection: socket.socket, *, actor: str = "") -> None:
    """Handle exactly one request on one connection, then close it.

    `actor` belongs to the listening socket, not to the request. It is how the
    caller's identity reaches the broker without the caller ever stating it.
    """

    try:
        peer = peer_of(connection)
        reply = broker.handle(peer, read_frame(connection), actor=actor)
    except (BrokerError, ExecutionError) as exc:
        # The reason is a fixed word from our own code, never request content.
        reply = {"ok": False, "state": str(exc)}
    except Exception:
        reply = {"ok": False, "state": "unavailable"}
    with suppress(OSError):
        connection.sendall(json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n")


def bind_control_socket(path: str | os.PathLike[str] = SOCKET_PATH) -> socket.socket:
    """The root-only socket. Mode 0600: nothing else can even connect."""

    target = _prepare(path, mode=0o755)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(0o177)
    try:
        server.bind(str(target))
    finally:
        os.umask(previous)
    if os.getuid() == 0:
        os.chown(target, 0, 0)
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    server.listen(8)
    return server


def bind_actor_socket(
    actor: str, *, group: int, path: str | os.PathLike[str] = ""
) -> socket.socket:
    """One actor's own socket, owned by that actor's own group.

    Mode 0660 with a per-actor group is the first lock: a worker running as the
    employee account is not in the main operator's group, so it cannot open
    that socket at all. The peer check inside the broker is the second.
    """

    if actor not in ACTOR_SOCKETS:
        raise BrokerError("unknown actor")
    target = _prepare(path or ACTOR_SOCKETS[actor], mode=0o755)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(0o117)
    try:
        server.bind(str(target))
    finally:
        os.umask(previous)
    if os.getuid() == 0:
        os.chown(target, 0, group)
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
    server.listen(8)
    return server


def _prepare(path: str | os.PathLike[str], *, mode: int) -> Path:
    target = Path(path)
    target.parent.mkdir(mode=mode, parents=True, exist_ok=True)
    if target.is_symlink():
        raise BrokerError("socket path is unsafe")
    if target.exists():
        target.unlink()
    return target


def bind_socket(path: str | os.PathLike[str], *, group: int) -> socket.socket:
    """Backwards-compatible binder for one socket with a named group."""

    target = _prepare(path, mode=0o755)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(0o117)
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
    broker: Broker,
    server: socket.socket,
    *,
    actor: str = "",
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Serve one socket. Everything accepted here belongs to one actor."""

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
            serve_once(broker, connection, actor=actor)


def serve_all(
    broker: Broker,
    sockets: Mapping[str, socket.socket],
    *,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Serve the control socket and every actor socket from one process.

    One process, several listening sockets, and the actor decided entirely by
    which of them accepted a connection. Nothing in the loop below can be
    talked into a different answer.
    """

    for server in sockets.values():
        server.settimeout(0.2)
    while not should_stop():
        for actor, server in sockets.items():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(5.0)
                serve_once(broker, connection, actor="" if actor == "control" else actor)
