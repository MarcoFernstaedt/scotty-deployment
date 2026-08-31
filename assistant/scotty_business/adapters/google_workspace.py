from __future__ import annotations

import urllib.parse
from collections.abc import Mapping, Sequence

from ..config import GoogleWorkspaceScope
from ..google_policy import GoogleActionClass, classify_google_action
from .http import ProviderError, RedactedMapping, Transport, require_success
from .records import ProviderRecord, utc_now

_GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
_CALENDAR = "https://www.googleapis.com/calendar/v3"
_DRIVE = "https://www.googleapis.com/drive/v3"
_DOCS = "https://docs.googleapis.com/v1"
_SHEETS = "https://sheets.googleapis.com/v4"
_PEOPLE = "https://people.googleapis.com/v1"


def _id(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > 256:
        raise ProviderError(f"{field} must be a bounded non-empty string")
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ProviderError(f"{field} contains forbidden characters")
    return value


def _path(value: object, field: str) -> str:
    return urllib.parse.quote(_id(value, field), safe="")


def _bounded_count(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ProviderError("max_results must be an integer from 1 to 100")
    return value


class GoogleWorkspaceAdapter:
    """Broad account-owned Workspace REST surface with code-enforced consequences."""

    def __init__(self, transport: Transport, access_token: str, scope: GoogleWorkspaceScope):
        if not access_token:
            raise ProviderError("Google OAuth is not configured")
        self.transport = transport
        self.scope = scope
        self.headers = RedactedMapping(Authorization=f"Bearer {access_token}")

    def _record(self, provider: str, source_id: str, body: object) -> ProviderRecord:
        if not isinstance(body, dict):
            raise ProviderError("Google response must be an object")
        return ProviderRecord(
            provider,
            source_id,
            utc_now(),
            str(body.get("etag", body.get("version", "unversioned"))),
            body,
            (),
        )

    def _records(
        self, provider: str, body: object, collection: str, id_field: str
    ) -> tuple[ProviderRecord, ...]:
        if not isinstance(body, dict) or not isinstance(body.get(collection), list):
            raise ProviderError("Google list response is malformed")
        result: list[ProviderRecord] = []
        for item in body[collection]:
            if not isinstance(item, dict):
                raise ProviderError("Google list item is malformed")
            source = _id(item.get(id_field), f"Google {id_field}")
            result.append(self._record(provider, source, item))
        return tuple(result)

    def search_gmail(self, query: str, *, max_results: int = 50) -> tuple[ProviderRecord, ...]:
        if type(query) is not str or len(query) > 1_000:
            raise ProviderError("Gmail query is malformed")
        body = require_success(
            self.transport.request(
                "GET",
                f"{_GMAIL}/messages",
                headers=self.headers,
                query={"q": query, "maxResults": _bounded_count(max_results)},
            )
        )
        return self._records("google_gmail", body, "messages", "id")

    def get_gmail_message(self, message_id: str) -> ProviderRecord:
        message = _id(message_id, "Gmail message id")
        body = require_success(
            self.transport.request(
                "GET",
                f"{_GMAIL}/messages/{_path(message, 'Gmail message id')}",
                headers=self.headers,
                query={"format": "full"},
            )
        )
        return self._record("google_gmail", message, body)

    def create_gmail_draft(self, raw_base64url: str) -> ProviderRecord:
        return self.execute_routine("gmail_create_draft", "new", {"raw": raw_base64url})

    def list_calendar_events(
        self,
        calendar_id: str,
        *,
        query: str = "",
        time_min: str | None = None,
        time_max: str | None = None,
        max_results: int = 50,
    ) -> tuple[ProviderRecord, ...]:
        params: dict[str, object] = {
            "maxResults": _bounded_count(max_results),
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if query:
            params["q"] = _id(query, "calendar query")
        if time_min:
            params["timeMin"] = _id(time_min, "calendar time_min")
        if time_max:
            params["timeMax"] = _id(time_max, "calendar time_max")
        body = require_success(
            self.transport.request(
                "GET",
                f"{_CALENDAR}/calendars/{_path(calendar_id, 'calendar id')}/events",
                headers=self.headers,
                query=params,
            )
        )
        return self._records("google_calendar", body, "items", "id")

    def get_calendar_event(self, calendar_id: str, event_id: str) -> ProviderRecord:
        event = _id(event_id, "calendar event id")
        body = require_success(
            self.transport.request(
                "GET",
                f"{_CALENDAR}/calendars/{_path(calendar_id, 'calendar id')}/events/"
                f"{_path(event, 'calendar event id')}",
                headers=self.headers,
            )
        )
        return self._record("google_calendar", event, body)

    def search_drive(self, query: str, *, max_results: int = 50) -> tuple[ProviderRecord, ...]:
        if type(query) is not str or len(query) > 1_000:
            raise ProviderError("Drive query is malformed")
        q = f"({query}) and trashed = false" if query else "trashed = false"
        body = require_success(
            self.transport.request(
                "GET",
                f"{_DRIVE}/files",
                headers=self.headers,
                query={
                    "q": q,
                    "pageSize": _bounded_count(max_results),
                    "fields": "files(id,name,mimeType,parents,trashed,modifiedTime,version,etag)",
                },
            )
        )
        return self._records("google_drive", body, "files", "id")

    def get_drive_file(self, file_id: str) -> ProviderRecord:
        source = _id(file_id, "Drive file id")
        body = require_success(
            self.transport.request(
                "GET",
                f"{_DRIVE}/files/{_path(source, 'Drive file id')}",
                headers=self.headers,
                query={"fields": "id,name,mimeType,parents,trashed,modifiedTime,version,etag"},
            )
        )
        return self._record("google_drive", source, body)

    def get_document(self, document_id: str) -> ProviderRecord:
        source = _id(document_id, "document id")
        body = require_success(
            self.transport.request(
                "GET",
                f"{_DOCS}/documents/{_path(source, 'document id')}",
                headers=self.headers,
            )
        )
        return self._record("google_docs", source, body)

    def get_spreadsheet(self, spreadsheet_id: str) -> ProviderRecord:
        source = _id(spreadsheet_id, "spreadsheet id")
        body = require_success(
            self.transport.request(
                "GET",
                f"{_SHEETS}/spreadsheets/{_path(source, 'spreadsheet id')}",
                headers=self.headers,
                query={"includeGridData": False},
            )
        )
        return self._record("google_sheets", source, body)

    def list_contacts(self, *, page_size: int = 100) -> tuple[ProviderRecord, ...]:
        body = require_success(
            self.transport.request(
                "GET",
                f"{_PEOPLE}/people/me/connections",
                headers=self.headers,
                query={
                    "pageSize": _bounded_count(page_size),
                    "personFields": "names,emailAddresses,phoneNumbers,organizations,metadata",
                },
            )
        )
        return self._records("google_contacts", body, "connections", "resourceName")

    def get_contact(self, resource_name: str) -> ProviderRecord:
        source = _id(resource_name, "contact resource name")
        if not source.startswith("people/"):
            raise ProviderError("contact resource name is malformed")
        body = require_success(
            self.transport.request(
                "GET",
                f"{_PEOPLE}/{urllib.parse.quote(source, safe='/')}",
                headers=self.headers,
                query={"personFields": "names,emailAddresses,phoneNumbers,organizations,metadata"},
            )
        )
        return self._record("google_contacts", source, body)

    def execute_routine(
        self, operation: str, resource_id: str, payload: Mapping[str, object]
    ) -> ProviderRecord:
        if classify_google_action(operation, payload) is not GoogleActionClass.ROUTINE:
            raise ProviderError("Google Workspace action requires approval or is forbidden")
        return self._execute(operation, resource_id, payload)

    def mutate(
        self, operation: str, resource_id: str, payload: Mapping[str, object]
    ) -> ProviderRecord:
        """Approval executor for exact consequence-classified actions only."""
        if classify_google_action(operation, payload) is not GoogleActionClass.CONSEQUENCE:
            raise ProviderError("Google Workspace consequence is not permitted")
        return self._execute(operation, resource_id, payload)

    def _execute(
        self, operation: str, resource_id: str, payload: Mapping[str, object]
    ) -> ProviderRecord:
        body: Mapping[str, object] = dict(payload)
        method = "POST"
        query: Mapping[str, object] | None = None
        source = resource_id

        if operation == "gmail_modify_labels":
            url = f"{_GMAIL}/messages/{_path(resource_id, 'Gmail message id')}/modify"
        elif operation == "gmail_create_draft":
            raw = payload.get("raw")
            if type(raw) is not str or not raw or len(raw) > 65_000:
                raise ProviderError("Gmail draft is malformed")
            url, source, body = f"{_GMAIL}/drafts", "new", {"message": {"raw": raw}}
        elif operation == "gmail_update_draft":
            url, method = f"{_GMAIL}/drafts/{_path(resource_id, 'Gmail draft id')}", "PUT"
            raw = payload.get("raw")
            if type(raw) is not str or not raw or len(raw) > 65_000:
                raise ProviderError("Gmail draft is malformed")
            body = {"message": {"raw": raw}}
        elif operation == "gmail_send_draft":
            url, body = f"{_GMAIL}/drafts/send", {"id": _id(resource_id, "Gmail draft id")}
        elif operation.startswith("calendar_"):
            if "/" in resource_id:
                calendar_id, event_id = resource_id.split("/", 1)
            else:
                calendar_id, event_id = resource_id, ""
            base = f"{_CALENDAR}/calendars/{_path(calendar_id, 'calendar id')}/events"
            if operation == "calendar_create_event":
                url = base
            elif operation == "calendar_update_event":
                url, method = f"{base}/{_path(event_id, 'calendar event id')}", "PATCH"
            elif operation == "calendar_cancel_event":
                url, method, body = f"{base}/{_path(event_id, 'calendar event id')}", "DELETE", {}
            else:
                raise ProviderError("Google Calendar action is not permitted")
        elif operation == "drive_create_file":
            url = f"{_DRIVE}/files"
        elif operation in {"drive_update_file", "drive_move_file", "drive_trash_file"}:
            url, method = f"{_DRIVE}/files/{_path(resource_id, 'Drive file id')}", "PATCH"
            if operation == "drive_move_file":
                allowed = {"addParents", "removeParents"}
                if not payload or set(payload) - allowed:
                    raise ProviderError("Drive move is malformed")
                query, body = dict(payload), {}
            elif operation == "drive_trash_file":
                body = {"trashed": True}
        elif operation == "drive_delete_permanently":
            url, method, body = f"{_DRIVE}/files/{_path(resource_id, 'Drive file id')}", "DELETE", {}
        elif operation == "drive_change_permissions":
            url = f"{_DRIVE}/files/{_path(resource_id, 'Drive file id')}/permissions"
        elif operation == "docs_create":
            url, source = f"{_DOCS}/documents", "new"
        elif operation == "docs_batch_update":
            url = f"{_DOCS}/documents/{_path(resource_id, 'document id')}:batchUpdate"
        elif operation == "sheets_create":
            url, source = f"{_SHEETS}/spreadsheets", "new"
        elif operation == "sheets_batch_update":
            url = f"{_SHEETS}/spreadsheets/{_path(resource_id, 'spreadsheet id')}:batchUpdate"
        elif operation == "sheets_update_values":
            url = f"{_SHEETS}/spreadsheets/{_path(resource_id, 'spreadsheet id')}/values:batchUpdate"
        elif operation == "contacts_create":
            url, source = f"{_PEOPLE}/people:createContact", "new"
        elif operation == "contacts_update":
            if not resource_id.startswith("people/"):
                raise ProviderError("contact resource name is malformed")
            url, method = (
                f"{_PEOPLE}/{urllib.parse.quote(resource_id, safe='/')}:updateContact",
                "PATCH",
            )
            fields = tuple(key for key in payload if key != "etag")
            if not fields:
                raise ProviderError("contact update has no fields")
            query = {"updatePersonFields": ",".join(fields)}
        elif operation == "contacts_delete":
            if not resource_id.startswith("people/"):
                raise ProviderError("contact resource name is malformed")
            url, method, body = (
                f"{_PEOPLE}/{urllib.parse.quote(resource_id, safe='/')}:deleteContact",
                "DELETE",
                {},
            )
        else:
            raise ProviderError("Google Workspace action is not permitted")

        response = require_success(
            self.transport.request(method, url, headers=self.headers, query=query, json_body=body),
            expected=(200, 201, 204),
        )
        if isinstance(response, dict):
            source = str(
                response.get("id")
                or response.get("resourceName")
                or response.get("documentId")
                or response.get("spreadsheetId")
                or source
            )
        return self._record("google_workspace", source, response)
