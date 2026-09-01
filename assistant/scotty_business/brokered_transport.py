"""A transport that cannot make a request of its own.

The provider adapters build the URL they want, as they always did. This
transport does not send it. It recognises the request as one of the declared
provider operations, converts it into that operation and its arguments, and
hands it to the broker — which holds the credential, rebuilds the request from
its own table, and makes the call.

So there are two independent checks and the important one is not here. A
request this shim does not recognise is refused before it leaves the container;
one it does recognise is still re-validated on the privileged side against the
authoritative table. A container that lied about the mapping would gain
nothing, because nothing here is trusted over there.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .adapters.http import AmbiguousEffectError, HttpResponse, ProviderError

#: Asked before every request: may this happen at all? It raises when not, so a
#: caller cannot proceed by ignoring a return value.
Guard = Callable[[str, str], None]

#: Told after every request: did it work? Drives the provider breakers without
#: anybody at a call site having to remember.
Record = Callable[[str, bool], None]

#: The provider hosts the adapters may name at all.
_HOSTS: Mapping[str, str] = {
    "api.trello.com": "trello",
    "services.leadconnectorhq.com": "ghl",
    "api.rentcast.io": "rentcast",
}


@dataclass(frozen=True, slots=True)
class _Route:
    """One recognised (method, path) shape and the operation it means."""

    method: str
    pattern: re.Pattern[str]
    operation: str
    #: Path capture groups, in order, and the argument each one supplies.
    captures: tuple[str, ...] = ()


_ROUTES: tuple[_Route, ...] = (
    _Route("GET", re.compile(r"^/1/cards/([A-Za-z0-9_-]+)$"), "trello.get_card", ("card_id",)),
    _Route(
        "GET",
        re.compile(r"^/1/boards/([A-Za-z0-9_-]+)/cards$"),
        "trello.list_board_cards",
        ("board_id",),
    ),
    _Route(
        "GET",
        re.compile(r"^/1/boards/([A-Za-z0-9_-]+)/lists$"),
        "trello.list_board_lists",
        ("board_id",),
    ),
    _Route(
        "GET",
        re.compile(r"^/1/lists/([A-Za-z0-9_-]+)/cards$"),
        "trello.list_cards_in_list",
        ("list_id",),
    ),
    _Route("POST", re.compile(r"^/1/cards$"), "trello.create_card"),
    _Route("PUT", re.compile(r"^/1/cards/([A-Za-z0-9_-]+)$"), "trello.update_card", ("card_id",)),
    _Route(
        "PUT",
        re.compile(r"^/1/cards/([A-Za-z0-9_-]+)/customField/([A-Za-z0-9_-]+)/item$"),
        "trello.set_custom_field",
        ("card_id", "field_id"),
    ),
    _Route("GET", re.compile(r"^/contacts/([A-Za-z0-9_-]+)$"), "ghl.get_contact", ("contact_id",)),
    _Route("GET", re.compile(r"^/conversations/search$"), "ghl.search_conversations"),
    _Route(
        "GET",
        re.compile(r"^/conversations/([A-Za-z0-9_-]+)/messages/([A-Za-z0-9_-]+)$"),
        "ghl.get_message",
        ("conversation_id", "message_id"),
    ),
    _Route("POST", re.compile(r"^/conversations/messages$"), "ghl.send_sms"),
    _Route("GET", re.compile(r"^(/v1/[A-Za-z0-9/_-]+)$"), "rentcast.fetch", ("endpoint",)),
)

#: Query names the adapters attach for authentication. They no longer carry a
#: value the container has, and the broker supplies its own, so they are
#: dropped rather than forwarded.
_CREDENTIAL_QUERY = frozenset({"key", "token"})


class BrokeredTransport:
    """Sends declared operations to the broker instead of making requests.

    There is no actor here any more. This side used to name one, and the broker
    believed it; now it carries the Discord message the work is being done for,
    and the broker asks Discord who wrote it. A transport built without a
    citation can still be built -- it simply cannot act, which is the right
    answer for work nobody asked for.
    """

    def __init__(
        self,
        broker: object,
        *,
        provenance: Mapping[str, object] | None = None,
        guard: Guard | None = None,
        record: Record | None = None,
    ):
        self.broker = broker
        self.provenance = dict(provenance) if provenance else None
        # Every request this transport makes passes the guard first and reports
        # its outcome after. Putting both here rather than at each call site is
        # the point: a call site that forgets is a limit that does not exist.
        self.guard = guard
        self.record = record

    def _route(self, method: str, path: str) -> _Route | None:
        for route in _ROUTES:
            if route.method == method and route.pattern.fullmatch(path):
                return route
        return None

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        attachment: object | None = None,
        text: bool = False,
    ) -> HttpResponse:
        import urllib.parse

        del headers
        if attachment is not None or text:
            raise ProviderError("this provider path is not available through the broker")
        parsed = urllib.parse.urlsplit(url)
        provider = _HOSTS.get(parsed.netloc)
        if parsed.scheme != "https" or provider is None:
            raise ProviderError("that destination is not a configured provider")
        route = self._route(method.upper(), parsed.path)
        if route is None:
            raise ProviderError("that provider operation is not one Scotty performs")

        arguments: dict[str, object] = {}
        matched = route.pattern.fullmatch(parsed.path)
        assert matched is not None  # noqa: S101 - matched in _route above
        for name, value in zip(route.captures, matched.groups(), strict=False):
            arguments[name] = value
        for name, value in (query or {}).items():
            if name in _CREDENTIAL_QUERY:
                continue
            arguments[name] = value
        for name, value in (json_body or {}).items():
            arguments[name] = value

        provider = route.operation.split(".", 1)[0]
        if self.guard is not None:
            # Refused before anything leaves. A budget checked after the call
            # is a count of what already happened.
            self.guard(provider, route.operation)
        try:
            reply = self.broker.execute(  # type: ignore[attr-defined]
                route.operation, arguments, provenance=self.provenance
            )
        except Exception:
            self._record(provider, ok=False)
            raise
        if reply is None:
            # The broker did not answer, so what the provider did is unknown.
            self._record(provider, ok=False)
            raise AmbiguousEffectError("provider outcome is unknown; reconcile before any retry")
        status = reply.get("status")
        if not isinstance(status, int):
            self._record(provider, ok=False)
            raise ProviderError(str(reply.get("state") or "provider request was refused"))
        self._record(provider, ok=200 <= status < 300)
        return HttpResponse(status=status, headers={}, body=reply.get("body"))

    def _record(self, provider: str, *, ok: bool) -> None:
        if self.record is not None:
            self.record(provider, ok)


__all__ = ["BrokeredTransport"]
