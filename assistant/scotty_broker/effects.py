"""Approvals root minted, and what became of every effect they authorized.

Two things live here because they are the same question asked twice: may this
happen, and did it. Both answers have to survive the process that asked, and
both have to be out of reach of the model-facing side — an approval a runtime
can write is not an approval, and an effect record it can rewrite is not a
record.

An approval is bound to one actor, one operation, one payload hash, and one
resource, and it expires. It is spent once: the row moves to `used` inside the
same transaction that claims it, so two concurrent requests cannot both find it
approved.

An effect is written before the provider is called and settled after. If the
process dies in between, the row is still there saying `unknown`, which is the
answer that stops somebody sending a second message to a seller because the
first one could not be seen.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: Where the installed broker keeps them. Root-owned, beside the credentials.
EFFECTS_PATH = "/var/lib/scotty/effects.db"

#: What an effect can be. There is no fourth answer, and `unknown` is never
#: quietly promoted to either of the others.
VERIFIED = "verified"
FAILED = "failed"
UNKNOWN = "unknown"

_SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'approved',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    used_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS effects (
    effect_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    approval_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    settled_at TEXT NOT NULL DEFAULT '',
    UNIQUE (actor, idempotency_key)
);
"""


class EffectError(RuntimeError):
    """An approval or effect cannot be trusted, so the call does not happen."""


def _now() -> datetime:
    return datetime.now(UTC)


def _moment(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Approval:
    approval_id: str
    actor: str
    operation: str
    payload_hash: str
    resource: str
    status: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Effect:
    effect_id: str
    actor: str
    operation: str
    payload_hash: str
    resource: str
    idempotency_key: str
    approval_id: str
    state: str
    detail: str = ""

    def as_json(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "operation": self.operation,
            "state": self.state,
            "detail": self.detail,
        }


class EffectLedger:
    """Root-owned approvals and effects. The runtime can read neither file."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.path = Path(path)
        self.clock = clock

    def __repr__(self) -> str:
        return f"EffectLedger(path={self.path!s})"

    def _now(self) -> datetime:
        moment = self.clock()
        return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise EffectError("the effect ledger path is unsafe")
        connection = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript(_SCHEMA)
        finally:
            connection.close()
        os.chmod(self.path, 0o600)

    @staticmethod
    def payload_hash(arguments: Mapping[str, object]) -> str:
        """The payload's identity, canonically, so an approval names one call."""

        canonical = json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # -- approvals -------------------------------------------------------

    def approve(
        self,
        *,
        actor: str,
        operation: str,
        payload_hash: str,
        resource: str,
        expires_at: datetime,
    ) -> Approval:
        """Record one approval. Only root's own code ever reaches this."""

        now = self._now()
        expiry = expires_at.astimezone(UTC) if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        if expiry <= now:
            raise EffectError("an approval that has already expired approves nothing")
        approval_id = secrets.token_hex(16)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO approvals (approval_id, actor, operation, payload_hash,
                       resource, status, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, 'approved', ?, ?)""",
                (
                    approval_id,
                    actor,
                    operation,
                    payload_hash,
                    resource,
                    expiry.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return Approval(approval_id, actor, operation, payload_hash, resource, "approved", expiry)

    def claim(
        self,
        approval_id: str,
        *,
        actor: str,
        operation: str,
        payload_hash: str,
        resource: str,
    ) -> Approval:
        """Spend one approval, or explain exactly why it does not apply.

        Claiming is one transaction that both checks and marks used, so two
        requests arriving together cannot both come away holding it.
        """

        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise EffectError("that approval does not exist")
            expiry = _moment(str(row["expires_at"]))
            if str(row["status"]) != "approved":
                raise EffectError("that approval has already been used")
            if expiry is None or now >= expiry:
                raise EffectError("that approval has expired")
            if str(row["actor"]) != actor:
                raise EffectError("that approval belongs to another user")
            if str(row["operation"]) != operation:
                raise EffectError("that approval is for another operation")
            if str(row["payload_hash"]) != payload_hash:
                raise EffectError("that approval is for a different payload")
            if str(row["resource"]) and str(row["resource"]) != resource:
                raise EffectError("that approval is for another resource")
            changed = connection.execute(
                """UPDATE approvals SET status='used', used_at=?
                   WHERE approval_id=? AND status='approved'""",
                (now.isoformat(), approval_id),
            ).rowcount
            if changed != 1:
                raise EffectError("that approval was claimed by something else")
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return Approval(approval_id, actor, operation, payload_hash, resource, "used", now)

    # -- effects ---------------------------------------------------------

    def begin(
        self,
        *,
        actor: str,
        operation: str,
        payload_hash: str,
        resource: str,
        idempotency_key: str,
        approval_id: str = "",
    ) -> tuple[Effect, bool]:
        """Claim the right to make one call, or hand back the record of it.

        The second value says whether this caller is the one that must now make
        the call. A key that has been used before comes back with `False` and
        whatever became of it, which is how the same request arriving twice
        produces one message rather than two.
        """

        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM effects WHERE actor = ? AND idempotency_key = ?",
                (actor, idempotency_key),
            ).fetchone()
            if row is not None:
                connection.commit()
                return self._effect(row), False
            effect_id = secrets.token_hex(16)
            connection.execute(
                """INSERT INTO effects (effect_id, actor, operation, payload_hash, resource,
                       idempotency_key, approval_id, state, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    effect_id,
                    actor,
                    operation,
                    payload_hash,
                    resource,
                    idempotency_key,
                    approval_id,
                    # Written as unknown before the call, so a process that dies
                    # mid-flight leaves the honest answer rather than no answer.
                    UNKNOWN,
                    now.isoformat(),
                ),
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return (
            Effect(
                effect_id,
                actor,
                operation,
                payload_hash,
                resource,
                idempotency_key,
                approval_id,
                UNKNOWN,
            ),
            True,
        )

    def settle(self, effect_id: str, state: str, detail: str = "") -> None:
        if state not in {VERIFIED, FAILED, UNKNOWN}:
            raise EffectError("an effect settles verified, failed, or unknown")
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE effects SET state=?, detail=?, settled_at=? WHERE effect_id=?",
                (state, detail[:500], now.isoformat(), effect_id),
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _effect(row: sqlite3.Row) -> Effect:
        return Effect(
            effect_id=str(row["effect_id"]),
            actor=str(row["actor"]),
            operation=str(row["operation"]),
            payload_hash=str(row["payload_hash"]),
            resource=str(row["resource"]),
            idempotency_key=str(row["idempotency_key"]),
            approval_id=str(row["approval_id"]),
            state=str(row["state"]),
            detail=str(row["detail"]),
        )

    def get(self, effect_id: str) -> Effect:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if row is None:
                raise EffectError("that effect is not in the ledger")
            return self._effect(row)
        finally:
            connection.close()

    def by_idempotency(self, actor: str, idempotency_key: str) -> Effect | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM effects WHERE actor = ? AND idempotency_key = ?",
                (actor, idempotency_key),
            ).fetchone()
            return None if row is None else self._effect(row)
        finally:
            connection.close()

    def unresolved(self, actor: str = "") -> tuple[Effect, ...]:
        """Effects nobody could see the outcome of. These need a person."""

        connection = self._connect()
        try:
            if actor:
                rows = connection.execute(
                    "SELECT * FROM effects WHERE state = ? AND actor = ? ORDER BY started_at",
                    (UNKNOWN, actor),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM effects WHERE state = ? ORDER BY started_at", (UNKNOWN,)
                ).fetchall()
            return tuple(self._effect(row) for row in rows)
        finally:
            connection.close()


__all__ = [
    "EFFECTS_PATH",
    "FAILED",
    "UNKNOWN",
    "VERIFIED",
    "Approval",
    "Effect",
    "EffectError",
    "EffectLedger",
]
