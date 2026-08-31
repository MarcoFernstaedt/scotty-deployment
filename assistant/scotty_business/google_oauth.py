from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class GoogleOAuthError(RuntimeError):
    """Google OAuth state is absent, malformed, expired, or out of scope."""


GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint, not a secret
GOOGLE_USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"

#: A form POST to a pinned Google endpoint. Injected so tests never use a socket.
TokenExchange = Callable[..., dict[str, object]]


#: Refresh this many seconds before the access token actually expires.
REFRESH_SKEW_SECONDS = 120

#: Google returns the OpenID shorthand scopes in their expanded form, so the
#: configured shorthand and the granted URL are the same authorization.
_SCOPE_ALIASES: dict[str, str] = {
    "email": "https://www.googleapis.com/auth/userinfo.email",
    "profile": "https://www.googleapis.com/auth/userinfo.profile",
}


def canonical_scopes(scopes: Iterable[str]) -> frozenset[str]:
    """Compare scope sets by authorization rather than by spelling."""

    return frozenset(_SCOPE_ALIASES.get(scope, scope) for scope in scopes)


_TOKEN_FIELDS = frozenset(
    {
        "version",
        "access_token",
        "refresh_token",
        "expires_at",
        "scopes",
        "account_email",
        "client_id",
        "client_secret",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class OAuthToken:
    """Owner-only OAuth state. Never rendered, logged, or returned to a caller."""

    access_token: str
    refresh_token: str
    expires_at: int
    scopes: tuple[str, ...]
    account_email: str
    client_id: str = ""
    client_secret: str = ""

    def __repr__(self) -> str:
        return "OAuthToken(<redacted>)"

    def access_valid(self, *, now: int | None = None) -> bool:
        current = int(time.time()) if now is None else now
        return self.expires_at > current + REFRESH_SKEW_SECONDS


class GoogleTokenStore:
    """Owner-only OAuth token state below the private Scotty data directory."""

    def __init__(self, path: Path, *, owner_uid: int = 10000, owner_gid: int = 10000):
        self.path = path
        self.owner_uid = owner_uid
        self.owner_gid = owner_gid

    def __repr__(self) -> str:
        return f"GoogleTokenStore(path={self.path!s})"

    def write(self, token: OAuthToken) -> None:
        if not token.access_token or not token.refresh_token or not token.scopes:
            raise GoogleOAuthError("Google OAuth token state is incomplete")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise GoogleOAuthError("Google OAuth token path is unsafe")
        payload = json.dumps(
            {
                "version": 2,
                "access_token": token.access_token,
                "refresh_token": token.refresh_token,
                "expires_at": token.expires_at,
                "scopes": list(token.scopes),
                "account_email": token.account_email,
                "client_id": token.client_id,
                "client_secret": token.client_secret,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chown(temporary, self.owner_uid, self.owner_gid)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except OSError as exc:
            raise GoogleOAuthError("Google OAuth token publication failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    def read(self) -> OAuthToken:
        if self.path.is_symlink() or not self.path.is_file():
            raise GoogleOAuthError("Google OAuth token state is unavailable")
        if self.path.stat().st_mode & 0o077:
            raise GoogleOAuthError("Google OAuth token state is not owner-only")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != _TOKEN_FIELDS or raw["version"] != 2:
                raise ValueError
            scopes = raw["scopes"]
            if not isinstance(scopes, list):
                raise ValueError
            token = OAuthToken(
                access_token=str(raw["access_token"]),
                refresh_token=str(raw["refresh_token"]),
                expires_at=int(raw["expires_at"]),
                scopes=tuple(str(item) for item in scopes),
                account_email=str(raw["account_email"]),
                client_id=str(raw["client_id"]),
                client_secret=str(raw["client_secret"]),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise GoogleOAuthError("Google OAuth token state is malformed") from exc
        if not token.access_token or not token.refresh_token or "@" not in token.account_email:
            raise GoogleOAuthError("Google OAuth token state is incomplete")
        return token

    def ready(self, exact_scopes: tuple[str, ...], account_email: str) -> bool:
        try:
            token = self.read()
        except GoogleOAuthError:
            return False
        # Expiry alone never means "not connected": an hour-old access token is
        # refreshed from stored state instead of forcing a second browser consent.
        return canonical_scopes(token.scopes) == canonical_scopes(
            exact_scopes
        ) and secrets.compare_digest(token.account_email.casefold(), account_email.casefold())

    def status(self) -> dict[str, object]:
        try:
            token = self.read()
        except GoogleOAuthError:
            return {"configured": False, "scope_count": 0}
        return {
            "configured": True,
            "scope_count": len(token.scopes),
            "account_bound": bool(token.account_email),
            "access_valid": token.access_valid(),
            "refreshable": bool(token.refresh_token and token.client_id and token.client_secret),
        }


def _load_installed_client(path: Path, *, owner_uid: int = 0) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise GoogleOAuthError("Google OAuth client material is unavailable")
    metadata = path.stat()
    if metadata.st_uid != owner_uid or metadata.st_mode & 0o077:
        raise GoogleOAuthError("Google OAuth client material must be owner-only")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        installed = raw["installed"]
        if not isinstance(installed, dict):
            raise ValueError
        result = {
            name: str(installed[name])
            for name in ("client_id", "client_secret", "auth_uri", "token_uri")
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GoogleOAuthError("Google OAuth client material is malformed") from exc
    if result["auth_uri"] != "https://accounts.google.com/o/oauth2/auth":
        raise GoogleOAuthError("Google OAuth authorization endpoint is unexpected")
    if result["token_uri"] != "https://oauth2.googleapis.com/token":  # noqa: S105
        raise GoogleOAuthError("Google OAuth token endpoint is unexpected")
    if not result["client_id"] or not result["client_secret"]:
        raise GoogleOAuthError("Google OAuth client material is incomplete")
    return result


def parse_callback(path: object, state: str) -> tuple[str, str] | None:
    """Classify one loopback request without ever raising out of the handler.

    Returns ("code", value) or ("error", reason) for the exact expected callback
    and `None` for anything else, so browser noise such as a favicon request
    never consumes or aborts the consent flow.
    """

    if type(path) is not str or not path:
        return None
    try:
        parsed = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
    except ValueError:
        return None
    if parsed.path != "/oauth2/callback" or query.get("state") != [state]:
        return None
    error = query.get("error", [""])[0]
    if error:
        return ("error", error[:120])
    code = query.get("code", [""])[0]
    if not code:
        return None
    return ("code", code)


def _post_form(url: str, fields: dict[str, str]) -> dict[str, object]:
    """Exchange form fields at a Google endpoint and return the parsed body."""

    request = urllib.request.Request(  # noqa: S310 - endpoint is pinned above
        url, data=urllib.parse.urlencode(fields).encode(), method="POST"
    )
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = json.loads(response.read(65_537).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        raise GoogleOAuthError("Google OAuth token exchange failed") from exc
    if not isinstance(body, dict):
        raise GoogleOAuthError("Google OAuth token response is malformed")
    return body


def _refresh(token: OAuthToken, exchange: TokenExchange) -> dict[str, object]:
    if not token.refresh_token or not token.client_id or not token.client_secret:
        raise GoogleOAuthError("Google OAuth state cannot refresh without a new browser consent")
    return exchange(
        url=GOOGLE_TOKEN_URI,
        fields={
            "client_id": token.client_id,
            "client_secret": token.client_secret,
            "refresh_token": token.refresh_token,
            "grant_type": "refresh_token",
        },
    )


def ensure_access_token(
    store: GoogleTokenStore,
    exact_scopes: tuple[str, ...],
    account_email: str,
    *,
    exchange: TokenExchange = _post_form,
    now: int | None = None,
) -> str:
    """Return a currently valid access token, refreshing it in place if needed.

    The refresh never widens scope, never rebinds the account, and never
    replaces stored state unless the provider returned a complete in-scope
    response. Any failure leaves the previous state exactly as it was.
    """

    token = store.read()
    if canonical_scopes(token.scopes) != canonical_scopes(
        exact_scopes
    ) or not secrets.compare_digest(token.account_email.casefold(), account_email.casefold()):
        raise GoogleOAuthError("Google OAuth state is bound to another account or scope set")
    current = int(time.time()) if now is None else now
    if token.access_valid(now=current):
        return token.access_token

    body = _refresh(token, exchange)
    access = body.get("access_token")
    expires = body.get("expires_in")
    if type(access) is not str or not access or type(expires) is not int or expires <= 0:
        raise GoogleOAuthError("Google OAuth refresh response is incomplete")
    granted = str(body.get("scope", "")).split()
    if granted and canonical_scopes(granted) != canonical_scopes(exact_scopes):
        raise GoogleOAuthError("Google OAuth refresh returned a different scope set")
    rotated = body.get("refresh_token")
    store.write(
        OAuthToken(
            access_token=access,
            refresh_token=rotated if type(rotated) is str and rotated else token.refresh_token,
            expires_at=current + expires,
            scopes=token.scopes,
            account_email=token.account_email,
            client_id=token.client_id,
            client_secret=token.client_secret,
        )
    )
    return access


def authorize_installed_app(
    client_path: Path,
    token_store: GoogleTokenStore,
    exact_scopes: tuple[str, ...],
    *,
    timeout: int = 300,
    exchange: TokenExchange = _post_form,
) -> None:
    """Perform Google's installed-app loopback browser flow without exposing codes."""

    client = _load_installed_client(client_path)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    outcome: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = parse_callback(self.path, state)
            if parsed is None:
                # Browser noise (a favicon probe, a stray reload) must not
                # consume or abort the one consent this flow is waiting for.
                self.send_response(404)
                self.end_headers()
                return
            outcome[parsed[0]] = parsed[1]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Google Workspace consent completed. Return to the local terminal.")

    server = HTTPServer(("127.0.0.1", 0), Callback)
    server.timeout = 5
    redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2/callback"
    authorization_url = (
        client["auth_uri"]
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": client["client_id"],
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(exact_scopes),
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    print("Opening Google provider-owned browser consent. No authorization code will be displayed.")
    try:
        if not webbrowser.open(authorization_url, new=1, autoraise=True):
            raise GoogleOAuthError("a local browser could not be opened for Google consent")
        deadline = time.monotonic() + timeout
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "error" in outcome:
        raise GoogleOAuthError("Google consent was declined or failed at the provider")
    code = outcome.get("code")
    if not code:
        raise GoogleOAuthError("Google consent did not complete before the timeout")

    token_body = exchange(
        url=client["token_uri"],
        fields={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    granted = tuple(str(token_body.get("scope", "")).split())
    if canonical_scopes(granted) != canonical_scopes(exact_scopes):
        raise GoogleOAuthError("Google OAuth granted scopes do not match configured scopes")
    access = token_body.get("access_token")
    refresh = token_body.get("refresh_token")
    expires = token_body.get("expires_in")
    if type(access) is not str or type(refresh) is not str or type(expires) is not int:
        raise GoogleOAuthError("Google OAuth token response is incomplete")
    account_email = _verify_account(access)
    token_store.write(
        OAuthToken(
            access_token=access,
            refresh_token=refresh,
            expires_at=int(time.time()) + expires,
            scopes=granted,
            account_email=account_email,
            client_id=client["client_id"],
            client_secret=client["client_secret"],
        )
    )


def _verify_account(access_token: str) -> str:
    """Bind the granted token to the exact Google account that consented."""

    request = urllib.request.Request(  # noqa: S310 - endpoint is pinned above
        GOOGLE_USERINFO_URI,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            identity = json.loads(response.read(65_537).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        raise GoogleOAuthError("Google OAuth account verification failed") from exc
    account_email = identity.get("email") if isinstance(identity, dict) else None
    if type(account_email) is not str or "@" not in account_email:
        raise GoogleOAuthError("Google OAuth account identity is unavailable")
    return account_email
