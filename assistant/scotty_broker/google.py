"""Google's long-lived credentials, held where the runtime cannot reach them.

The refresh token and the client secret were written into the container's own
state directory, owned by the account the runtime runs as. Mode 0600 separates
users; it does not separate a plugin, a tool call and a maintainer session that
all run as the same one. A file read in the wrong session was one step from an
OAuth grant that outlives every password change.

So the exchange happens here instead. Root holds the client secret and each
person's refresh token, and answers one question for the runtime: "an access
token for this person, for exactly these scopes." What crosses back is good for
an hour and cannot mint another. That is a smaller exposure rather than none,
and it is named as such: until every Google call is a declared operation in the
table next door, the container still handles a bearer token.

Rotation stays on this side too. Google may hand back a new refresh token on
any exchange, and a runtime that had to store it would be a runtime that had to
hold it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint, not a secret

#: Mint again this many seconds before Google stops accepting the token, so a
#: call that starts just inside the window does not finish just outside it.
REFRESH_SKEW_SECONDS = 120

#: The most scopes one deployment asks for. A bound, so a malformed request
#: cannot turn into an unbounded form body.
MAX_SCOPES = 32

#: Google returns the OpenID shorthand in expanded form, so the configured
#: shorthand and the granted URL are the same authorization.
_SCOPE_ALIASES: Mapping[str, str] = {
    "email": "https://www.googleapis.com/auth/userinfo.email",
    "profile": "https://www.googleapis.com/auth/userinfo.profile",
}


class GoogleTokenError(Exception):
    """No token can be minted, and nothing stored has been changed."""


def canonical_scopes(scopes: Iterable[str]) -> frozenset[str]:
    """Compare scope sets by authorization rather than by spelling."""

    return frozenset(_SCOPE_ALIASES.get(scope, scope) for scope in scopes)


@dataclass(frozen=True, slots=True)
class MintedToken:
    """One short-lived access token, and when it stops working.

    Deliberately holds nothing else. There is no field here that could carry a
    refresh token or a client secret, so no future caller can be handed one by
    accident, and the redacted repr keeps the bearer out of a traceback.
    """

    access_token: str = field(repr=False)
    expires_at: int
    scopes: tuple[str, ...]

    def __repr__(self) -> str:
        return f"MintedToken(expires_at={self.expires_at}, scopes={len(self.scopes)})"

    def usable(self, now: int) -> bool:
        return self.expires_at > now + REFRESH_SKEW_SECONDS


class Holder:
    """What this needs from the credential store, and nothing more."""

    def read(self, provider: str, credential_class: str, actor: str = "shared") -> str | None:
        raise NotImplementedError  # pragma: no cover - structural

    def put(
        self, provider: str, credential_class: str, material: str, actor: str = "shared"
    ) -> None:
        raise NotImplementedError  # pragma: no cover - structural


#: A form POST to the pinned Google token endpoint. Injected so tests never
#: open a socket and so the service can hand in its own bounded opener.
TokenExchange = Callable[[str, Mapping[str, str]], Mapping[str, object]]


def _post_form(url: str, fields: Mapping[str, str]) -> Mapping[str, object]:
    """Exchange form fields at the pinned endpoint and return the parsed body."""

    if not url.startswith("https://oauth2.googleapis.com/"):  # pragma: no cover - pinned above
        raise GoogleTokenError("the Google token endpoint is not the expected one")
    request = urllib.request.Request(  # noqa: S310 - the scheme and host are checked above
        url, data=urllib.parse.urlencode(dict(fields)).encode(), method="POST"
    )
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = json.loads(response.read(65_537).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        raise GoogleTokenError("the Google token exchange failed") from exc
    if not isinstance(body, Mapping):
        raise GoogleTokenError("the Google token response is malformed")
    return body


class GoogleTokenMinter:
    """Turns stored consent into a short-lived token, on the privileged side.

    One instance serves every actor; whose consent gets spent is decided by the
    actor the broker resolved, never by anything in a request. There is no
    method here that returns a refresh token or a client secret, and the
    minted-token cache is in memory, so a restart re-mints rather than leaving
    a usable bearer on disk.
    """

    def __init__(
        self,
        store: Holder,
        *,
        exchange: TokenExchange = _post_form,
        clock: Callable[[], float] = time.time,
        token_uri: str = GOOGLE_TOKEN_URI,
    ) -> None:
        self.store = store
        self.exchange = exchange
        self.clock = clock
        self.token_uri = token_uri
        self._minted: dict[tuple[str, frozenset[str]], MintedToken] = {}

    def __repr__(self) -> str:
        return f"GoogleTokenMinter(minted={len(self._minted)})"

    def connected(self, actor: str) -> bool:
        """Whether this person's own consent is held. Never says what it is."""

        return bool(self.store.read("google", "refresh_token", actor))

    def access_token(self, actor: str, scopes: Sequence[str]) -> MintedToken:
        """One usable access token for exactly this person and these scopes."""

        wanted = self._scopes(scopes)
        key = (actor, canonical_scopes(wanted))
        now = int(self.clock())
        held = self._minted.get(key)
        if held is not None and held.usable(now):
            return held

        # This person's own consent. There is no shared fallback: one user's
        # refresh token is stored under their own actor and no other lookup
        # reaches it, so an unconnected actor is refused rather than served
        # from somebody else's grant.
        refresh = self.store.read("google", "refresh_token", actor)
        if not refresh:
            raise GoogleTokenError("no Google consent is held for this user")
        client_id = self.store.read("google", "client_id", "shared")
        client_secret = self.store.read("google", "client_secret", "shared")
        if not client_id or not client_secret:
            raise GoogleTokenError("no Google OAuth client is held by this deployment")

        body = self.exchange(
            self.token_uri,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
        )
        minted = self._accept(body, wanted, now)
        # Only once the response is known good. A partial answer must leave the
        # stored consent exactly as it was, or a transient provider fault turns
        # into a grant nobody can use again.
        rotated = body.get("refresh_token")
        if type(rotated) is str and rotated and rotated != refresh:
            self.store.put("google", "refresh_token", rotated, actor)
        self._minted[key] = minted
        return minted

    def forget(self, actor: str) -> None:
        """Drop every cached token for one person, on revoke or reconnect."""

        for key in [held for held in self._minted if held[0] == actor]:
            del self._minted[key]

    def _scopes(self, scopes: Sequence[str]) -> tuple[str, ...]:
        if not isinstance(scopes, list | tuple) or not scopes:
            raise GoogleTokenError("a token is minted for a named scope set")
        if len(scopes) > MAX_SCOPES:
            raise GoogleTokenError("that is more scopes than this deployment asks for")
        cleaned: list[str] = []
        for scope in scopes:
            if type(scope) is not str or not scope or len(scope) > 200:
                raise GoogleTokenError("a scope is a non-empty name")
            cleaned.append(scope)
        return tuple(cleaned)

    def _accept(self, body: Mapping[str, object], wanted: tuple[str, ...], now: int) -> MintedToken:
        """Take the response apart, refusing anything short of complete."""

        access = body.get("access_token")
        expires = body.get("expires_in")
        if type(access) is not str or not access:
            raise GoogleTokenError("the Google token response carried no access token")
        if type(expires) is not int or expires <= 0:
            raise GoogleTokenError("the Google token response carried no lifetime")
        granted = str(body.get("scope", "")).split()
        if granted and canonical_scopes(granted) != canonical_scopes(wanted):
            # Narrower is a call that will fail later in a confusing place;
            # wider is authority nobody asked for. Both are refused here.
            raise GoogleTokenError("Google granted a different scope set than the one asked for")
        return MintedToken(access_token=access, expires_at=now + expires, scopes=wanted)


__all__ = [
    "GOOGLE_TOKEN_URI",
    "MAX_SCOPES",
    "REFRESH_SKEW_SECONDS",
    "GoogleTokenError",
    "GoogleTokenMinter",
    "MintedToken",
    "TokenExchange",
    "canonical_scopes",
]
