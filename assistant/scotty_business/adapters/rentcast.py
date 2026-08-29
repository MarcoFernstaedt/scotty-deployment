from __future__ import annotations

from datetime import datetime
from typing import Mapping

from .http import ProviderError, RedactedMapping, Transport, require_success
from .records import ProviderRecord, utc_now

_BASE = "https://api.rentcast.io"


class RentCastAdapter:
    api_version = "v1"

    def __init__(self, transport: Transport, api_key: str, endpoints: tuple[str, ...]):
        if not api_key:
            raise ProviderError("RentCast credential is not configured")
        if not endpoints:
            raise ProviderError("RentCast endpoint allowlist is empty")
        self.transport = transport
        self.endpoints = frozenset(endpoints)
        self._headers = RedactedMapping(**{"X-Api-Key": api_key})

    def fetch(
        self,
        endpoint: str,
        query: Mapping[str, object],
        *,
        retrieved_at: datetime | None = None,
    ) -> ProviderRecord:
        if endpoint not in self.endpoints or not endpoint.startswith("/v1/") or ".." in endpoint:
            raise ProviderError("RentCast endpoint is not configured")
        if not isinstance(query, Mapping):
            raise ProviderError("RentCast query must be an object")
        response = self.transport.request(
            "GET", f"{_BASE}{endpoint}", headers=self._headers, query=dict(query)
        )
        body = require_success(response)
        if isinstance(body, list):
            if len(body) != 1 or not isinstance(body[0], dict):
                raise ProviderError("RentCast query must resolve to exactly one record")
            fields = dict(body[0])
        elif isinstance(body, dict):
            fields = dict(body)
        else:
            raise ProviderError("RentCast response is malformed")
        source_id = fields.get("id") or fields.get("propertyId") or fields.get("formattedAddress")
        if type(source_id) is not str or not source_id:
            raise ProviderError("RentCast response is missing a provider record ID")
        revision = fields.get("lastUpdatedDate") or fields.get("retrievedAt") or "unversioned"
        if type(revision) is not str:
            raise ProviderError("RentCast source revision is malformed")
        expected = ("formattedAddress", "latitude", "longitude")
        return ProviderRecord(
            provider="rentcast",
            source_id=source_id,
            retrieved_at=retrieved_at or utc_now(),
            source_revision=revision,
            fields={"endpoint": endpoint, **fields},
            missing_attributes=tuple(field for field in expected if field not in fields),
        )
