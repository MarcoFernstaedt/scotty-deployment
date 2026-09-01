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
from typing import Any

from .operations import (
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


def _open(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(  # noqa: S310 - scheme is pinned to https above
            request, timeout=timeout, context=context
        ) as response:
            return int(response.status), response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(MAX_RESPONSE_BYTES + 1)


class Executor:
    """Runs declared operations with credentials the caller never sees."""

    def __init__(self, store: Any, *, opener: Opener = _open):
        self.store = store
        self.opener = opener

    def _credentials(self, provider: str, actor: str) -> dict[str, str]:
        """This actor's own credential, or the shared identity, never another's.

        There is no third case. A user with neither gets an error naming the
        provider, and the request is not made — falling back to whatever
        happens to be stored would be exactly the cross-account leak this
        boundary exists to prevent.
        """

        resolved: dict[str, str] = {}
        for credential_class, placement in PROVIDER_CREDENTIALS[provider]:
            material = self.store.read(provider, credential_class, actor)
            shared = False
            if material is None:
                material = self.store.read(provider, credential_class, "shared")
                shared = True
            if material is None:
                raise ExecutionError(f"{provider} is not connected for this actor")
            resolved[placement] = material
            del shared
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
        credentials = self._credentials(operation.provider, actor)

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
