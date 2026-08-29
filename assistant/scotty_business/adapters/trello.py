from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from ..config import TrelloScope
from .http import (
    AmbiguousEffectError,
    ProviderError,
    RedactedMapping,
    Transport,
    fixed_id,
    require_success,
)
from .records import ProviderRecord, utc_now

_BASE = "https://api.trello.com/1"
_READ_FIELDS = (
    "id",
    "idBoard",
    "idList",
    "name",
    "desc",
    "closed",
    "due",
    "dueComplete",
    "idLabels",
    "customFieldItems",
    "dateLastActivity",
)
_UPDATE_FIELDS = frozenset({"name", "desc", "due", "dueComplete", "idLabels"})


class TrelloAdapter:
    api_version = "1"

    def __init__(self, transport: Transport, key: str, token: str, scope: TrelloScope):
        if not key or not token:
            raise ProviderError("Trello credentials are not configured")
        self.transport = transport
        self.scope = scope
        self._auth = RedactedMapping(key=key, token=token)

    def _query(self, values: Mapping[str, object] | None = None) -> RedactedMapping:
        query = RedactedMapping(self._auth)
        for key, value in (values or {}).items():
            if isinstance(value, bool):
                query[key] = "true" if value else "false"
            elif isinstance(value, list):
                query[key] = ",".join(str(item) for item in value)
            elif value is not None:
                query[key] = str(value)
        return query

    def _record(self, body: object, *, retrieved_at: datetime | None = None) -> ProviderRecord:
        if not isinstance(body, dict):
            raise ProviderError("Trello card response must be an object")
        card_id = fixed_id(body.get("id"), "card id")
        if body.get("idBoard") != self.scope.board_id:
            raise ProviderError("Trello card is outside the configured board")
        if body.get("idList") not in self.scope.list_ids:
            raise ProviderError("Trello card is outside configured lists")
        revision = body.get("dateLastActivity")
        if type(revision) is not str or not revision:
            revision = "unversioned"
        missing = tuple(field for field in _READ_FIELDS if field not in body)
        return ProviderRecord(
            provider="trello",
            source_id=card_id,
            retrieved_at=retrieved_at or utc_now(),
            source_revision=revision,
            fields=dict(body),
            missing_attributes=missing,
        )

    def _mutation_record(self, body: object) -> ProviderRecord:
        try:
            return self._record(body)
        except ProviderError as exc:
            raise AmbiguousEffectError(
                "Trello mutation acknowledgement is malformed; reconcile before retry"
            ) from exc

    def get_card(self, card_id: str, *, retrieved_at: datetime | None = None) -> ProviderRecord:
        card = fixed_id(card_id, "card id")
        response = self.transport.request(
            "GET",
            f"{_BASE}/cards/{card}",
            query=self._query({"fields": list(_READ_FIELDS), "customFieldItems": True}),
        )
        return self._record(require_success(response), retrieved_at=retrieved_at)

    def list_cards(self) -> tuple[ProviderRecord, ...]:
        response = self.transport.request(
            "GET",
            f"{_BASE}/boards/{fixed_id(self.scope.board_id, 'board id')}/cards",
            query=self._query({"fields": list(_READ_FIELDS), "customFieldItems": True}),
        )
        body = require_success(response)
        if not isinstance(body, list):
            raise ProviderError("Trello cards response must be a list")
        return tuple(self._record(card) for card in body)

    def create_card(self, list_id: str, fields: Mapping[str, object]) -> ProviderRecord:
        if list_id not in self.scope.list_ids:
            raise ProviderError("Trello destination list is not configured")
        allowed = {"name", "desc", "due", "idLabels"}
        self._validate_fields(fields, allowed)
        response = self.transport.request(
            "POST", f"{_BASE}/cards", query=self._query({"idList": list_id, **fields})
        )
        return self._mutation_record(require_success(response, expected=(200, 201)))

    def update_card(self, card_id: str, fields: Mapping[str, object]) -> ProviderRecord:
        self._validate_fields(fields, _UPDATE_FIELDS)
        response = self.transport.request(
            "PUT",
            f"{_BASE}/cards/{fixed_id(card_id, 'card id')}",
            query=self._query(fields),
        )
        return self._mutation_record(require_success(response))

    def move_card(self, card_id: str, list_id: str) -> ProviderRecord:
        if list_id not in self.scope.list_ids:
            raise ProviderError("Trello destination list is not configured")
        response = self.transport.request(
            "PUT",
            f"{_BASE}/cards/{fixed_id(card_id, 'card id')}",
            query=self._query({"idList": list_id}),
        )
        return self._mutation_record(require_success(response))

    def set_custom_field(
        self, card_id: str, field_id: str, value: Mapping[str, object]
    ) -> ProviderRecord:
        if field_id not in self.scope.custom_field_ids:
            raise ProviderError("Trello custom field is not configured")
        if set(value) - {"text", "number", "checked", "date"} or len(value) != 1:
            raise ProviderError("Trello custom-field value is not permitted")
        response = self.transport.request(
            "PUT",
            f"{_BASE}/cards/{fixed_id(card_id, 'card id')}/customField/{fixed_id(field_id, 'field id')}/item",
            query=self._auth,
            json_body={"value": dict(value)},
        )
        require_success(response)
        return self.get_card(card_id)

    def archive_card(self, card_id: str) -> ProviderRecord:
        response = self.transport.request(
            "PUT",
            f"{_BASE}/cards/{fixed_id(card_id, 'card id')}",
            query=self._query({"closed": True}),
        )
        record = self._mutation_record(require_success(response))
        if record.fields.get("closed") is not True:
            raise AmbiguousEffectError(
                "Trello archive acknowledgement is malformed; reconcile before retry"
            )
        return record

    def _validate_fields(
        self, fields: Mapping[str, object], allowed: set[str] | frozenset[str]
    ) -> None:
        if not isinstance(fields, Mapping) or not fields:
            raise ProviderError("Trello fields must be a non-empty object")
        unknown = set(fields) - set(allowed)
        if unknown:
            raise ProviderError("Trello update contains forbidden fields")
        labels = fields.get("idLabels")
        if labels is not None and (
            not isinstance(labels, list)
            or any(label not in self.scope.label_ids for label in labels)
        ):
            raise ProviderError("Trello labels are outside configured scope")
