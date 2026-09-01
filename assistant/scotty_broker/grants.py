"""Explicit permission to spend a shared identity, written down by root.

One business Trello account, two people using it. That is an ordinary thing to
want and a dangerous thing to infer. The version this replaces inferred it: if
an actor had no credential of their own, the executor quietly reached for the
shared one. Nothing anywhere said Mikey was allowed to act as the business —
the shared token simply existed, and existing was treated as permission.

So permission is a record now. A grant names one actor, one provider, the
operations it covers, the resources it covers, and when it stops being true.
Absent a matching grant, a shared credential is not merely unused: the answer
is `not authorized`, which is a different thing from `not connected` and reads
differently to whoever has to fix it.

Grants live beside the credentials, root-owned, outside every mount the runtime
can reach. There is no wire operation that writes one from an actor socket.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

#: Where the installed broker keeps them. Root-owned, 0600, beside the store.
GRANTS_PATH = "/var/lib/scotty/grants.json"

MAX_ENTRIES = 256


class GrantError(RuntimeError):
    """A grant is malformed or unsafe, so nothing is written and none is used."""


def _now() -> datetime:
    return datetime.now(UTC)


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Grant:
    """One actor's explicit permission to use one shared provider identity."""

    actor: str
    provider: str
    operations: tuple[str, ...]
    resources: tuple[str, ...]
    expires_at: datetime
    granted_by: str = "root"
    revoked: bool = False
    grant_id: str = field(default_factory=lambda: secrets.token_hex(16))

    def covers(self, operation: str, resource: str, *, now: datetime) -> bool:
        """Whether this grant actually authorizes that exact call, right now."""

        if self.revoked or now >= self.expires_at:
            return False
        if operation not in self.operations:
            return False
        # An empty resource list is a grant over the provider rather than over
        # named resources; a non-empty one is exhaustive.
        return not self.resources or not resource or resource in self.resources

    def as_json(self) -> dict[str, object]:
        return {
            "grant_id": self.grant_id,
            "actor": self.actor,
            "provider": self.provider,
            "operations": list(self.operations),
            "resources": list(self.resources),
            "expires_at": self.expires_at.isoformat(),
            "granted_by": self.granted_by,
            "revoked": self.revoked,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, object]) -> Grant | None:
        expires = _moment(raw.get("expires_at"))
        actor, provider = raw.get("actor"), raw.get("provider")
        operations, resources = raw.get("operations"), raw.get("resources")
        if (
            expires is None
            or type(actor) is not str
            or type(provider) is not str
            or not isinstance(operations, list)
            or not isinstance(resources, list)
        ):
            # A malformed entry authorizes nothing. It is dropped rather than
            # guessed at, because a half-understood grant is not a grant.
            return None
        return cls(
            actor=actor,
            provider=provider,
            operations=tuple(str(item) for item in operations),
            resources=tuple(str(item) for item in resources),
            expires_at=expires,
            granted_by=str(raw.get("granted_by", "root")),
            revoked=raw.get("revoked") is True,
            grant_id=str(raw.get("grant_id", secrets.token_hex(16))),
        )


class GrantStore:
    """Every shared-identity grant this deployment has, and nothing else."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.path = Path(path)
        self.clock = clock

    def __repr__(self) -> str:
        return f"GrantStore(path={self.path!s})"

    def _now(self) -> datetime:
        moment = self.clock()
        return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)

    def _load(self) -> list[Grant]:
        if self.path.is_symlink() or not self.path.is_file():
            return []
        metadata = self.path.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise GrantError("the grant store is not owner-only")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GrantError("the grant store is unreadable") from exc
        if not isinstance(raw, list):
            raise GrantError("the grant store is malformed")
        parsed = [Grant.from_json(item) for item in raw if isinstance(item, Mapping)]
        return [item for item in parsed if item is not None]

    def _save(self, grants: Sequence[Grant]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise GrantError("the grant store path is unsafe")
        payload = json.dumps(
            [item.as_json() for item in grants], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        temporary = self.path.parent / f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise GrantError("the grant store could not be written") from exc

    def put(self, grant: Grant) -> Grant:
        """Record one grant. Only ever called with root's own authority."""

        if not grant.operations:
            raise GrantError("a grant that names no operation authorizes nothing")
        grants = [item for item in self._load() if item.grant_id != grant.grant_id]
        if len(grants) >= MAX_ENTRIES:
            raise GrantError("this deployment holds as many grants as it will hold")
        grants.append(grant)
        self._save(grants)
        return grant

    def revoke(self, actor: str, provider: str) -> int:
        """Withdraw every grant an actor holds over one provider."""

        grants = self._load()
        changed = 0
        updated: list[Grant] = []
        for item in grants:
            if item.actor == actor and item.provider == provider and not item.revoked:
                updated.append(
                    Grant(
                        actor=item.actor,
                        provider=item.provider,
                        operations=item.operations,
                        resources=item.resources,
                        expires_at=item.expires_at,
                        granted_by=item.granted_by,
                        revoked=True,
                        grant_id=item.grant_id,
                    )
                )
                changed += 1
                continue
            updated.append(item)
        if changed:
            self._save(updated)
        return changed

    def find(self, actor: str, provider: str, operation: str, resource: str = "") -> Grant | None:
        """The grant that authorizes this exact call, or nothing at all."""

        now = self._now()
        for item in self._load():
            if item.actor != actor or item.provider != provider:
                continue
            if item.covers(operation, resource, now=now):
                return item
        return None

    def any_for(self, actor: str, provider: str) -> Grant | None:
        """Whether this actor holds any live grant over that provider.

        Used to answer "is this connected for me?" without naming an operation:
        readiness is about whether there is a route at all, and each individual
        call still has to find a grant that covers it.
        """

        now = self._now()
        for item in self._load():
            if item.actor != actor or item.provider != provider or item.revoked:
                continue
            if now < item.expires_at:
                return item
        return None

    def list_all(self) -> tuple[Grant, ...]:
        return tuple(self._load())


__all__ = ["GRANTS_PATH", "Grant", "GrantError", "GrantStore"]
