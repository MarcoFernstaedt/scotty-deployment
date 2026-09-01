"""Deciding who is asking, from Discord rather than from the asker.

The hole this closes was the whole authority model: the broker read the actor
out of the request. Anything running as the runtime account could say
`"actor": "employee"` and be believed.

The obvious fix is to take identity from the kernel, and where the topology
allows it that is exactly what happens -- each actor has its own socket, owned
by its own group, and the actor is whichever socket accepted the connection.
But the pinned runtime is one Discord gateway process serving three profiles,
and one process cannot be three accounts. On that topology a kernel check can
only ever say "the runtime", which is what it said before.

So identity comes from the provider instead. A caller does not name an actor;
it cites the Discord message it is acting on. The broker holds the bot token --
the runtime does not -- and asks Discord who wrote that message and where. The
actor is then resolved from root's own channel and user mapping, on this side
of the socket.

What that buys is precise, and worth stating rather than overselling: the
runtime cannot claim to be a user who has not spoken to it, cannot act as the
maintainer from a client channel, and cannot act at all in a channel this
deployment does not serve. It cannot forge the evidence, because forging it
would mean posting to Discord as somebody else, and the token that could do
that is not in the container.

Nothing in here trusts the message body. Only the author, the channel, and the
fact that Discord agrees the message exists.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

DISCORD_API = "https://discord.com/api/v10"

#: How long one attestation stays good for. A message is evidence of who spoke,
#: not a standing authority, so it goes stale quickly.
ATTESTATION_SECONDS = 300

MAX_RESPONSE_BYTES = 64 * 1024
TIMEOUT_SECONDS = 10.0

_SNOWFLAKE_MIN = 17
_SNOWFLAKE_MAX = 20


class ProvenanceError(RuntimeError):
    """The citation does not establish who is asking, so nothing is done."""


def _snowflake(value: object, label: str) -> str:
    if type(value) is not str or not value.isdigit():
        raise ProvenanceError(f"malformed {label}")
    if not _SNOWFLAKE_MIN <= len(value) <= _SNOWFLAKE_MAX:
        raise ProvenanceError(f"malformed {label}")
    return value


@dataclass(frozen=True, slots=True)
class Route:
    """One channel this deployment serves, and who it belongs to."""

    channel_id: str
    user_id: str
    actor: str
    guild_id: str = ""
    shared: bool = False


@dataclass(frozen=True, slots=True)
class Attestation:
    """Who Discord says wrote the cited message, resolved to an actor."""

    actor: str
    channel_id: str
    user_id: str
    message_id: str
    at: float


Fetcher = Callable[[str, Mapping[str, str]], tuple[int, object]]


def _fetch(url: str, headers: Mapping[str, str]) -> tuple[int, object]:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise ProvenanceError("Discord could not be reached to confirm who is asking") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProvenanceError("Discord returned more than one message could be")
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError("Discord's answer was not readable") from exc


class ProvenanceResolver:
    """Turns a cited Discord message into the actor it actually came from."""

    def __init__(
        self,
        routes: tuple[Route, ...],
        token_reader: Callable[[], str | None],
        *,
        fetch: Fetcher = _fetch,
        clock: Callable[[], float] = time.monotonic,
        window: float = ATTESTATION_SECONDS,
    ) -> None:
        self.routes = routes
        # A callable rather than the token itself, so this object can be built,
        # inspected and logged without ever holding the material.
        self.token_reader = token_reader
        self.fetch = fetch
        self.clock = clock
        self.window = window
        self._seen: dict[str, Attestation] = {}

    def __repr__(self) -> str:
        return f"ProvenanceResolver(routes={len(self.routes)})"

    def _route(self, channel_id: str) -> Route | None:
        for route in self.routes:
            if route.channel_id == channel_id:
                return route
        return None

    def resolve(self, citation: object) -> Attestation:
        """Ask Discord who wrote this, and say which actor that is.

        Every failure here is the same kind of failure: we could not establish
        who is asking. None of them fall back to a default actor, because a
        default actor is the bug this exists to remove.
        """

        if not isinstance(citation, Mapping):
            raise ProvenanceError("this request cites no message")
        channel_id = _snowflake(citation.get("channel_id"), "channel id")
        message_id = _snowflake(citation.get("message_id"), "message id")

        route = self._route(channel_id)
        if route is None:
            # A channel this deployment does not serve. Not an actor, not an
            # error to explain in detail -- there is simply nobody here.
            raise ProvenanceError("that channel is not one this deployment serves")

        cached = self._seen.get(message_id)
        now = self.clock()
        if cached is not None and now - cached.at < self.window:
            if cached.channel_id != channel_id:
                raise ProvenanceError("that message is not in that channel")
            return cached

        token = self.token_reader()
        if not token:
            raise ProvenanceError("this deployment cannot confirm who is asking")
        status, body = self.fetch(
            f"{DISCORD_API}/channels/{urllib.parse.quote(channel_id)}"
            f"/messages/{urllib.parse.quote(message_id)}",
            {"Authorization": f"Bot {token}", "Accept": "application/json"},
        )
        if status == 404:
            raise ProvenanceError("that message is not there")
        if status != 200 or not isinstance(body, Mapping):
            raise ProvenanceError("Discord did not confirm who is asking")

        observed_channel = body.get("channel_id")
        author = body.get("author")
        if type(observed_channel) is str and observed_channel != channel_id:
            # The message exists, in a different channel. Somebody is citing a
            # message from somewhere they were not.
            raise ProvenanceError("that message is not in that channel")
        if not isinstance(author, Mapping):
            raise ProvenanceError("Discord did not say who wrote that")
        if author.get("bot") is True:
            # Scotty's own messages are not a person asking for something.
            raise ProvenanceError("a bot's own message is not somebody asking")
        user_id = author.get("id")
        if type(user_id) is not str or not user_id:
            raise ProvenanceError("Discord did not say who wrote that")

        if not route.shared and user_id != route.user_id:
            # A private channel belongs to exactly one person. Someone else
            # speaking in it is not that person.
            raise ProvenanceError("that message is not from the person this channel belongs to")
        actor = route.actor if not route.shared else self._actor_of(user_id)
        if not actor:
            raise ProvenanceError("that message is not from somebody this deployment serves")

        attestation = Attestation(actor, channel_id, user_id, message_id, now)
        self._remember(attestation)
        return attestation

    def _actor_of(self, user_id: str) -> str:
        """Which actor a person is, in a channel more than one of them uses."""

        for route in self.routes:
            if route.user_id == user_id and not route.shared:
                return route.actor
        return ""

    def _remember(self, attestation: Attestation) -> None:
        now = attestation.at
        for message_id, held in list(self._seen.items()):
            if now - held.at >= self.window:
                del self._seen[message_id]
        # Bounded: a runtime that cited thousands of messages must not be able
        # to grow this without limit.
        while len(self._seen) >= 512:
            oldest = min(self._seen, key=lambda key: self._seen[key].at)
            del self._seen[oldest]
        self._seen[attestation.message_id] = attestation


def routes_from(mapping: object) -> tuple[Route, ...]:
    """Root's own channel map, read from the privileged configuration file."""

    if not isinstance(mapping, list):
        raise ProvenanceError("the route map is malformed")
    routes: list[Route] = []
    for entry in mapping:
        if not isinstance(entry, Mapping):
            raise ProvenanceError("the route map is malformed")
        routes.append(
            Route(
                channel_id=_snowflake(entry.get("channel_id"), "channel id"),
                user_id=_snowflake(entry.get("user_id"), "user id"),
                actor=str(entry.get("actor", "")),
                guild_id=str(entry.get("guild_id", "")),
                shared=entry.get("shared") is True,
            )
        )
    return tuple(routes)


__all__ = [
    "ATTESTATION_SECONDS",
    "DISCORD_API",
    "Attestation",
    "ProvenanceError",
    "ProvenanceResolver",
    "Route",
    "routes_from",
]
