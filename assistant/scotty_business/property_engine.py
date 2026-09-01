"""Typed property-card work against Trello, proven and never doubled.

A Trello acknowledgement is what the provider says it did. It is not proof, and
it is not always delivered: a request can land and its reply can be lost. So
every mutation here is followed by an independent read of the card, and only an
exact match counts as done. Anything else — a lost reply, a disagreeing read, a
card that is suddenly elsewhere — becomes `unknown` and is reconciled before
anyone retries it.

The other half of the problem is duplication. Two people describing the same
house, or one person retrying after a timeout, must not produce two cards. Every
creation therefore claims a stable key derived from what identifies the property
itself, and a claim that already exists is reconciled rather than repeated.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .adapters.http import AmbiguousEffectError, ProviderError
from .adapters.records import ProviderRecord
from .config import RuntimeConfig
from .policy import Principal
from .property_cards import (
    Authority,
    CardDiff,
    DuplicateResult,
    FieldValue,
    PropertyCard,
    compare,
    dedupe_key,
    duplicate_score,
    find_duplicates,
    parse_card,
)

#: How many cards one bulk review may cover. Past this a batch stops being a
#: review and becomes a mass edit, which belongs in the approval ledger.
MAX_BULK_CARDS = 50

#: Operations a client may run directly, and operations that need an approval.
ROUTINE_OPERATIONS = frozenset({"create", "update", "move", "label", "reformat"})
CONSEQUENCE_OPERATIONS = frozenset({"archive", "bulk_update", "bulk_move"})


def consequence_operations() -> frozenset[str]:
    return CONSEQUENCE_OPERATIONS


class EffectStatus(StrEnum):
    """What actually happened to one intended change."""

    VERIFIED = "verified"
    UNKNOWN = "unknown"
    FAILED = "failed"
    REFUSED = "refused"
    DUPLICATE = "duplicate"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS effects (
    effect_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    card_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS effects_claim
    ON effects (operation, dedupe_key);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class EffectRecord:
    """One durable intent and what became of it."""

    effect_id: str
    dedupe_key: str
    operation: str
    payload_hash: str
    card_id: str
    status: EffectStatus


class EffectLog:
    """Durable intent and outcome records for every property-card mutation.

    The claim is the point: an intent is written before the provider is called,
    so a crash between the call and its reply leaves a record to reconcile
    against instead of an invisible half-effect.
    """

    def __init__(self, path: Path | str):
        self.path = str(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(_SCHEMA)
        finally:
            connection.close()

    def claim(
        self,
        *,
        operation: str,
        key: str,
        actor: Principal,
        payload_hash: str,
        source_revision: str,
    ) -> tuple[EffectRecord, bool]:
        """Reserve one intent. Returns the record and whether it is new."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM effects WHERE operation = ? AND dedupe_key = ?",
                (operation, key),
            ).fetchone()
            if row is not None:
                connection.execute("COMMIT")
                return self._record(row), False
            effect_id = uuid.uuid4().hex
            moment = _now()
            connection.execute(
                """INSERT INTO effects (
                    effect_id, dedupe_key, operation, actor_json, payload_hash,
                    source_revision, card_id, status, receipt_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '', ?, '{}', ?, ?)""",
                (
                    effect_id,
                    key,
                    operation,
                    json.dumps(_actor_json(actor), sort_keys=True),
                    payload_hash,
                    source_revision,
                    EffectStatus.UNKNOWN.value,
                    moment,
                    moment,
                ),
            )
            connection.execute("COMMIT")
            return (
                EffectRecord(
                    effect_id=effect_id,
                    dedupe_key=key,
                    operation=operation,
                    payload_hash=payload_hash,
                    card_id="",
                    status=EffectStatus.UNKNOWN,
                ),
                True,
            )
        finally:
            connection.close()

    def settle(
        self,
        effect_id: str,
        status: EffectStatus,
        *,
        card_id: str = "",
        receipt: Mapping[str, object] | None = None,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE effects SET status = ?, card_id = ?, receipt_json = ?, updated_at = ?"
                " WHERE effect_id = ?",
                (
                    status.value,
                    card_id,
                    json.dumps(dict(receipt or {}), sort_keys=True),
                    _now(),
                    effect_id,
                ),
            )
            connection.execute("COMMIT")
        finally:
            connection.close()

    def receipt(self, effect_id: str) -> dict[str, object]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?", (effect_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("no such property-card effect")
        return {
            "effect_id": row["effect_id"],
            "operation": row["operation"],
            "actor": json.loads(row["actor_json"]),
            "payload_hash": row["payload_hash"],
            "source_revision": row["source_revision"],
            "card_id": row["card_id"],
            "status": row["status"],
            **json.loads(row["receipt_json"]),
        }

    @staticmethod
    def _record(row: sqlite3.Row) -> EffectRecord:
        return EffectRecord(
            effect_id=row["effect_id"],
            dedupe_key=row["dedupe_key"],
            operation=row["operation"],
            payload_hash=row["payload_hash"],
            card_id=row["card_id"],
            status=EffectStatus(row["status"]),
        )


def _actor_json(actor: Principal) -> dict[str, object]:
    """Who did it. Identifiers only, never a credential."""

    return {
        "role": actor.role.value,
        "user_id": actor.user_id,
        "channel_id": actor.channel_id,
    }


@dataclass(frozen=True, slots=True)
class EffectOutcome:
    """The result of one attempted change, in the caller's own terms."""

    status: EffectStatus
    effect_id: str
    card_id: str = ""
    reason: str = ""
    reconciled: bool = False
    duplicates: tuple[Mapping[str, object], ...] = ()

    def as_json(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "effect_id": self.effect_id,
            "card_id": self.card_id,
            "reason": self.reason,
            "reconciled": self.reconciled,
            "duplicates": [dict(item) for item in self.duplicates],
            "reconcile_before_retry": self.status is EffectStatus.UNKNOWN,
        }


@dataclass(frozen=True, slots=True)
class BulkPlan:
    """Exactly what a batch would do, before anyone agrees to it."""

    operation: str
    affected: int
    changes: tuple[Mapping[str, object], ...]
    unreadable: tuple[str, ...] = ()
    diffs: Mapping[str, CardDiff] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "affected": self.affected,
            "changes": [dict(change) for change in self.changes],
            "unreadable": list(self.unreadable),
        }


#: The Trello fields a card's canonical values are written into.
_NAME_FIELD = "name"
_DESC_FIELD = "desc"


class PropertyCardEngine:
    """Typed property operations with readback, idempotency, and receipts."""

    def __init__(
        self,
        config: RuntimeConfig,
        trello: object,
        effects: EffectLog,
        *,
        list_id: str = "",
        source_revision: str = "property-card-v2",
    ):
        self.config = config
        self.trello = trello
        self.effects = effects
        self.list_id = list_id or (config.trello.list_ids[0] if config.trello else "")
        self.source_revision = source_revision

    # ---- reading -------------------------------------------------------

    def existing(self) -> tuple[PropertyCard, ...]:
        """Every card currently on the configured board, canonically shaped."""

        return self._existing()[0]

    def _existing(self) -> tuple[tuple[PropertyCard, ...], bool]:
        """The board, and whether that really is the whole board.

        The flag travels with the cards because a duplicate check is only worth
        anything against a complete board: answering "no match" from the first
        thousand cards of a larger one is how one property gets two cards.
        """

        records, complete = self.trello.list_all_cards()  # type: ignore[attr-defined]
        return tuple(self._to_card(record) for record in records), complete

    def _to_card(self, record: ProviderRecord) -> PropertyCard:
        """Read one Trello card back into the canonical shape."""

        stored = record.fields.get(_DESC_FIELD)
        if type(stored) is str and stored.startswith("{"):
            try:
                return parse_card({**json.loads(stored), "card_id": record.source_id})
            except (ValueError, json.JSONDecodeError):
                pass
        name = record.fields.get(_NAME_FIELD)
        card = PropertyCard.new(record.source_id)
        if type(name) is str and name:
            card = card.with_field(
                name="address",
                value=FieldValue(
                    value=name,
                    source="trello",
                    source_id=record.source_id,
                    retrieved_at=record.retrieved_at.isoformat(),
                    authority=Authority.PROVIDER,
                ),
            )
        return card

    # ---- creating ------------------------------------------------------

    def create(self, actor: Principal, card: PropertyCard) -> EffectOutcome:
        """Create one property card, at most once, and prove it landed."""

        try:
            key = dedupe_key(card)
        except ValueError as exc:
            return EffectOutcome(EffectStatus.REFUSED, "", reason=str(exc))

        existing, complete = self._existing()
        if not complete:
            # Refusing is the only honest answer: the duplicate this would
            # create might be sitting in the part of the board we did not read.
            return EffectOutcome(
                EffectStatus.REFUSED,
                "",
                reason="this board is larger than one read, so the whole board could not be "
                "checked for an existing card; narrow the board or archive old cards first",
            )
        matches = find_duplicates(card, existing)
        record, claimed = self.effects.claim(
            operation="create",
            key=key,
            actor=actor,
            payload_hash=card.payload_hash(),
            source_revision=self.source_revision,
        )
        if not claimed:
            # This property was already attempted. Whatever happened to that
            # attempt, the answer is never a second card.
            return self._reconcile_create(record, card, existing, matches)
        if matches:
            self.effects.settle(
                record.effect_id,
                EffectStatus.DUPLICATE,
                receipt={"duplicates": [result.as_json() for _, result in matches]},
            )
            return EffectOutcome(
                EffectStatus.DUPLICATE,
                record.effect_id,
                card_id=matches[0][0].card_id,
                reason="this property already has a card",
                duplicates=tuple(result.as_json() for _, result in matches),
            )
        return self._perform_create(record, card)

    def _perform_create(self, record: EffectRecord, card: PropertyCard) -> EffectOutcome:
        payload = self._payload(card)
        try:
            created = self.trello.create_card(self.list_id, payload)  # type: ignore[attr-defined]
        except AmbiguousEffectError as exc:
            self.effects.settle(record.effect_id, EffectStatus.UNKNOWN, receipt={"note": str(exc)})
            return EffectOutcome(
                EffectStatus.UNKNOWN,
                record.effect_id,
                reason="the card may or may not have been created; reconcile before retry",
            )
        except ProviderError as exc:
            self.effects.settle(record.effect_id, EffectStatus.FAILED, receipt={"note": str(exc)})
            return EffectOutcome(EffectStatus.FAILED, record.effect_id, reason=str(exc))
        return self._verify(record, created.source_id, card, changed=tuple(card.fields))

    def _reconcile_create(
        self,
        record: EffectRecord,
        card: PropertyCard,
        existing: Sequence[PropertyCard],
        matches: Sequence[tuple[PropertyCard, DuplicateResult]],
    ) -> EffectOutcome:
        """Work out what an earlier attempt actually did, and report that."""

        evidence = tuple(result.as_json() for _, result in matches)
        if record.card_id:
            return EffectOutcome(
                EffectStatus.DUPLICATE,
                record.effect_id,
                card_id=record.card_id,
                reason="this property already has a card",
                reconciled=True,
                duplicates=evidence,
            )
        landed = [item for item, _ in matches] or [
            item for item in existing if duplicate_score(card, item).duplicate
        ]
        if landed:
            # The earlier attempt did land; the reply was simply lost.
            self.effects.settle(
                record.effect_id,
                EffectStatus.VERIFIED,
                card_id=landed[0].card_id,
                receipt={"note": "reconciled a lost acknowledgement"},
            )
            return EffectOutcome(
                EffectStatus.VERIFIED,
                record.effect_id,
                card_id=landed[0].card_id,
                reason="the earlier attempt had already created this card",
                reconciled=True,
            )
        return self._perform_create(record, card)

    # ---- updating ------------------------------------------------------

    def update(
        self,
        actor: Principal,
        card_id: str,
        card: PropertyCard,
        *,
        existing: PropertyCard | None = None,
    ) -> EffectOutcome:
        """Change one card's canonical values and prove the change landed."""

        before = existing if existing is not None else self._read(card_id)
        if before is None:
            return EffectOutcome(EffectStatus.REFUSED, "", reason="that card could not be read")
        diff = compare(before, card)
        changed = tuple(sorted({*diff.conflicts, *diff.additions}))
        if not changed:
            return EffectOutcome(
                EffectStatus.VERIFIED, "", card_id=card_id, reason="nothing needed to change"
            )
        record, claimed = self.effects.claim(
            operation="update",
            key=f"{card_id}:{card.payload_hash()}",
            actor=actor,
            payload_hash=card.payload_hash(),
            source_revision=self.source_revision,
        )
        if not claimed and record.status is EffectStatus.UNKNOWN:
            # This exact change was attempted and its outcome was never
            # established. Repeating it is how an ambiguous effect becomes a
            # doubled one, so it is reconciled against the card instead.
            return self._reconcile_change(record, card_id, card, changed)
        try:
            self.trello.update_card(card_id, self._payload(card))  # type: ignore[attr-defined]
        except AmbiguousEffectError as exc:
            self.effects.settle(record.effect_id, EffectStatus.UNKNOWN, receipt={"note": str(exc)})
            return EffectOutcome(
                EffectStatus.UNKNOWN,
                record.effect_id,
                card_id=card_id,
                reason="the update may or may not have applied; reconcile before retry",
            )
        except ProviderError as exc:
            self.effects.settle(record.effect_id, EffectStatus.FAILED, receipt={"note": str(exc)})
            return EffectOutcome(EffectStatus.FAILED, record.effect_id, reason=str(exc))
        return self._verify(record, card_id, card, changed=changed)

    def _reconcile_change(
        self,
        record: EffectRecord,
        card_id: str,
        intended: PropertyCard,
        changed: tuple[str, ...],
    ) -> EffectOutcome:
        """Establish what an unresolved earlier change actually did."""

        observed = self._read(card_id)
        if observed is None:
            return EffectOutcome(
                EffectStatus.UNKNOWN,
                record.effect_id,
                card_id=card_id,
                reason="the card still cannot be read; reconcile before retry",
            )
        diff = compare(intended, observed)
        if diff.conflicts or diff.only_left:
            return EffectOutcome(
                EffectStatus.UNKNOWN,
                record.effect_id,
                card_id=card_id,
                reason="an earlier change to this card is unresolved; reconcile before retry",
            )
        self.effects.settle(
            record.effect_id,
            EffectStatus.VERIFIED,
            card_id=card_id,
            receipt={
                "changed_fields": list(changed),
                "note": "reconciled a lost acknowledgement",
            },
        )
        return EffectOutcome(
            EffectStatus.VERIFIED, record.effect_id, card_id=card_id, reconciled=True
        )

    def routine(
        self, actor: Principal, operation: str, card_id: str, arguments: Mapping[str, object]
    ) -> EffectOutcome:
        """Run one routine operation. A consequence never comes through here."""

        if operation in CONSEQUENCE_OPERATIONS:
            raise PermissionError(f"{operation} needs an approved proposal")
        if operation not in ROUTINE_OPERATIONS:
            raise ValueError("property-card operation is not permitted")
        if operation == "move":
            destination = arguments.get("list_id")
            if type(destination) is not str or not destination:
                raise ValueError("a move needs a configured destination list")
            record, claimed = self.effects.claim(
                operation="move",
                key=f"{card_id}:{destination}",
                actor=actor,
                payload_hash=destination,
                source_revision=self.source_revision,
            )
            if not claimed and record.status is EffectStatus.UNKNOWN:
                observed = self.trello.get_card(card_id)  # type: ignore[attr-defined]
                if observed.fields.get("idList") == destination:
                    # The earlier attempt did land; only its reply was lost.
                    self.effects.settle(
                        record.effect_id,
                        EffectStatus.VERIFIED,
                        card_id=card_id,
                        receipt={"note": "reconciled a lost acknowledgement"},
                    )
                    return EffectOutcome(
                        EffectStatus.VERIFIED,
                        record.effect_id,
                        card_id=card_id,
                        reconciled=True,
                    )
                return EffectOutcome(
                    EffectStatus.UNKNOWN,
                    record.effect_id,
                    card_id=card_id,
                    reason="an earlier move is unresolved; reconcile before retry",
                )
            try:
                self.trello.move_card(card_id, destination)  # type: ignore[attr-defined]
            except AmbiguousEffectError as exc:
                self.effects.settle(
                    record.effect_id, EffectStatus.UNKNOWN, receipt={"note": str(exc)}
                )
                return EffectOutcome(
                    EffectStatus.UNKNOWN,
                    record.effect_id,
                    card_id=card_id,
                    reason="the move may or may not have applied; reconcile before retry",
                )
            moved = self.trello.get_card(card_id)  # type: ignore[attr-defined]
            landed = moved.fields.get("idList") == destination
            status = EffectStatus.VERIFIED if landed else EffectStatus.UNKNOWN
            self.effects.settle(
                record.effect_id,
                status,
                card_id=card_id,
                receipt={"changed_fields": ["idList"], "to": destination},
            )
            return EffectOutcome(status, record.effect_id, card_id=card_id)
        raise ValueError("property-card operation is not permitted")

    # ---- verification --------------------------------------------------

    def _read(self, card_id: str) -> PropertyCard | None:
        try:
            return self._to_card(self.trello.get_card(card_id))  # type: ignore[attr-defined]
        except (ProviderError, AmbiguousEffectError):
            return None

    def _verify(
        self,
        record: EffectRecord,
        card_id: str,
        intended: PropertyCard,
        *,
        changed: tuple[str, ...],
    ) -> EffectOutcome:
        """Read the card back and accept the effect only on an exact match."""

        observed = self._read(card_id)
        if observed is None:
            self.effects.settle(
                record.effect_id,
                EffectStatus.UNKNOWN,
                card_id=card_id,
                receipt={"note": "the card could not be read back"},
            )
            return EffectOutcome(
                EffectStatus.UNKNOWN,
                record.effect_id,
                card_id=card_id,
                reason="the card could not be read back; reconcile before retry",
            )
        diff = compare(intended, observed)
        if diff.conflicts or diff.only_left:
            self.effects.settle(
                record.effect_id,
                EffectStatus.UNKNOWN,
                card_id=card_id,
                receipt={
                    "changed_fields": list(changed),
                    "note": "readback does not match the intended state",
                    "diff": diff.as_json(),
                },
            )
            return EffectOutcome(
                EffectStatus.UNKNOWN,
                record.effect_id,
                card_id=card_id,
                reason="the card does not read back as intended; reconcile before retry",
            )
        self.effects.settle(
            record.effect_id,
            EffectStatus.VERIFIED,
            card_id=card_id,
            receipt={"changed_fields": list(changed), "diff": diff.as_json()},
        )
        return EffectOutcome(EffectStatus.VERIFIED, record.effect_id, card_id=card_id)

    # ---- presentation --------------------------------------------------

    def _payload(self, card: PropertyCard) -> dict[str, object]:
        """The Trello body for one card: a title, and the canonical record."""

        address = card.fields.get("address")
        return {
            _NAME_FIELD: address.value if address is not None else card.card_id,
            _DESC_FIELD: json.dumps(card.as_json(), sort_keys=True, separators=(",", ":")),
        }

    def reformat(self, card: PropertyCard) -> PropertyCard:
        """Rewrite a card into the current shape, losing nothing it knew.

        Reformatting is presentation only. Every stored value keeps its own
        value, source, and authority, so a tidier card is not a weaker one.
        """

        rebuilt = PropertyCard.new(card.card_id)
        for name, value in card.fields.items():
            rebuilt = rebuilt.with_field(name, value)
        return rebuilt

    def apply_template(self, card: PropertyCard, template: Mapping[str, str]) -> PropertyCard:
        """Fill only what the card does not already know.

        A template is a default, not an instruction: it never replaces a value
        someone verified, and it says so through its own authority.
        """

        updated = card
        for name, text in template.items():
            if name in card.fields:
                continue
            updated = updated.with_field(
                name,
                FieldValue(
                    value=text,
                    source="template",
                    source_id="",
                    retrieved_at=_now(),
                    authority=Authority.CONFIGURED,
                ),
            )
        return updated

    # ---- bulk ----------------------------------------------------------

    def dry_run(
        self,
        actor: Principal,
        operation: str,
        card_ids: Sequence[str],
        arguments: Mapping[str, object],
    ) -> BulkPlan:
        """Say exactly what a batch would change, without changing anything."""

        del actor
        if operation not in {"move", "update", "label"}:
            raise ValueError("bulk property-card operation is not permitted")
        if not card_ids or len(card_ids) > MAX_BULK_CARDS:
            raise ValueError(f"a bulk review covers 1 to {MAX_BULK_CARDS} cards")
        changes: list[Mapping[str, object]] = []
        unreadable: list[str] = []
        for card_id in card_ids:
            observed = self._read(card_id)
            if observed is None:
                unreadable.append(card_id)
                continue
            if operation == "move":
                destination = arguments.get("list_id")
                changes.append({"card_id": card_id, "field": "list", "to": destination})
            else:
                changes.append(
                    {
                        "card_id": card_id,
                        "field": "fields",
                        "to": {
                            name: str(value)
                            for name, value in arguments.items()
                            if name != "list_id"
                        },
                    }
                )
        return BulkPlan(
            operation=operation,
            affected=len(changes),
            changes=tuple(changes),
            unreadable=tuple(unreadable),
        )


__all__ = [
    "CONSEQUENCE_OPERATIONS",
    "MAX_BULK_CARDS",
    "ROUTINE_OPERATIONS",
    "BulkPlan",
    "EffectLog",
    "EffectOutcome",
    "EffectRecord",
    "EffectStatus",
    "PropertyCardEngine",
    "consequence_operations",
]
