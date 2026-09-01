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

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from .config import GoogleWorkspaceScope, RuntimeConfig
from .policy import Principal, Role
from .routing import CLIENT_PROFILES


class CredentialHolder(Protocol):
    """Whatever can answer whether a credential is usable. Never what it is.

    Both questions are asked per citation rather than per actor name: the
    broker decides whose the request is from the Discord message it cites, so
    this side has no actor to pass and no way to name one.
    """

    def status_for(
        self, provider: str, credential_class: str, provenance: Mapping[str, object]
    ) -> bool: ...

    def owned_for(
        self, provider: str, credential_class: str, provenance: Mapping[str, object]
    ) -> bool: ...


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

#: The credential that decides whether a provider is reachable at all, asked of
#: the broker by (provider, class, actor). The values live only in the broker.
#: The credential that carries an identity for each provider, not the one that
#: carries the application's. Trello's api_key says which product is calling;
#: the token says who. "Whose Trello is this" is a question about the token.
_PROVIDER_CREDENTIALS: tuple[tuple[str, str], ...] = (
    ("trello", "token"),
    ("ghl", "private_token"),
    ("rentcast", "api_key"),
)


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


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """One actor's provider identity. Never rendered with its credentials."""

    role: Role
    profile: str
    user_id: str
    google: GoogleWorkspaceScope | None
    google_token_name: str
    #: Which provider credentials the broker holds for this actor. Names and
    #: whether they are this user's own or the shared business identity —
    #: never the material, which this process has no way to obtain.
    held: Mapping[str, bool] = field(default_factory=dict)

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

    def attribution(self) -> dict[str, object]:
        """Who an effect is recorded against. Carries no credential."""

        return {
            "role": self.role.value,
            "profile": self.profile,
            "user_id": self.user_id,
            "google_account": self.google_account,
            "shared_identities": sorted(name for name, own in self.held.items() if not own),
        }


class ProviderIdentityResolver:
    """Map an authorized client principal to the identity it may act as."""

    def __init__(self, config: RuntimeConfig, broker: CredentialHolder | None = None):
        self.config = config
        # Asks the broker what it holds. It cannot ask for the material, and
        # there is no operation that would return it.
        self.broker = broker

    def resolve(
        self, principal: Principal, *, environ: Mapping[str, str] | None = None
    ) -> ProviderIdentity:
        if principal.role not in CLIENT_PROFILES:
            # The maintainer route is served separately and never borrows a
            # client's provider identity.
            raise ProviderIdentityError("this route has no client provider identity")
        del environ
        return ProviderIdentity(
            role=principal.role,
            profile=CLIENT_PROFILES[principal.role],
            user_id=principal.user_id,
            google=self.config.google_for(principal.role),
            google_token_name=f"google-oauth.{principal.role.value}.json",
            held=dict(self.held(principal)),
        )

    def held(self, principal: Principal) -> dict[str, bool]:
        """Which providers the broker holds a credential for, and whose it is.

        True means this actor's own; False means the shared business identity.
        A provider that is absent from the mapping is one this actor cannot
        reach at all. The material itself is never returned here or anywhere
        else in this process — it does not leave the broker.
        """

        if self.broker is None:
            return {}
        citation = principal.citation()
        if citation is None:
            # Nobody has asked for anything, so there is no person to answer
            # about. Not an error: an empty mapping is "reaches nothing".
            return {}
        held: dict[str, bool] = {}
        for provider, credential_class in _PROVIDER_CREDENTIALS:
            # One question, asked of the broker for whoever Discord says wrote
            # the message this work is for. Whether the credential is their own
            # or a granted shared one is the broker's business, not this side's.
            if self.broker.status_for(provider, credential_class, citation):
                held[provider] = self.broker.owned_for(provider, credential_class, citation)
        return held


__all__ = [
    "ProviderIdentity",
    "ProviderIdentityError",
    "ProviderIdentityResolver",
    "reject_identity_override",
]
