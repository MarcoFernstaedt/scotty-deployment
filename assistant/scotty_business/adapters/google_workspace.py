from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable, Mapping, Sequence

from ..config import GoogleWorkspaceScope
from ..google_oauth import GoogleOAuthError
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


#: Exactly the person fields Scotty may name in a contacts update.
_PERSON_FIELDS = frozenset(
    {
        "addresses",
        "biographies",
        "birthdays",
        "emailAddresses",
        "events",
        "memberships",
        "names",
        "nicknames",
        "occupations",
        "organizations",
        "phoneNumbers",
        "relations",
        "urls",
        "userDefined",
    }
)


def _label_ids(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 50:
        raise ProviderError(f"{field} must be a bounded list of label ids")
    return [_id(item, f"{field} entry") for item in value]


def _text(value: object, field: str, *, limit: int = 1_000) -> str:
    """Bounded free text for a provider search parameter, spaces included."""

    if type(value) is not str or len(value) > limit:
        raise ProviderError(f"{field} is malformed")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ProviderError(f"{field} contains forbidden characters")
    return value


#: Google-native documents have no bytes to download; they are exported to a
#: bounded text form instead. Anything not named here is refused rather than
#: guessed at.
EXPORTABLE_GOOGLE_TYPES: Mapping[str, str] = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

#: Stored file types Scotty may read directly as text.
READABLE_TEXT_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/tab-separated-values",
        "text/html",
        "text/xml",
        "application/json",
        "application/xml",
    }
)

#: Hard ceilings for one bounded read.
MAX_DRIVE_TEXT_BYTES = 1_000_000
MAX_SHEETS_READ_RANGES = 10
MAX_SHEETS_READ_CELLS = 20_000

#: A1 notation: an optional sheet name, then a cell or a cell range.
_A1_RANGE = re.compile(
    r"(?:(?:'[^'\r\n]{1,100}'|[A-Za-z0-9_ .\-]{1,100})!)?"
    r"[A-Za-z]{1,3}[0-9]{0,7}(?::[A-Za-z]{1,3}[0-9]{0,7})?"
)


def _a1_range(value: object, field: str) -> str:
    if type(value) is not str or not _A1_RANGE.fullmatch(value):
        raise ProviderError(f"{field} must be an A1 range such as Sheet1!A1:C20")
    return value


def _count_cells(values: object) -> int:
    if not isinstance(values, list):
        return 0
    return sum(len(row) if isinstance(row, list) else 1 for row in values)


def _bounded_count(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ProviderError("max_results must be an integer from 1 to 100")
    return value


class GoogleWorkspaceAdapter:
    """Broad account-owned Workspace REST surface with code-enforced consequences."""

    def __init__(
        self,
        transport: Transport,
        access_token: str | Callable[[], str],
        scope: GoogleWorkspaceScope,
    ):
        if not callable(access_token) and not access_token:
            raise ProviderError("Google OAuth is not configured")
        self.transport = transport
        self.scope = scope
        self._access_token = access_token

    @property
    def headers(self) -> RedactedMapping:
        """Build the bearer header per request so a refreshed token is used."""

        provider = self._access_token
        try:
            token = provider() if callable(provider) else provider
        except GoogleOAuthError as exc:
            raise ProviderError("Google OAuth is not available") from exc
        if type(token) is not str or not token:
            raise ProviderError("Google OAuth is not available")
        return RedactedMapping(Authorization=f"Bearer {token}")

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
        if not isinstance(body, dict):
            raise ProviderError("Google list response is malformed")
        if collection not in body:
            # Gmail and People omit the collection entirely on zero results.
            return ()
        if not isinstance(body[collection], list):
            raise ProviderError("Google list response is malformed")
        result: list[ProviderRecord] = []
        for item in body[collection]:
            if not isinstance(item, dict):
                raise ProviderError("Google list item is malformed")
            source = _id(item.get(id_field), f"Google {id_field}")
            result.append(self._record(provider, source, item))
        return tuple(result)

    def search_gmail(self, query: str, *, max_results: int = 50) -> tuple[ProviderRecord, ...]:
        _text(query, "Gmail query")
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
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        if query:
            params["q"] = _text(query, "calendar query")
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
        _text(query, "Drive query")
        q = f"({query}) and trashed = false" if query else "trashed = false"
        body = require_success(
            self.transport.request(
                "GET",
                f"{_DRIVE}/files",
                headers=self.headers,
                query={
                    "q": q,
                    "pageSize": _bounded_count(max_results),
                    "fields": "files(id,name,mimeType,size,parents,trashed,modifiedTime,version,etag)",
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
                query={"fields": "id,name,mimeType,size,parents,trashed,modifiedTime,version,etag"},
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
                query={"includeGridData": "false"},
            )
        )
        return self._record("google_sheets", source, body)

    def read_drive_file(self, file_id: str) -> ProviderRecord:
        """Read one Drive file as bounded text, exporting Google-native types.

        The file's own metadata decides the path: a Google-native document is
        exported to a fixed text form, a stored text file is downloaded, and
        anything else is refused rather than returned as guessed-at bytes.
        """

        source = _id(file_id, "Drive file id")
        metadata = self.get_drive_file(source)
        mime = metadata.fields.get("mimeType")
        if type(mime) is not str or not mime:
            raise ProviderError("Drive file metadata is malformed")

        declared = metadata.fields.get("size")
        if type(declared) is str and declared.isdigit() and int(declared) > MAX_DRIVE_TEXT_BYTES:
            raise ProviderError("Drive file is larger than Scotty reads in one call")

        export_as = EXPORTABLE_GOOGLE_TYPES.get(mime)
        if export_as is not None:
            url = f"{_DRIVE}/files/{_path(source, 'Drive file id')}/export"
            query: Mapping[str, object] = {"mimeType": export_as}
        elif mime in READABLE_TEXT_TYPES:
            url = f"{_DRIVE}/files/{_path(source, 'Drive file id')}"
            query = {"alt": "media"}
            export_as = mime
        else:
            raise ProviderError("that Drive file type cannot be read as text")

        body = require_success(
            self.transport.request("GET", url, headers=self.headers, query=query, text=True)
        )
        if type(body) is not str:
            raise ProviderError("Drive content response is malformed")
        if len(body.encode("utf-8")) > MAX_DRIVE_TEXT_BYTES:
            raise ProviderError("Drive file is larger than Scotty reads in one call")
        return self._record(
            "google_drive",
            source,
            {
                "id": source,
                "name": metadata.fields.get("name"),
                "mimeType": mime,
                "readAs": export_as,
                "text": body,
                "etag": metadata.fields.get("etag", "unversioned"),
            },
        )

    def get_sheet_values(self, spreadsheet_id: str, range_: str) -> ProviderRecord:
        """Read one validated A1 range of values."""

        source = _id(spreadsheet_id, "spreadsheet id")
        target = _a1_range(range_, "spreadsheet range")
        body = require_success(
            self.transport.request(
                "GET",
                f"{_SHEETS}/spreadsheets/{_path(source, 'spreadsheet id')}/values/"
                f"{urllib.parse.quote(target, safe='')}",
                headers=self.headers,
                query={"majorDimension": "ROWS"},
            )
        )
        return self._record("google_sheets", source, self._bounded_values(body))

    def batch_get_sheet_values(self, spreadsheet_id: str, ranges: Sequence[str]) -> ProviderRecord:
        """Read a bounded set of validated A1 ranges in one call."""

        source = _id(spreadsheet_id, "spreadsheet id")
        if not isinstance(ranges, Sequence) or isinstance(ranges, str | bytes):
            raise ProviderError("spreadsheet ranges must be a list")
        if not 1 <= len(ranges) <= MAX_SHEETS_READ_RANGES:
            raise ProviderError(
                f"spreadsheet ranges must number from 1 to {MAX_SHEETS_READ_RANGES}"
            )
        targets = [_a1_range(item, "spreadsheet range") for item in ranges]
        body = require_success(
            self.transport.request(
                "GET",
                f"{_SHEETS}/spreadsheets/{_path(source, 'spreadsheet id')}/values:batchGet",
                headers=self.headers,
                query={"ranges": targets, "majorDimension": "ROWS"},
            )
        )
        if not isinstance(body, dict):
            raise ProviderError("Sheets values response is malformed")
        value_ranges = body.get("valueRanges")
        if not isinstance(value_ranges, list):
            raise ProviderError("Sheets values response is malformed")
        total = 0
        bounded: list[Mapping[str, object]] = []
        for entry in value_ranges:
            checked = self._bounded_values(entry)
            total += _count_cells(checked.get("values"))
            if total > MAX_SHEETS_READ_CELLS:
                raise ProviderError("Sheets response is larger than Scotty reads in one call")
            bounded.append(checked)
        return self._record(
            "google_sheets", source, {"spreadsheetId": source, "valueRanges": bounded}
        )

    def _bounded_values(self, body: object) -> dict[str, object]:
        """Validate one values payload and refuse an oversize response."""

        if not isinstance(body, Mapping):
            raise ProviderError("Sheets values response is malformed")
        values = body.get("values", [])
        if not isinstance(values, list):
            raise ProviderError("Sheets values response is malformed")
        if _count_cells(values) > MAX_SHEETS_READ_CELLS:
            raise ProviderError("Sheets response is larger than Scotty reads in one call")
        return {
            "range": body.get("range", ""),
            "majorDimension": body.get("majorDimension", "ROWS"),
            "values": values,
        }

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
            # Only the two label fields Gmail's modify endpoint applies, so no
            # unrelated key is ever posted against the caller's message.
            if not payload or set(payload) - {"addLabelIds", "removeLabelIds"}:
                raise ProviderError("Gmail label modification is malformed")
            body = {field: _label_ids(value, field) for field, value in payload.items()}
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
            url, method, body = (
                f"{_DRIVE}/files/{_path(resource_id, 'Drive file id')}",
                "DELETE",
                {},
            )
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
            url = (
                f"{_SHEETS}/spreadsheets/{_path(resource_id, 'spreadsheet id')}/values:batchUpdate"
            )
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
            if not fields or any(field not in _PERSON_FIELDS for field in fields):
                raise ProviderError("contact update names no known person field")
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
