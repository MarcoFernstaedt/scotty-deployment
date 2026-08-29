from __future__ import annotations

import re
from datetime import datetime

from .http import (
    AmbiguousEffectError,
    ProviderError,
    RedactedMapping,
    Transport,
    fixed_id,
    require_success,
)
from .records import ProviderRecord, utc_now

_BASE = "https://services.leadconnectorhq.com"


class GHLAdapter:
    api_version = "v3"

    def __init__(self, transport: Transport, private_token: str, location_id: str):
        if not private_token:
            raise ProviderError("GoHighLevel credential is not configured")
        self.transport = transport
        self.location_id = fixed_id(location_id, "location id")
        self._headers = RedactedMapping(
            Authorization=f"Bearer {private_token}", Version=self.api_version
        )

    def get_contact(
        self, contact_id: str, *, retrieved_at: datetime | None = None
    ) -> ProviderRecord:
        contact = fixed_id(contact_id, "contact id")
        response = self.transport.request(
            "GET", f"{_BASE}/contacts/{contact}", headers=self._headers
        )
        body = require_success(response)
        if not isinstance(body, dict) or not isinstance(body.get("contact"), dict):
            raise ProviderError("GoHighLevel contact response is malformed")
        fields = dict(body["contact"])
        if fields.get("id") != contact or fields.get("locationId") != self.location_id:
            raise ProviderError("GoHighLevel contact identity or location mismatch")
        revision = fields.get("dateUpdated") or fields.get("updatedAt") or "unversioned"
        if type(revision) is not str:
            raise ProviderError("GoHighLevel contact revision is malformed")
        return ProviderRecord(
            "ghl",
            contact,
            retrieved_at or utc_now(),
            revision,
            fields,
            tuple(field for field in ("phone", "email", "dateUpdated") if field not in fields),
        )

    def search_conversations(self, contact_id: str) -> tuple[ProviderRecord, ...]:
        contact = fixed_id(contact_id, "contact id")
        response = self.transport.request(
            "GET",
            f"{_BASE}/conversations/search",
            headers=self._headers,
            query={"locationId": self.location_id, "contactId": contact},
        )
        body = require_success(response)
        rows = body.get("conversations") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise ProviderError("GoHighLevel conversation response is malformed")
        records = []
        for row in rows:
            if not isinstance(row, dict) or row.get("contactId") != contact:
                raise ProviderError("GoHighLevel conversation contact mismatch")
            conversation_id = fixed_id(row.get("id"), "conversation id")
            records.append(
                ProviderRecord(
                    "ghl",
                    conversation_id,
                    utc_now(),
                    str(row.get("lastMessageDate") or row.get("dateUpdated") or "unversioned"),
                    dict(row),
                    (),
                )
            )
        return tuple(records)

    def send_sms(self, contact_id: str, normalized_destination: str, body: str) -> dict[str, str]:
        contact = fixed_id(contact_id, "contact id")
        if type(normalized_destination) is not str or not re.fullmatch(
            r"\+[1-9][0-9]{7,14}", normalized_destination
        ):
            raise ProviderError("SMS destination must be normalized E.164")
        if type(body) is not str or not body.strip() or len(body) > 1600:
            raise ProviderError("SMS body must contain 1-1600 characters")
        response = self.transport.request(
            "POST",
            f"{_BASE}/conversations/messages",
            headers=self._headers,
            json_body={
                "type": "SMS",
                "contactId": contact,
                "toNumber": normalized_destination,
                "message": body,
                "status": "pending",
            },
        )
        result = require_success(response, expected=(200, 201))
        if not isinstance(result, dict):
            raise ProviderError("GoHighLevel SMS acknowledgement is malformed")
        message_id = result.get("messageId")
        conversation_id = result.get("conversationId")
        returned_contact = result.get("contactId", contact)
        if (
            type(message_id) is not str
            or not message_id
            or type(conversation_id) is not str
            or not conversation_id
            or returned_contact != contact
        ):
            raise AmbiguousEffectError(
                "GoHighLevel SMS acknowledgement is malformed; reconcile before retry"
            )
        return {"message_id": message_id, "conversation_id": conversation_id, "contact_id": contact}

    def get_message(self, conversation_id: str, message_id: str, contact_id: str) -> ProviderRecord:
        conversation = fixed_id(conversation_id, "conversation id")
        message = fixed_id(message_id, "message id")
        contact = fixed_id(contact_id, "contact id")
        response = self.transport.request(
            "GET",
            f"{_BASE}/conversations/{conversation}/messages",
            headers=self._headers,
            query={"limit": 100},
        )
        body = require_success(response)
        rows = None
        if isinstance(body, dict):
            nested = body.get("messages")
            rows = nested.get("messages") if isinstance(nested, dict) else nested
        if not isinstance(rows, list):
            raise ProviderError("GoHighLevel message response is malformed")
        for row in rows:
            if not isinstance(row, dict) or row.get("id") != message:
                continue
            if row.get("contactId") != contact or row.get("conversationId") != conversation:
                raise ProviderError("GoHighLevel message identity mismatch")
            revision = str(row.get("dateAdded") or row.get("createdAt") or "unversioned")
            return ProviderRecord("ghl", message, utc_now(), revision, dict(row), ())
        raise ProviderError("GoHighLevel message was not found in authoritative conversation")
