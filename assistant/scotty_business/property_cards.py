"""The canonical wholesaling property card.

The same property arrives from a conversation, a Trello card, a RentCast lookup
and a GHL record, spelled differently every time. This module is the one place
that decides what a property record *is*: which fields exist, who is allowed to
write each of them, when two records are the same property, and what happens
when two records disagree.

Three rules run through all of it. A value carries its provenance, so nothing is
anonymous. A weaker source never overwrites a stronger one, so a provider guess
cannot quietly replace something a person verified. And a real disagreement is
preserved and surfaced rather than resolved automatically: a merge that would
lose a value refuses to commit until someone chooses.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import IntEnum

#: Bumped whenever the stored shape changes. Older cards are migrated on read.
CARD_SCHEMA_VERSION = 2


class ConflictError(ValueError):
    """A merge cannot proceed because a real disagreement is unresolved."""


class Authority(IntEnum):
    """Who may overwrite whom. Higher wins; equal authority defers to the clock."""

    #: The model's own reading of a conversation. Never overwrites anything else.
    INFERRED = 10
    #: A value read from a provider record.
    PROVIDER = 20
    #: A value a client user stated or confirmed.
    VERIFIED = 30
    #: A value fixed by the deployment's configuration.
    CONFIGURED = 40


#: Every field a property card may carry, and nothing else. An unknown field is
#: refused rather than stored, so a model cannot invent a place to put data.
CARD_FIELDS: tuple[str, ...] = (
    "address",
    "parcel_id",
    "county",
    "property_type",
    "beds",
    "baths",
    "square_feet",
    "year_built",
    "lot_size",
    "asking_price",
    "arv",
    "repair_estimate",
    "offer_price",
    "assignment_fee",
    "seller_name",
    "seller_stage",
    "lead_source",
    "occupancy",
    "condition_notes",
    "next_step",
    "rentcast_id",
    "ghl_contact_id",
    "trello_card_id",
)

_REQUIRED_FIELDS: tuple[str, ...] = ("address",)

MAX_VALUE_CHARS = 2_000


@dataclass(frozen=True, slots=True)
class FieldValue:
    """One field's value, and where it came from."""

    value: str
    source: str
    source_id: str
    retrieved_at: str
    authority: Authority

    def as_json(self) -> dict[str, object]:
        return {
            "value": self.value,
            "source": self.source,
            "source_id": self.source_id,
            "retrieved_at": self.retrieved_at,
            "authority": int(self.authority),
        }

    def beats(self, other: FieldValue) -> bool:
        """Whether this value may replace `other`.

        Stronger authority always wins. Equal authority is decided by recency,
        and a tie keeps what is already stored so repeated writes are stable.
        """

        if self.authority != other.authority:
            return self.authority > other.authority
        return self.retrieved_at > other.retrieved_at


_DIRECTIONS = {
    "NORTH": "N",
    "SOUTH": "S",
    "EAST": "E",
    "WEST": "W",
    "NORTHEAST": "NE",
    "NORTHWEST": "NW",
    "SOUTHEAST": "SE",
    "SOUTHWEST": "SW",
}

_STREET_TYPES = {
    "STREET": "ST",
    "AVENUE": "AVE",
    "ROAD": "RD",
    "DRIVE": "DR",
    "LANE": "LN",
    "COURT": "CT",
    "BOULEVARD": "BLVD",
    "PLACE": "PL",
    "TERRACE": "TER",
    "CIRCLE": "CIR",
    "PARKWAY": "PKWY",
    "HIGHWAY": "HWY",
    "TRAIL": "TRL",
    "WAY": "WAY",
}

_UNIT_WORDS = {"APARTMENT", "APT", "UNIT", "SUITE", "STE", "#"}

_STATES = {
    "ALABAMA": "AL",
    "ALASKA": "AK",
    "ARIZONA": "AZ",
    "ARKANSAS": "AR",
    "CALIFORNIA": "CA",
    "COLORADO": "CO",
    "CONNECTICUT": "CT",
    "DELAWARE": "DE",
    "FLORIDA": "FL",
    "GEORGIA": "GA",
    "HAWAII": "HI",
    "IDAHO": "ID",
    "ILLINOIS": "IL",
    "INDIANA": "IN",
    "IOWA": "IA",
    "KANSAS": "KS",
    "KENTUCKY": "KY",
    "LOUISIANA": "LA",
    "MAINE": "ME",
    "MARYLAND": "MD",
    "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI",
    "MINNESOTA": "MN",
    "MISSISSIPPI": "MS",
    "MISSOURI": "MO",
    "MONTANA": "MT",
    "NEBRASKA": "NE",
    "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM",
    "NEW YORK": "NY",
    "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND",
    "OHIO": "OH",
    "OKLAHOMA": "OK",
    "OREGON": "OR",
    "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN",
    "TEXAS": "TX",
    "UTAH": "UT",
    "VERMONT": "VT",
    "VIRGINIA": "VA",
    "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI",
    "WYOMING": "WY",
}
_STATE_CODES = frozenset(_STATES.values())

_POSTAL = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_NUMBER = re.compile(r"^(\d+[A-Z]?)\b")


@dataclass(frozen=True, slots=True)
class Address:
    """A parsed, standardized address, kept in parts rather than as one string."""

    number: str = ""
    street: str = ""
    unit: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    raw: str = ""
    problems: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether enough is present to identify a property."""

        return bool(
            self.number and self.street and (self.postal_code or (self.city and self.state))
        )

    def key(self) -> str:
        """The comparison key. Two spellings of one address share it exactly."""

        return "|".join(
            (
                self.number,
                self.street,
                self.unit,
                self.postal_code or f"{self.city},{self.state}",
            )
        )

    def as_json(self) -> dict[str, object]:
        return {
            "number": self.number,
            "street": self.street,
            "unit": self.unit,
            "city": self.city,
            "state": self.state,
            "postal_code": self.postal_code,
            "complete": self.complete,
            "problems": list(self.problems),
        }


def _words(text: str) -> list[str]:
    return [word for word in re.split(r"[\s,]+", text) if word]


def _expand_state(tokens: list[str]) -> tuple[str, list[str]]:
    """Pull a state out of the tail, accepting a code or a full name."""

    if len(tokens) >= 2:
        pair = f"{tokens[-2]} {tokens[-1]}"
        if pair in _STATES:
            return _STATES[pair], tokens[:-2]
    if tokens:
        last = tokens[-1]
        if last in _STATE_CODES:
            return last, tokens[:-1]
        if last in _STATES:
            return _STATES[last], tokens[:-1]
    return "", tokens


def normalize_address(text: object) -> Address:
    """Parse one address into standardized parts, reporting what it could not read.

    The parser is deliberately conservative: it standardizes the spellings it
    knows and says what it could not identify rather than inventing a component.
    A property is only ever matched on parts it actually read.
    """

    if type(text) is not str or not text.strip():
        return Address(raw="", problems=("an address is required",))
    raw = text.strip()
    upper = re.sub(r"\s+", " ", raw.upper().replace(".", ""))

    postal = ""
    match = _POSTAL.search(upper)
    if match:
        postal = match.group(1)
        upper = (upper[: match.start()] + " " + upper[match.end() :]).strip()

    unit = ""
    unit_match = re.search(r"(?:#\s*|\b(?:APARTMENT|APT|UNIT|SUITE|STE)\s+)([A-Z0-9-]+)", upper)
    if unit_match:
        unit = unit_match.group(1)
        upper = (upper[: unit_match.start()] + " " + upper[unit_match.end() :]).strip()

    segments = [segment.strip() for segment in upper.split(",") if segment.strip()]
    tokens = _words(segments[0]) if segments else []
    tail = _words(" ".join(segments[1:])) if len(segments) > 1 else []

    state, tail = _expand_state(tail)
    if not state:
        state, tokens = _expand_state(tokens)

    problems: list[str] = []
    number = ""
    number_match = _NUMBER.match(" ".join(tokens))
    if number_match:
        number = number_match.group(1)
        tokens = tokens[1:]
    else:
        problems.append("no house number was found")

    standardized: list[str] = []
    for word in tokens:
        if word in _UNIT_WORDS:
            continue
        standardized.append(_DIRECTIONS.get(word, _STREET_TYPES.get(word, word)))
    # "North West" and "NW" are the same direction, so adjacent single-letter
    # directions are joined before anything is compared.
    joined: list[str] = []
    for word in standardized:
        if (
            joined
            and word in {"N", "S", "E", "W"}
            and joined[-1] in {"N", "S"}
            and f"{joined[-1]}{word}" in set(_DIRECTIONS.values())
        ):
            joined[-1] = f"{joined[-1]}{word}"
            continue
        joined.append(word)
    standardized = joined
    city_words = tail
    if not city_words and len(standardized) > 2:
        # No comma separated the city, so the trailing words are the city only
        # when a street type marks where the street name ended.
        for index in range(len(standardized) - 1, 0, -1):
            if standardized[index] in set(_STREET_TYPES.values()):
                city_words = standardized[index + 1 :]
                standardized = standardized[: index + 1]
                break
    street = " ".join(standardized)
    city = " ".join(city_words)
    if not street:
        problems.append("no street was found")
    if not postal and not (city and state):
        problems.append("no postal code, city, or state was found")
    return Address(
        number=number,
        street=street,
        unit=unit,
        city=city,
        state=state,
        postal_code=postal,
        raw=raw,
        problems=tuple(problems),
    )


@dataclass(frozen=True, slots=True)
class PropertyCard:
    """One property, as this deployment knows it."""

    card_id: str
    fields: Mapping[str, FieldValue] = field(default_factory=dict)
    schema_version: int = CARD_SCHEMA_VERSION

    @classmethod
    def new(cls, card_id: str) -> PropertyCard:
        if type(card_id) is not str or not card_id:
            raise ValueError("a property card needs an identifier")
        return cls(card_id=card_id, fields={})

    def with_field(self, name: str, value: FieldValue) -> PropertyCard:
        """Set one field, refusing an unknown name or an oversized value."""

        if name not in CARD_FIELDS:
            raise ValueError(f"{name} is not a property-card field")
        if not isinstance(value, FieldValue):
            raise ValueError("a property-card value must carry its provenance")
        if type(value.value) is not str or len(value.value) > MAX_VALUE_CHARS:
            raise ValueError(f"{name} must be text of at most {MAX_VALUE_CHARS} characters")
        return replace(self, fields={**self.fields, name: value})

    def apply(
        self, *updates: tuple[str, FieldValue]
    ) -> tuple[PropertyCard, tuple[tuple[str, str], ...]]:
        """Apply updates that are allowed to win, and say what was rejected.

        A rejection is not an error: it is the record refusing to let a weaker
        source overwrite a stronger one, and it is reported so the assistant can
        explain why the number did not change.
        """

        card = self
        rejected: list[tuple[str, str]] = []
        for name, value in updates:
            if name not in CARD_FIELDS:
                raise ValueError(f"{name} is not a property-card field")
            current = card.fields.get(name)
            if current is not None and not value.beats(current):
                rejected.append(
                    (
                        name,
                        f"kept the {current.source} value; {value.source} does not outrank it",
                    )
                )
                continue
            card = card.with_field(name, value)
        return card, tuple(rejected)

    def address(self) -> Address:
        stored = self.fields.get("address")
        return normalize_address(stored.value if stored is not None else "")

    def missing_required(self) -> tuple[str, ...]:
        return tuple(name for name in _REQUIRED_FIELDS if name not in self.fields)

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "card_id": self.card_id,
            "fields": {name: value.as_json() for name, value in sorted(self.fields.items())},
        }

    def payload_hash(self) -> str:
        """A stable hash of exactly this card's content."""

        return hashlib.sha256(
            json.dumps(self.as_json(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def parse_card(stored: object) -> PropertyCard:
    """Read a stored card, migrating an older schema rather than refusing it."""

    if not isinstance(stored, Mapping):
        raise ValueError("a stored property card must be an object")
    card_id = stored.get("card_id")
    if type(card_id) is not str or not card_id:
        raise ValueError("a stored property card needs an identifier")
    raw_fields = stored.get("fields", {})
    if not isinstance(raw_fields, Mapping):
        raise ValueError("stored property-card fields must be an object")
    card = PropertyCard.new(card_id)
    for name, entry in raw_fields.items():
        if name not in CARD_FIELDS or not isinstance(entry, Mapping):
            # Version 1 allowed fields this version does not. Dropping an
            # unknown field is the migration: nothing invents a place for it.
            continue
        try:
            card = card.with_field(
                name,
                FieldValue(
                    value=str(entry["value"]),
                    source=str(entry.get("source", "unknown")),
                    source_id=str(entry.get("source_id", "")),
                    retrieved_at=str(entry.get("retrieved_at", "")),
                    authority=Authority(int(entry.get("authority", Authority.PROVIDER))),
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"stored property-card field {name} is malformed") from exc
    return card


@dataclass(frozen=True, slots=True)
class DuplicateResult:
    """Why two cards are, or are not, the same property."""

    confidence: float
    duplicate: bool
    reasons: tuple[str, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "confidence": round(self.confidence, 3),
            "duplicate": self.duplicate,
            "reasons": list(self.reasons),
        }


#: What each kind of agreement is worth, and the score at which two cards are
#: treated as the same property. Both are configurable rather than implicit.
DUPLICATE_EVIDENCE: Mapping[str, float] = {
    "parcel_id": 0.95,
    "address": 0.9,
    "rentcast_id": 0.9,
    "ghl_contact_id": 0.4,
    "street_and_postal": 0.6,
    "street_and_number": 0.3,
    "seller_name": 0.15,
}
DUPLICATE_THRESHOLD = 0.85


def _shared(left: PropertyCard, right: PropertyCard, name: str) -> str | None:
    first, second = left.fields.get(name), right.fields.get(name)
    if first is None or second is None:
        return None
    if first.value.strip().casefold() != second.value.strip().casefold():
        return None
    return first.value.strip()


def duplicate_score(
    left: PropertyCard,
    right: PropertyCard,
    *,
    threshold: float = DUPLICATE_THRESHOLD,
    evidence: Mapping[str, float] | None = None,
) -> DuplicateResult:
    """Score whether two cards describe one property, and explain the score.

    Every contribution is named, so a user can see exactly why two cards were
    or were not treated as the same property, and disagree with it.
    """

    weights = dict(DUPLICATE_EVIDENCE)
    weights.update(evidence or {})
    reasons: list[str] = []
    best = 0.0

    for name in ("parcel_id", "rentcast_id", "ghl_contact_id", "seller_name"):
        if _shared(left, right, name) is not None:
            weight = weights.get(name, 0.0)
            reasons.append(f"the same {name.replace('_', ' ')} ({weight:.2f})")
            best = max(best, weight)

    left_address, right_address = left.address(), right.address()
    if left_address.complete and right_address.complete:
        if left_address.key() == right_address.key():
            weight = weights.get("address", 0.0)
            reasons.append(f"the same normalized address ({weight:.2f})")
            best = max(best, weight)
        elif (
            left_address.number == right_address.number
            and left_address.street == right_address.street
            and left_address.postal_code == right_address.postal_code
            and left_address.unit != right_address.unit
        ):
            reasons.append("the same street address but a different unit")
        elif (
            left_address.street == right_address.street
            and left_address.postal_code
            and left_address.postal_code == right_address.postal_code
        ):
            weight = weights.get("street_and_postal", 0.0)
            reasons.append(f"the same street and postal code ({weight:.2f})")
            best = max(best, weight)
        elif (
            left_address.number == right_address.number
            and left_address.street == right_address.street
        ):
            # The same street address in a different postal area is weak
            # evidence: it is often a typo, and sometimes a different property.
            weight = weights.get("street_and_number", 0.0)
            reasons.append(f"the same house number and street ({weight:.2f})")
            best = max(best, weight)
        else:
            reasons.append("different addresses")
    else:
        reasons.append("at least one address could not be read")

    if not reasons:  # pragma: no cover - address always contributes a reason
        reasons.append("no comparable identifiers")
    return DuplicateResult(
        confidence=min(best, 1.0),
        duplicate=best >= threshold,
        reasons=tuple(sorted(reasons)),
    )


@dataclass(frozen=True, slots=True)
class CardDiff:
    """What two cards agree on, disagree on, and each know alone."""

    agreements: tuple[str, ...]
    conflicts: tuple[str, ...]
    additions: tuple[str, ...]
    only_left: tuple[str, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "agreements": list(self.agreements),
            "conflicts": list(self.conflicts),
            "additions": list(self.additions),
            "only_left": list(self.only_left),
        }


def _comparable(name: str, value: FieldValue) -> str:
    if name == "address":
        return normalize_address(value.value).key()
    return value.value.strip().casefold()


def compare(left: PropertyCard, right: PropertyCard) -> CardDiff:
    """Compare two cards field by field, by meaning rather than by spelling."""

    agreements: list[str] = []
    conflicts: list[str] = []
    additions: list[str] = []
    only_left: list[str] = []
    for name in CARD_FIELDS:
        first, second = left.fields.get(name), right.fields.get(name)
        if first is None and second is None:
            continue
        if first is None:
            additions.append(name)
        elif second is None:
            only_left.append(name)
        elif _comparable(name, first) == _comparable(name, second):
            agreements.append(name)
        else:
            conflicts.append(name)
    return CardDiff(
        agreements=tuple(agreements),
        conflicts=tuple(conflicts),
        additions=tuple(additions),
        only_left=tuple(only_left),
    )


@dataclass(frozen=True, slots=True)
class MergePreview:
    """A merge that has not happened yet, and what it still needs.

    Everything both cards agree on, and everything only one of them knows, is
    already resolved. A genuine disagreement stays unresolved until someone
    chooses a side, so a merge can never quietly drop a value.
    """

    left: PropertyCard
    right: PropertyCard
    diff: CardDiff
    result: PropertyCard
    unresolved: tuple[str, ...]
    choices: Mapping[str, str] = field(default_factory=dict)

    @property
    def payload_hash(self) -> str:
        """Stable over the same inputs and the same choices."""

        return hashlib.sha256(
            json.dumps(
                {
                    "left": self.left.as_json(),
                    "right": self.right.as_json(),
                    "choices": dict(sorted(self.choices.items())),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def choose(self, name: str, side: str) -> MergePreview:
        """Resolve exactly one conflict, by naming the side to keep."""

        if name not in self.unresolved:
            raise ConflictError(f"{name} is not an unresolved conflict")
        if side not in {"left", "right"}:
            raise ConflictError("a conflict is resolved by choosing left or right")
        chosen = dict(self.choices)
        chosen[name] = side
        source = self.left if side == "left" else self.right
        value = source.fields[name]
        return replace(
            self,
            result=self.result.with_field(name, value),
            unresolved=tuple(item for item in self.unresolved if item != name),
            choices=chosen,
        )

    def commit(self) -> PropertyCard:
        """Produce the merged card, refusing while a conflict is unresolved."""

        if self.unresolved:
            raise ConflictError(
                "these fields disagree and need an explicit choice: " + ", ".join(self.unresolved)
            )
        return self.result

    def as_json(self) -> dict[str, object]:
        return {
            "diff": self.diff.as_json(),
            "unresolved": list(self.unresolved),
            "choices": dict(sorted(self.choices.items())),
            "payload_hash": self.payload_hash,
            "result": self.result.as_json(),
        }


def merge_preview(left: PropertyCard, right: PropertyCard) -> MergePreview:
    """Plan a merge of two cards without performing it.

    The preview is deterministic: the same two cards always produce the same
    result and the same hash, so a user approves exactly what will happen.
    """

    diff = compare(left, right)
    merged = left
    for name in diff.additions:
        merged = merged.with_field(name, right.fields[name])
    for name in diff.agreements:
        first, second = left.fields[name], right.fields[name]
        # Identical values still have provenance; keep the stronger one.
        merged = merged.with_field(name, second if second.beats(first) else first)
    return MergePreview(
        left=left,
        right=right,
        diff=diff,
        result=merged,
        unresolved=diff.conflicts,
        choices={},
    )


def find_duplicates(
    candidate: PropertyCard,
    existing: Iterable[PropertyCard],
    *,
    threshold: float = DUPLICATE_THRESHOLD,
) -> tuple[tuple[PropertyCard, DuplicateResult], ...]:
    """Every existing card that looks like the candidate, most likely first."""

    scored = [
        (card, duplicate_score(candidate, card, threshold=threshold))
        for card in existing
        if card.card_id != candidate.card_id
    ]
    matches = [(card, result) for card, result in scored if result.duplicate]
    matches.sort(key=lambda pair: (-pair[1].confidence, pair[0].card_id))
    return tuple(matches)


def dedupe_key(card: PropertyCard) -> str:
    """A stable key for one property, used to avoid creating it twice.

    Derived from what actually identifies a property, so the same property
    described twice produces the same key even after a lost acknowledgement.
    """

    parcel = card.fields.get("parcel_id")
    if parcel is not None and parcel.value.strip():
        material = f"parcel:{parcel.value.strip().casefold()}"
    else:
        address = card.address()
        if not address.complete:
            raise ValueError("a property needs a readable address or a parcel id")
        material = f"address:{address.key()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "CARD_FIELDS",
    "CARD_SCHEMA_VERSION",
    "DUPLICATE_EVIDENCE",
    "DUPLICATE_THRESHOLD",
    "Address",
    "Authority",
    "CardDiff",
    "ConflictError",
    "DuplicateResult",
    "FieldValue",
    "MergePreview",
    "PropertyCard",
    "compare",
    "dedupe_key",
    "duplicate_score",
    "find_duplicates",
    "merge_preview",
    "normalize_address",
    "parse_card",
]
