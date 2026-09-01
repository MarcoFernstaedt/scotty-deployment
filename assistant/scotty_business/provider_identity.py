"""Which provider identity one authenticated actor is allowed to act as.

Trent and Mikey share a Discord bot and a deployment, not an inbox. Every
provider call therefore runs as exactly one actor, and that actor is resolved
here from the Discord provenance the gateway already authorized — never from
anything the model wrote. A tool argument that names an account, a token, a
profile, or another user is refused before the call reaches a provider.

An actor with no identity of their own for a provider gets no access to it and
a deterministic instruction for connecting their own; they never fall through
to someone else's credential. Where a provider genuinely has one business
identity, the shared credential is used and marked shared, and the record still
carries the authenticated actor so every effect stays attributable.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .config import GoogleWorkspaceScope, RuntimeConfig
from .policy import Principal, Role
from .routing import CLIENT_PROFILES


class ProviderIdentityError(RuntimeError):
    """An actor cannot be bound to a provider identity, so nothing runs."""


#: Argument names that would let a model choose whose identity a call uses.
#: The check is by name, at any depth, because an override is an override
#: wherever it appears in a payload.
_IDENTITY_ARGUMENTS = frozenset(
    {
        "account",
        "account_email",
        "account_id",
        "acting_as",
        "actor",
        "as_role",
        "as_user",
        "authorization",
        "client_id",
        "client_secret",
        "credential",
        "credential_class",
        "credential_id",
        "impersonate",
        "oauth_client",
        "on_behalf_of",
        "principal",
        "profile",
        "refresh_token",
        "role",
        "run_as",
        "session_id",
        "tenant",
        "token",
        "token_path",
        "user",
        "user_id",
        "workspace_account",
    }
)

_MAX_OVERRIDE_DEPTH = 12

#: The credential each provider needs. An unsuffixed variable is the shared
#: business identity; a suffixed one belongs to exactly one actor.
_TRELLO_KEY = "SCOTTY_TRELLO_API_KEY"
_TRELLO_TOKEN = "SCOTTY_TRELLO_TOKEN"  # noqa: S105 - env var name, not a secret
_GHL_TOKEN = "SCOTTY_GHL_PRIVATE_TOKEN"  # noqa: S105 - env var name, not a secret
_RENTCAST_KEY = "SCOTTY_RENTCAST_API_KEY"

_SAFE_SUFFIX = re.compile(r"[A-Z0-9_]{1,32}")


def reject_identity_override(args: object, depth: int = 0) -> None:
    """Refuse any argument that names an actor, account, or credential.

    Called before a provider identity is resolved, so a model that asks to run
    as someone else is stopped before the question of authority even arises.
    """

    if depth > _MAX_OVERRIDE_DEPTH:
        raise ProviderIdentityError("tool arguments are nested too deeply")
    if isinstance(args, Mapping):
        for key, value in args.items():
            if type(key) is str and key.casefold() in _IDENTITY_ARGUMENTS:
                raise ProviderIdentityError(
                    "the acting identity is resolved from the authorized Discord "
                    "origin and cannot be supplied"
                )
            reject_identity_override(value, depth + 1)
        return
    if isinstance(args, list | tuple):
        for item in args:
            reject_identity_override(item, depth + 1)


def _suffix(role: Role) -> str:
    suffix = role.value.upper()
    if not _SAFE_SUFFIX.fullmatch(suffix):  # pragma: no cover - roles are fixed slugs
        raise ProviderIdentityError("role is not a usable credential suffix")
    return suffix


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """One actor's provider identity. Never rendered with its credentials."""

    role: Role
    profile: str
    user_id: str
    google: GoogleWorkspaceScope | None
    google_token_name: str
    _credentials: Mapping[str, tuple[str, bool]] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        return f"ProviderIdentity(role={self.role.value}, profile={self.profile})"

    def __str__(self) -> str:
        return self.__repr__()

    @property
    def google_account(self) -> str | None:
        return self.google.account_email if self.google is not None else None

    @property
    def google_linked(self) -> bool:
        """Whether this actor has a Workspace account of their own configured."""

        return self.google is not None

    def credential(self, name: str) -> str | None:
        entry = self._credentials.get(name)
        return entry[0] if entry is not None else None

    def is_shared(self, name: str) -> bool:
        entry = self._credentials.get(name)
        return bool(entry is not None and entry[1])

    @property
    def trello_token(self) -> str | None:
        return self.credential(_TRELLO_TOKEN)

    @property
    def trello_key(self) -> str | None:
        return self.credential(_TRELLO_KEY)

    @property
    def trello_shared(self) -> bool:
        return self.is_shared(_TRELLO_TOKEN)

    @property
    def trello_connected(self) -> bool:
        return bool(self.trello_key and self.trello_token)

    @property
    def ghl_token(self) -> str | None:
        return self.credential(_GHL_TOKEN)

    @property
    def rentcast_key(self) -> str | None:
        return self.credential(_RENTCAST_KEY)

    def attribution(self) -> dict[str, object]:
        """Who an effect is recorded against. Carries no credential."""

        return {
            "role": self.role.value,
            "profile": self.profile,
            "user_id": self.user_id,
            "google_account": self.google_account,
            "shared_identities": sorted(
                name for name, (_, shared) in self._credentials.items() if shared
            ),
        }


class ProviderIdentityResolver:
    """Map an authorized client principal to the identity it may act as."""

    def __init__(self, config: RuntimeConfig):
        self.config = config

    def resolve(
        self, principal: Principal, *, environ: Mapping[str, str] | None = None
    ) -> ProviderIdentity:
        if principal.role not in CLIENT_PROFILES:
            # The maintainer route is served separately and never borrows a
            # client's provider identity.
            raise ProviderIdentityError("this route has no client provider identity")
        environ = os.environ if environ is None else environ
        suffix = _suffix(principal.role)
        credentials: dict[str, tuple[str, bool]] = {}
        for name in (_TRELLO_KEY, _TRELLO_TOKEN, _GHL_TOKEN, _RENTCAST_KEY):
            own = environ.get(f"{name}_{suffix}")
            if own:
                credentials[name] = (own, False)
                continue
            # The unsuffixed variable is the deployment's one shared business
            # identity, which the provider itself may require. Another actor's
            # suffixed credential is never reachable from here at all.
            shared = environ.get(name)
            if shared:
                credentials[name] = (shared, True)
        return ProviderIdentity(
            role=principal.role,
            profile=CLIENT_PROFILES[principal.role],
            user_id=principal.user_id,
            google=self.config.google_for(principal.role),
            google_token_name=f"google-oauth.{principal.role.value}.json",
            _credentials=credentials,
        )


__all__ = [
    "ProviderIdentity",
    "ProviderIdentityError",
    "ProviderIdentityResolver",
    "reject_identity_override",
]
