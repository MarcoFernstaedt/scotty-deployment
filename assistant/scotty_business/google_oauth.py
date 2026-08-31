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
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class GoogleOAuthError(RuntimeError):
    """Google OAuth state is absent, malformed, expired, or out of scope."""


@dataclass(frozen=True, slots=True, repr=False)
class OAuthToken:
    access_token: str
    refresh_token: str
    expires_at: int
    scopes: tuple[str, ...]
    account_email: str

    def __repr__(self) -> str:
        return "OAuthToken(<redacted>)"


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
                "version": 1,
                "access_token": token.access_token,
                "refresh_token": token.refresh_token,
                "expires_at": token.expires_at,
                "scopes": list(token.scopes),
                "account_email": token.account_email,
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
            if not isinstance(raw, dict) or set(raw) != {
                "version",
                "access_token",
                "refresh_token",
                "expires_at",
                "scopes",
                "account_email",
            }:
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
        return (
            set(token.scopes) == set(exact_scopes)
            and secrets.compare_digest(token.account_email.casefold(), account_email.casefold())
            and token.expires_at > int(time.time()) + 60
        )

    def status(self) -> dict[str, object]:
        try:
            token = self.read()
        except GoogleOAuthError:
            return {"configured": False, "scope_count": 0}
        return {
            "configured": True,
            "scope_count": len(token.scopes),
            "account_bound": bool(token.account_email),
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


def authorize_installed_app(
    client_path: Path,
    token_store: GoogleTokenStore,
    exact_scopes: tuple[str, ...],
    *,
    timeout: int = 300,
) -> None:
    """Perform Google's installed-app loopback browser flow without exposing codes."""

    client = _load_installed_client(client_path)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    result: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
            if parsed.path != "/oauth2/callback" or query.get("state") != [state]:
                self.send_response(400)
                self.end_headers()
                return
            code = query.get("code", [""])[0]
            if not code:
                self.send_response(400)
                self.end_headers()
                return
            result["code"] = code
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Google Workspace consent completed. Return to the local terminal.")

    server = HTTPServer(("127.0.0.1", 0), Callback)
    server.timeout = timeout
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
    if not webbrowser.open(authorization_url, new=1, autoraise=True):
        server.server_close()
        raise GoogleOAuthError("a local browser could not be opened for Google consent")
    server.handle_request()
    server.server_close()
    code = result.get("code")
    if not code:
        raise GoogleOAuthError("Google consent did not complete before the timeout")
    body = urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode()
    request = urllib.request.Request(client["token_uri"], data=body, method="POST")  # noqa: S310
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            token_body = json.loads(response.read(65_537).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        raise GoogleOAuthError("Google OAuth token exchange failed") from exc
    if not isinstance(token_body, dict):
        raise GoogleOAuthError("Google OAuth token response is malformed")
    granted = tuple(str(token_body.get("scope", "")).split())
    if set(granted) != set(exact_scopes):
        raise GoogleOAuthError("Google OAuth granted scopes do not match configured scopes")
    access = token_body.get("access_token")
    refresh = token_body.get("refresh_token")
    expires = token_body.get("expires_in")
    if type(access) is not str or type(refresh) is not str or type(expires) is not int:
        raise GoogleOAuthError("Google OAuth token response is incomplete")
    identity_request = urllib.request.Request(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(identity_request, timeout=30) as response:  # noqa: S310
            identity = json.loads(response.read(65_537).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        raise GoogleOAuthError("Google OAuth account verification failed") from exc
    account_email = identity.get("email") if isinstance(identity, dict) else None
    if type(account_email) is not str or "@" not in account_email:
        raise GoogleOAuthError("Google OAuth account identity is unavailable")
    token_store.write(
        OAuthToken(access, refresh, int(time.time()) + expires, granted, account_email)
    )
