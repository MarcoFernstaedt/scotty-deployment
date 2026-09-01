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
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
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


#: A Desktop OAuth client redirects to loopback. On a headless server nothing
#: listens there, which is the point: the browser lands on an address that fails
#: to load, and its address bar carries the code back to the operator.
HEADLESS_REDIRECT_URI = "http://localhost:8765/oauth2/callback"


@dataclass(frozen=True, slots=True)
class ConsentRequest:
    """One consent attempt: what to show, and what the exchange must match."""

    authorization_url: str
    redirect_uri: str
    state: str
    verifier: str
    scopes: tuple[str, ...]
    client_id: str

    def presentable(self) -> Mapping[str, object]:
        """Exactly what may be shown to Trent. No secret is in this mapping."""

        return {
            "authorization_url": self.authorization_url,
            "redirect_uri": self.redirect_uri,
            "scopes": list(self.scopes),
        }


def import_client(source: Path, destination: Path, *, owner_uid: int = 0) -> dict[str, str]:
    """Copy a Desktop OAuth client into the protected path, owner-only.

    The file is validated before it is stored, so a wrong client type or an
    unexpected endpoint is refused at import rather than at consent time.
    """

    client = _load_installed_client(source, owner_uid=owner_uid)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.parent.is_symlink() or destination.is_symlink():
        raise GoogleOAuthError("Google OAuth client destination is unsafe")
    payload = json.dumps({"installed": client}, sort_keys=True, separators=(",", ":")).encode()
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        raise GoogleOAuthError("Google OAuth client could not be stored") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
    return client


def begin_consent(
    client_path: Path,
    exact_scopes: tuple[str, ...],
    *,
    owner_uid: int = 0,
    redirect_uri: str = HEADLESS_REDIRECT_URI,
) -> ConsentRequest:
    """Build the exact authorization URL to show, with PKCE, showing no secret."""

    client = _load_installed_client(client_path, owner_uid=owner_uid)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
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
    return ConsentRequest(
        authorization_url=authorization_url,
        redirect_uri=redirect_uri,
        state=state,
        verifier=verifier,
        scopes=tuple(exact_scopes),
        client_id=client["client_id"],
    )


def authorization_code(redirect_url: object, state: str) -> str:
    """Pull the code out of the redirect URL Trent pasted back.

    The whole URL is treated as secret input: nothing here echoes it, and a
    mismatched state, a provider-side error, or a missing code is refused
    without repeating what was pasted.
    """

    if type(redirect_url) is not str or not redirect_url.strip():
        raise GoogleOAuthError("no redirect URL was provided")
    candidate = redirect_url.strip()
    if len(candidate) > 4096:
        raise GoogleOAuthError("the redirect URL is malformed")
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
    }:
        raise GoogleOAuthError("the redirect URL is not the expected loopback address")
    outcome = parse_callback(f"/oauth2/callback?{parsed.query}", state)
    if outcome is None:
        raise GoogleOAuthError("the redirect URL does not match this consent attempt")
    if outcome[0] == "error":
        raise GoogleOAuthError("Google reported that consent was declined or failed")
    return outcome[1]


def complete_consent(
    client_path: Path,
    token_store: GoogleTokenStore,
    request: ConsentRequest,
    redirect_url: object,
    *,
    owner_uid: int = 0,
    exchange: TokenExchange = _post_form,
    verify_account: Callable[[str], str] | None = None,
) -> str:
    """Exchange the pasted redirect for tokens and bind them to the account.

    Returns the verified account email. Nothing else about the exchange leaves
    this function, and no code or token is logged, printed, or raised.
    """

    client = _load_installed_client(client_path, owner_uid=owner_uid)
    code = authorization_code(redirect_url, request.state)
    token_body = exchange(
        url=client["token_uri"],
        fields={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": code,
            "code_verifier": request.verifier,
            "grant_type": "authorization_code",
            "redirect_uri": request.redirect_uri,
        },
    )
    granted = tuple(str(token_body.get("scope", "")).split())
    if canonical_scopes(granted) != canonical_scopes(request.scopes):
        raise GoogleOAuthError("Google OAuth granted scopes do not match configured scopes")
    access = token_body.get("access_token")
    refresh = token_body.get("refresh_token")
    expires = token_body.get("expires_in")
    if type(access) is not str or type(refresh) is not str or type(expires) is not int:
        raise GoogleOAuthError("Google OAuth token response is incomplete")
    account_email = (verify_account or _verify_account)(access)
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
    return account_email


def publish_consent_prompt(path: Path, request: ConsentRequest, *, owner_uid: int = 10000) -> None:
    """Write the non-secret half of a consent attempt for the runtime to show.

    Only what Trent needs in order to click through: the authorization URL, the
    redirect it will land on, and the scopes. The client secret, the verifier,
    and every token stay in the root-owned files.
    """

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise GoogleOAuthError("Google consent prompt path is unsafe")
    payload = json.dumps(dict(request.presentable()), sort_keys=True, separators=(",", ":"))
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        with suppress(OSError, PermissionError):
            os.chown(temporary, owner_uid, owner_uid)
        os.replace(temporary, path)
    except OSError as exc:
        raise GoogleOAuthError("Google consent prompt could not be published") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def clear_consent_prompt(path: Path) -> None:
    """Remove a published consent prompt once its attempt is over.

    The PKCE verifier for an attempt lives only in the root-owned side of that
    attempt, so once the attempt ends the published URL can no longer be
    completed by anyone. Leaving it in place would show Trent a dead link that
    looks live, so the prompt is removed whether consent succeeded or failed.
    """

    with suppress(OSError):
        path.unlink(missing_ok=True)


def read_consent_prompt(path: Path) -> Mapping[str, object] | None:
    """Read the non-secret consent prompt, or None when there is none."""

    if path.is_symlink() or not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, Mapping):
        return None
    url = raw.get("authorization_url")
    if type(url) is not str or not url.startswith(GOOGLE_AUTH_URI):
        return None
    return {
        "authorization_url": url,
        "redirect_uri": str(raw.get("redirect_uri", "")),
        "scopes": [str(item) for item in raw.get("scopes", []) if type(item) is str],
    }


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
