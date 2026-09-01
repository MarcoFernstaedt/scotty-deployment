"""A stateful synthetic Google Workspace for readback tests.

The real defect this exists to catch is a mutation that reports success while
the provider state says otherwise. A stub that echoes the request back can
never catch that, so this double keeps actual state: a mutation changes it and
a later read answers from it, exactly as the adapter's readback expects.

Every identifier is invented. Nothing here opens a socket or reaches Google.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping

from assistant.scotty_business.adapters.http import HttpResponse

_GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
_CALENDAR = "https://www.googleapis.com/calendar/v3"
_DRIVE = "https://www.googleapis.com/drive/v3"
_DOCS = "https://docs.googleapis.com/v1"
_SHEETS = "https://sheets.googleapis.com/v4"
_PEOPLE = "https://people.googleapis.com/v1"


class SyntheticGoogle:
    """In-memory Workspace state behind the transport interface."""

    def __init__(self) -> None:
        self.messages: dict[str, dict[str, object]] = {
            "message-1": {"id": "message-1", "labelIds": ["INBOX", "UNREAD"]}
        }
        self.drafts: dict[str, dict[str, object]] = {}
        self.events: dict[tuple[str, str], dict[str, object]] = {}
        self.files: dict[str, dict[str, object]] = {
            "file-1": {"id": "file-1", "name": "Notes", "mimeType": "text/plain", "parents": []}
        }
        self.permissions: dict[str, list[dict[str, object]]] = {}
        self.documents: dict[str, dict[str, object]] = {}
        self.spreadsheets: dict[str, dict[str, object]] = {}
        self.values: dict[tuple[str, str], list[list[object]]] = {}
        self.people: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, str]] = []
        self._next = 1

        # A spreadsheet and document that already exist for update paths.
        self.documents["document-1"] = {"documentId": "document-1", "title": "Notes"}
        self.spreadsheets["spreadsheet-1"] = {"spreadsheetId": "spreadsheet-1"}
        self.people["people/contact-1"] = {
            "resourceName": "people/contact-1",
            "etag": "etag-1",
            "names": [],
        }

        #: Set to break one readback and prove the caller fails closed.
        self.readback_status: int | None = None
        self.readback_body: object | None = None
        self.drop_batch_replies = 0

    # -- helpers --------------------------------------------------------

    def _id(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}-{self._next}"

    def _ok(self, body: object, status: int = 200) -> HttpResponse:
        return HttpResponse(status, {}, body)

    def _missing(self) -> HttpResponse:
        return HttpResponse(404, {}, {"error": {"code": 404}})

    def _override(self) -> HttpResponse | None:
        if self.readback_status is None:
            return None
        status, body = self.readback_status, self.readback_body
        self.readback_status, self.readback_body = None, None
        return HttpResponse(status, {}, body)

    # -- transport interface -------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        attachment: object | None = None,
        text: bool = False,
    ) -> HttpResponse:
        self.calls.append((method, url))
        query = query or {}
        body = json_body or {}
        if method == "GET":
            override = self._override()
            if override is not None:
                return override
        for handler in (
            self._gmail,
            self._calendar,
            self._drive,
            self._docs,
            self._sheets,
            self._people,
        ):
            response = handler(method, url, query, body)
            if response is not None:
                return response
        return self._ok({})

    # -- Gmail ----------------------------------------------------------

    def _gmail(self, method, url, query, body):
        if not url.startswith(_GMAIL):
            return None
        path = url[len(_GMAIL) :]
        if method == "POST" and path.endswith("/modify"):
            message_id = path.split("/")[2]
            record = self.messages.setdefault(message_id, {"id": message_id, "labelIds": []})
            labels = list(record.get("labelIds", []))
            for label in body.get("addLabelIds", []):
                if label not in labels:
                    labels.append(label)
            for label in body.get("removeLabelIds", []):
                if label in labels:
                    labels.remove(label)
            record["labelIds"] = labels
            return self._ok(dict(record))
        if method == "GET" and path.startswith("/messages/"):
            record = self.messages.get(path.split("/")[2])
            return self._ok(dict(record)) if record else self._missing()
        if method == "POST" and path == "/drafts":
            draft_id = self._id("draft")
            self.drafts[draft_id] = {"id": draft_id, "message": dict(body.get("message", {}))}
            return self._ok(dict(self.drafts[draft_id]), status=201)
        if method == "PUT" and path.startswith("/drafts/"):
            draft_id = path.split("/")[2]
            self.drafts[draft_id] = {"id": draft_id, "message": dict(body.get("message", {}))}
            return self._ok(dict(self.drafts[draft_id]))
        if method == "POST" and path == "/drafts/send":
            draft = self.drafts.pop(str(body.get("id")), None)
            if draft is None:
                return self._missing()
            message_id = self._id("message")
            self.messages[message_id] = {"id": message_id, "labelIds": ["SENT"]}
            return self._ok({"id": message_id})
        if method == "GET" and path.startswith("/drafts/"):
            record = self.drafts.get(path.split("/")[2])
            return self._ok(dict(record)) if record else self._missing()
        if method == "GET" and path.startswith("/messages"):
            return self._ok({"messages": [{"id": key} for key in self.messages]})
        return None

    # -- Calendar --------------------------------------------------------

    def _calendar(self, method, url, query, body):
        if not url.startswith(_CALENDAR):
            return None
        parts = url[len(_CALENDAR) :].strip("/").split("/")
        if len(parts) < 3 or parts[0] != "calendars":
            return None
        calendar_id = urllib.parse.unquote(parts[1])
        if method == "POST":
            event_id = self._id("event")
            self.events[(calendar_id, event_id)] = {"id": event_id, "status": "confirmed", **body}
            return self._ok(dict(self.events[(calendar_id, event_id)]), status=201)
        if len(parts) < 4:
            return self._ok({"items": []})
        event_id = urllib.parse.unquote(parts[3])
        key = (calendar_id, event_id)
        if method == "PATCH":
            record = self.events.setdefault(key, {"id": event_id, "status": "confirmed"})
            record.update(body)
            return self._ok(dict(record))
        if method == "DELETE":
            record = self.events.setdefault(key, {"id": event_id})
            record["status"] = "cancelled"
            return self._ok({}, status=204)
        if method == "GET":
            record = self.events.get(key)
            return self._ok(dict(record)) if record else self._missing()
        return None

    # -- Drive -----------------------------------------------------------

    def _drive(self, method, url, query, body):
        if not url.startswith(_DRIVE):
            return None
        path = url[len(_DRIVE) :]
        if path == "/files" and method == "POST":
            file_id = self._id("file")
            self.files[file_id] = {"id": file_id, "parents": [], **body}
            return self._ok(dict(self.files[file_id]), status=201)
        if path == "/files" and method == "GET":
            return self._ok({"files": [{"id": key} for key in self.files]})
        if path.endswith("/permissions"):
            file_id = path.split("/")[2]
            if method == "POST":
                entry = {"id": self._id("permission"), **body}
                self.permissions.setdefault(file_id, []).append(entry)
                return self._ok(dict(entry), status=201)
            return self._ok({"permissions": list(self.permissions.get(file_id, []))})
        if path.startswith("/files/"):
            file_id = path.split("/")[2]
            if method == "PATCH":
                record = self.files.setdefault(file_id, {"id": file_id, "parents": []})
                parents = list(record.get("parents", []))
                for parent in str(query.get("addParents", "")).split(","):
                    if parent and parent not in parents:
                        parents.append(parent)
                for parent in str(query.get("removeParents", "")).split(","):
                    if parent in parents:
                        parents.remove(parent)
                record.update(body)
                record["parents"] = parents
                return self._ok(dict(record))
            if method == "DELETE":
                self.files.pop(file_id, None)
                return self._ok({}, status=204)
            if method == "GET":
                record = self.files.get(file_id)
                return self._ok(dict(record)) if record else self._missing()
        return None

    # -- Docs ------------------------------------------------------------

    def _docs(self, method, url, query, body):
        if not url.startswith(_DOCS):
            return None
        path = url[len(_DOCS) :]
        if path == "/documents" and method == "POST":
            document_id = self._id("document")
            self.documents[document_id] = {"documentId": document_id, **body}
            return self._ok(dict(self.documents[document_id]), status=201)
        if path.endswith(":batchUpdate") and method == "POST":
            document_id = path.split("/")[2].removesuffix(":batchUpdate")
            requests = body.get("requests", [])
            applied = max(len(requests) - self.drop_batch_replies, 0)
            self.documents.setdefault(document_id, {"documentId": document_id})
            return self._ok({"documentId": document_id, "replies": [{} for _ in range(applied)]})
        if path.startswith("/documents/") and method == "GET":
            record = self.documents.get(path.split("/")[2])
            return self._ok(dict(record)) if record else self._missing()
        return None

    # -- Sheets ----------------------------------------------------------

    def _sheets(self, method, url, query, body):
        if not url.startswith(_SHEETS):
            return None
        path = url[len(_SHEETS) :]
        if path == "/spreadsheets" and method == "POST":
            spreadsheet_id = self._id("spreadsheet")
            self.spreadsheets[spreadsheet_id] = {"spreadsheetId": spreadsheet_id, **body}
            return self._ok(dict(self.spreadsheets[spreadsheet_id]), status=201)
        if path.endswith("/values:batchUpdate") and method == "POST":
            spreadsheet_id = path.split("/")[2]
            for entry in body.get("data", []):
                self.values[(spreadsheet_id, entry.get("range", ""))] = list(
                    entry.get("values", [])
                )
            return self._ok({"spreadsheetId": spreadsheet_id})
        if path.endswith("/values:batchGet") and method == "GET":
            spreadsheet_id = path.split("/")[2]
            ranges = query.get("ranges", [])
            return self._ok(
                {
                    "spreadsheetId": spreadsheet_id,
                    "valueRanges": [
                        {
                            "range": item,
                            "majorDimension": "ROWS",
                            "values": self.values.get((spreadsheet_id, item), []),
                        }
                        for item in ranges
                    ],
                }
            )
        if "/values/" in path and method == "GET":
            spreadsheet_id = path.split("/")[2]
            target = urllib.parse.unquote(path.split("/values/")[1])
            return self._ok(
                {
                    "range": target,
                    "majorDimension": "ROWS",
                    "values": self.values.get((spreadsheet_id, target), []),
                }
            )
        if path.endswith(":batchUpdate") and method == "POST":
            spreadsheet_id = path.split("/")[2].removesuffix(":batchUpdate")
            requests = body.get("requests", [])
            applied = max(len(requests) - self.drop_batch_replies, 0)
            self.spreadsheets.setdefault(spreadsheet_id, {"spreadsheetId": spreadsheet_id})
            return self._ok(
                {"spreadsheetId": spreadsheet_id, "replies": [{} for _ in range(applied)]}
            )
        if path.startswith("/spreadsheets/") and method == "GET":
            record = self.spreadsheets.get(path.split("/")[2])
            return self._ok(dict(record)) if record else self._missing()
        return None

    # -- People ----------------------------------------------------------

    def _people(self, method, url, query, body):
        if not url.startswith(_PEOPLE):
            return None
        path = url[len(_PEOPLE) :].lstrip("/")
        if path == "people:createContact" and method == "POST":
            resource = f"people/{self._id('contact')}"
            self.people[resource] = {"resourceName": resource, "etag": "etag-new", **body}
            return self._ok(dict(self.people[resource]), status=201)
        if path.endswith(":updateContact") and method == "PATCH":
            resource = urllib.parse.unquote(path.removesuffix(":updateContact"))
            record = self.people.setdefault(resource, {"resourceName": resource})
            record.update({key: value for key, value in body.items() if key != "etag"})
            return self._ok(dict(record))
        if path.endswith(":deleteContact") and method == "DELETE":
            self.people.pop(urllib.parse.unquote(path.removesuffix(":deleteContact")), None)
            return self._ok({}, status=204)
        if method == "GET" and path.startswith("people/"):
            record = self.people.get(urllib.parse.unquote(path))
            return self._ok(dict(record)) if record else self._missing()
        if method == "GET" and path.startswith("people/me/connections"):
            return self._ok({"connections": []})
        return None
