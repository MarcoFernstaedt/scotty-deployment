"""Performing one declared provider operation, on the privileged side.

This is the only code in the deployment that puts a provider credential onto a
request. It runs in the root-owned broker, outside every mount and every process
the model can reach, and it will only ever build a request that one of the
declared operations describes.

Nothing here accepts a URL. The host comes from the provider table, the path
from the operation's own template, and every substituted value has already
matched a declared shape. An argument the operation did not declare is refused
rather than dropped, because silently ignoring an argument is how a caller ends
up believing it asked for something it did not.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import Message
from typing import IO, Any

from .operations import (
    APPLICATION_CREDENTIALS,
    AUTH_BEARER,
    AUTH_HEADER,
    AUTH_QUERY,
    PROVIDER_BASES,
    PROVIDER_CREDENTIALS,
    SHAPES,
    Operation,
    known,
)

MAX_RESPONSE_BYTES = 1_048_576
MAX_REQUEST_BYTES = 65_536
DEFAULT_TIMEOUT = 20.0
MAX_TIMEOUT = 60.0


class ExecutionError(RuntimeError):
    """The request was not made, and the reason names no credential."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """What came back, bounded and already stripped of anything sensitive."""

    ok: bool
    status: int
    body: object
    state: str = ""

    def as_reply(self) -> dict[str, object]:
        return {"ok": self.ok, "status": self.status, "body": self.body, "state": self.state}


def _checked(operation: Operation, arguments: Mapping[str, object]) -> dict[str, str]:
    """Validate every argument against the shape the operation declared."""

    shapes = operation.argument_shapes()
    unknown = set(arguments) - set(shapes)
    if unknown:
        raise ExecutionError(f"unsupported argument: {sorted(unknown)[0]}")
    missing = [name for name in operation.required if not arguments.get(name)]
    if missing:
        raise ExecutionError(f"missing argument: {missing[0]}")
    checked: dict[str, str] = {}
    for name, value in arguments.items():
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int | float):
            rendered = str(value)
        elif type(value) is str:
            rendered = value
        else:
            raise ExecutionError(f"argument {name} is not a simple value")
        pattern = SHAPES[shapes[name]]
        if not pattern.fullmatch(rendered):
            raise ExecutionError(f"argument {name} is malformed")
        checked[name] = rendered
    return checked


def _url(operation: Operation, checked: Mapping[str, str]) -> str:
    """Build the URL from the table, never from a caller-supplied address."""

    base = PROVIDER_BASES[operation.provider]
    path = operation.path
    for name in operation.path_args:
        placeholder = "{" + name + "}"
        if placeholder not in path:  # pragma: no cover - table is self-consistent
            raise ExecutionError("operation template is inconsistent")
        # The value already matched its shape, so it carries no separator that
        # could escape the path segment it belongs to.
        path = path.replace(placeholder, urllib.parse.quote(checked[name], safe="/"))
    if "{" in path or "}" in path:
        raise ExecutionError("operation template is incomplete")
    target = f"{base}{path}"
    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme != "https" or parsed.netloc != urllib.parse.urlsplit(base).netloc:
        raise ExecutionError("operation resolved outside its provider")
    return target


def _resource_of(operation: Any, checked: Mapping[str, object]) -> str:
    """What this call acts on, from the operation's own declared shape."""

    for name in operation.path_args:
        value = checked.get(name)
        if type(value) is str and value:
            return value
    for name in ("contactId", "id", "card_id", "channel_id"):
        value = checked.get(name)
        if type(value) is str and value:
            return value
    return ""


def _project(body: object, depth: int = 0) -> object:
    """Bound what comes back so a provider cannot flood the runtime."""

    if depth > 8:
        return "…"
    if isinstance(body, Mapping):
        return {
            str(key): _project(value, depth + 1)
            for index, (key, value) in enumerate(body.items())
            if index < 200
        }
    if isinstance(body, list):
        return [_project(item, depth + 1) for item in body[:200]]
    if type(body) is str:
        return body[:4000]
    if isinstance(body, bool | int | float) or body is None:
        return body
    return str(body)[:200]


Opener = Callable[[urllib.request.Request, float], tuple[int, bytes]]


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is a provider telling us to go somewhere else. We do not.

    Following one would mean sending a credential to a host that is not in the
    operation table -- the one thing the table exists to prevent. Trello, GHL
    and RentCast do not redirect their API endpoints, so a redirect here is
    either a misconfiguration or somebody moving the target.
    """

    def redirect_request(  # noqa: PLR0913 - urllib's own signature
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        raise urllib.error.HTTPError(
            req.full_url, code, "this provider redirected; refusing to follow", headers, fp
        )


def _opener() -> urllib.request.OpenerDirector:
    """An opener that goes exactly where it was told, and nowhere else.

    Assembled by hand rather than taken from `build_opener`, because that one
    installs handlers this process must not have: a ProxyHandler that reads
    `http_proxy` and friends out of the environment, and handlers for `http`,
    `ftp`, `file` and `data`. In a process holding every provider credential,
    each of those is a way for a mistyped or manipulated target to become a
    request somewhere it should never go.

    What is left is https, and a redirect handler that refuses.
    """

    director = urllib.request.OpenerDirector()
    director.add_handler(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    director.add_handler(NoRedirects())
    director.add_handler(urllib.request.HTTPDefaultErrorHandler())
    director.add_handler(urllib.request.HTTPErrorProcessor())
    return director


def _open(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    opener = _opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(MAX_RESPONSE_BYTES + 1)


#: What a call actually sends, so the transport can be swapped for a recorder
#: in a test without the test having to imitate urllib.
Sender = Callable[..., tuple[int, object]]


class Executor:
    """Runs declared operations with credentials the caller never sees."""

    def __init__(
        self,
        store: Any,
        grants: Any = None,
        *,
        opener: Opener = _open,
        send: Sender | None = None,
    ):
        self.store = store
        # Grants say whether an actor may spend the shared business identity.
        # Absent a grant store, the shared identity is simply unreachable.
        self.grants = grants
        self.opener = opener
        self.send = send

    def _credentials(
        self, provider: str, actor: str, operation_id: str, resource: str
    ) -> dict[str, str]:
        """This actor's own credential, or one they were explicitly granted.

        The version this replaces fell back to the shared identity whenever an
        actor had none of their own. Nothing anywhere said they were allowed to
        act as the business -- the shared token merely existed, and existing was
        treated as permission. Now the fallback needs a grant that names this
        actor, this provider, this operation and this resource, and the answer
        without one is `not authorized`, which is a different thing from `not
        connected` and reads differently to whoever has to fix it.
        """

        resolved: dict[str, str] = {}
        application = APPLICATION_CREDENTIALS.get(provider, frozenset())
        for credential_class, placement in PROVIDER_CREDENTIALS[provider]:
            material = self.store.read(provider, credential_class, actor)
            if material is None and credential_class in application:
                # The application key identifies this product, not a person.
                material = self.store.read(provider, credential_class, "shared")
            if material is None:
                if credential_class in application:
                    raise ExecutionError(f"{provider} is not connected for this user")
                if self.grants is None:
                    raise ExecutionError(f"{provider} is not connected for this user")
                if self.grants.find(actor, provider, operation_id, resource) is None:
                    if self.store.read(provider, credential_class, "shared") is None:
                        raise ExecutionError(f"{provider} is not connected for this user")
                    raise ExecutionError(
                        f"this user is not authorized to use the shared {provider} identity "
                        "for that operation"
                    )
                material = self.store.read(provider, credential_class, "shared")
            if material is None:
                raise ExecutionError(f"{provider} is not connected for this user")
            resolved[placement] = material
        return resolved

    def run(
        self,
        operation_id: object,
        arguments: Mapping[str, object],
        *,
        actor: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Outcome:
        """Execute one declared operation and return a bounded projection."""

        try:
            operation = known(operation_id)
        except KeyError as exc:
            raise ExecutionError("unknown operation") from exc
        if not isinstance(arguments, Mapping):
            raise ExecutionError("arguments must be an object")
        checked = _checked(operation, arguments)
        credentials = self._credentials(
            operation.provider, actor, str(operation_id), _resource_of(operation, checked)
        )

        query = {name: checked[name] for name in operation.query_args if name in checked}
        headers = {"Accept": "application/json", "User-Agent": "scotty-broker/1"}
        body: dict[str, str] | None = None
        if operation.body_args:
            body = {name: checked[name] for name in operation.body_args if name in checked}

        if operation.auth == AUTH_QUERY:
            query.update(credentials)
        elif operation.auth == AUTH_BEARER:
            for placement, material in credentials.items():
                headers[placement] = f"Bearer {material}"
        elif operation.auth == AUTH_HEADER:
            headers.update(credentials)
        else:  # pragma: no cover - the table only declares the three above
            raise ExecutionError("unsupported authorization placement")

        url = _url(operation, checked)
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            if len(payload) > MAX_REQUEST_BYTES:
                raise ExecutionError("request body exceeds the limit")
            headers["Content-Type"] = "application/json"

        if self.send is not None:
            # A recorder rather than the network. Everything above this line --
            # the shapes, the credential resolution, the URL construction -- has
            # already happened, so what a test sees is what would go out.
            status, parsed = self.send(
                operation.method, url, headers=headers, body=body, timeout=timeout
            )
            return Outcome(
                ok=200 <= status < 300,
                status=status,
                body=_project(parsed),
                state="" if 200 <= status < 300 else f"provider returned HTTP {status}",
            )
        request = urllib.request.Request(  # noqa: S310 - https enforced in _url
            url, data=payload, headers=headers, method=operation.method
        )
        try:
            status, raw = self.opener(request, min(max(timeout, 1.0), MAX_TIMEOUT))
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            # The provider may or may not have acted. The caller is told that
            # rather than being told it failed, so nothing is blindly retried.
            raise ExecutionError("provider outcome unknown") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ExecutionError("provider response exceeds the limit")
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = {}
        return Outcome(
            ok=200 <= status < 300,
            status=status,
            body=_project(parsed),
            state="" if 200 <= status < 300 else f"provider returned HTTP {status}",
        )


__all__ = ["DEFAULT_TIMEOUT", "ExecutionError", "Executor", "Outcome"]
