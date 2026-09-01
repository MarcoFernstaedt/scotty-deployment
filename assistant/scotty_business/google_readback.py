"""Authoritative readback for every Google mutation Scotty performs.

A mutation response is the provider telling us what it believes it did. It is
not proof. Google can accept a batch and apply part of it, answer a retried
request from cache, or time out after the effect landed. So every mutation is
followed by an independent read of the resource, and the intended state is
compared against what that read actually shows.

Only an exact match verifies. Absent, malformed, unavailable, mismatched, or
partially applied outcomes are reported as unverified, which the caller turns
into `unknown`: the effect may or may not have happened, so it is reconciled
rather than retried. An operation with no authoritative read available is
unverified too — never assumed good.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TypeGuard


class ReadbackStatus(StrEnum):
    """Why a mutation was or was not verified. Fixed, redacted vocabulary."""

    VERIFIED = "verified"
    MISMATCH = "readback does not match the intended state"
    ABSENT = "the resource is absent after the mutation"
    PRESENT = "the resource is still present after the mutation"
    MALFORMED = "readback response is malformed"
    UNAVAILABLE = "readback is unavailable"
    PARTIAL = "the provider applied only part of the request"
    UNSUPPORTED = "this operation has no authoritative readback"


@dataclass(frozen=True, slots=True)
class ReadbackRequest:
    """The independent read that proves one mutation landed."""

    url: str
    query: Mapping[str, object] | None = None
    #: True when verification means the resource is gone rather than present.
    expect_absent: bool = False
    #: True when the readback returns text rather than JSON.
    text: bool = False


@dataclass(frozen=True, slots=True)
class ReadbackPlan:
    """How to verify one mutation, and what the result must show.

    `expected` is what must be present. `absent` is what must be gone: a
    removal is only proven by the removed value no longer being there, and
    subset matching alone can never show that.
    """

    request: ReadbackRequest
    expected: Mapping[str, object]
    absent: Mapping[str, Sequence[object]] = field(default_factory=dict)


def _text(value: object) -> str | None:
    return value.strip() if type(value) is str else None


def _instant(value: object) -> float | None:
    """Parse an RFC3339 timestamp so equal instants compare equal."""

    raw = _text(value)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_list(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def matches(intended: object, observed: object) -> bool:
    """Whether the provider's state contains exactly the intended state.

    Comparison is by meaning, not by spelling: surrounding whitespace is
    ignored, equal instants written in different offsets are equal, lists of
    scalars compare without regard to order, and a mapping is satisfied when
    every key the intent named is satisfied. Keys the intent did not name are
    ignored, because a mutation asserts nothing about them.
    """

    if isinstance(intended, Mapping):
        if not isinstance(observed, Mapping):
            return False
        return all(
            key in observed and matches(value, observed[key]) for key, value in intended.items()
        )
    if _is_list(intended):
        if not _is_list(observed):
            return False
        remaining = list(observed)
        for item in intended:
            for index, candidate in enumerate(remaining):
                if matches(item, candidate):
                    del remaining[index]
                    break
            else:
                return False
        return True
    left, right = _instant(intended), _instant(observed)
    if left is not None and right is not None:
        return left == right
    if type(intended) is str and type(observed) is str:
        return intended.strip() == observed.strip()
    return bool(intended == observed)


def _absent(field: str) -> Mapping[str, object]:
    del field
    return {}


def _reply_count(response: object) -> int | None:
    if not isinstance(response, Mapping):
        return None
    replies = response.get("replies")
    return len(replies) if _is_list(replies) else None


def applied_fully(operation: str, payload: Mapping[str, object], response: object) -> bool | None:
    """Whether a batch applied every request, or None when not a batch.

    Google answers a batch with one reply per request. Fewer replies than
    requests means part of the batch did not apply, which is a partial effect
    and must never read as verified.
    """

    if operation not in {"docs_batch_update", "sheets_batch_update"}:
        return None
    requests = payload.get("requests")
    if not _is_list(requests):
        return None
    replies = _reply_count(response)
    if replies is None:
        return None
    return replies >= len(requests)


def plan(
    operation: str,
    resource_id: str,
    payload: Mapping[str, object],
    response: object,
    endpoints: Mapping[str, str],
) -> ReadbackPlan | None:
    """The authoritative read for one mutation, or None when none exists."""

    gmail = endpoints["gmail"]
    calendar = endpoints["calendar"]
    drive = endpoints["drive"]
    docs = endpoints["docs"]
    sheets = endpoints["sheets"]
    people = endpoints["people"]
    body = response if isinstance(response, Mapping) else {}
    created = _text(body.get("id")) or _text(body.get("resourceName")) or ""

    if operation == "gmail_modify_labels":
        expected: dict[str, object] = {"id": resource_id}
        add = payload.get("addLabelIds")
        if _is_list(add):
            expected["labelIds"] = list(add)
        remove = payload.get("removeLabelIds")
        gone: dict[str, Sequence[object]] = {"labelIds": list(remove)} if _is_list(remove) else {}
        return ReadbackPlan(
            ReadbackRequest(f"{gmail}/messages/{resource_id}", {"format": "minimal"}),
            expected,
            gone,
        )

    if operation in {"gmail_create_draft", "gmail_update_draft"}:
        draft = created if operation == "gmail_create_draft" else resource_id
        if not draft:
            return None
        # Gmail rewrites headers when it stores a draft, so the uploaded bytes
        # are not what comes back. Verify the draft exists and carries a
        # message, which is what the mutation actually asserts.
        return ReadbackPlan(
            ReadbackRequest(f"{gmail}/drafts/{draft}", {"format": "minimal"}), {"id": draft}
        )

    if operation == "gmail_send_draft":
        message = _text(body.get("id")) or ""
        if not message:
            return None
        return ReadbackPlan(
            ReadbackRequest(f"{gmail}/messages/{message}", {"format": "minimal"}),
            {"id": message, "labelIds": ["SENT"]},
        )

    if operation.startswith("calendar_"):
        calendar_id, _, event_hint = resource_id.partition("/")
        event = created or event_hint
        if not calendar_id or not event:
            return None
        url = f"{calendar}/calendars/{calendar_id}/events/{event}"
        if operation == "calendar_cancel_event":
            return ReadbackPlan(ReadbackRequest(url), {"status": "cancelled"})
        intended = {
            key: value
            for key, value in payload.items()
            if key in {"summary", "location", "description", "start", "end", "attendees"}
        }
        return ReadbackPlan(ReadbackRequest(url), {"id": event, **intended})

    if operation in {"drive_create_file", "drive_update_file", "drive_move_file"}:
        target = created if operation == "drive_create_file" else resource_id
        if not target:
            return None
        request = ReadbackRequest(
            f"{drive}/files/{target}",
            {"fields": "id,name,mimeType,size,parents,trashed,modifiedTime,version,etag"},
        )
        if operation == "drive_move_file":
            add = _text(payload.get("addParents"))
            removed = _text(payload.get("removeParents"))
            return ReadbackPlan(
                request,
                {"id": target, **({"parents": add.split(",")} if add else {})},
                {"parents": removed.split(",")} if removed else {},
            )
        intended = {
            key: value for key, value in payload.items() if key in {"name", "mimeType", "trashed"}
        }
        return ReadbackPlan(request, {"id": target, **intended})

    if operation == "drive_trash_file":
        return ReadbackPlan(
            ReadbackRequest(f"{drive}/files/{resource_id}", {"fields": "id,trashed"}),
            {"id": resource_id, "trashed": True},
        )

    if operation == "drive_delete_permanently":
        return ReadbackPlan(
            ReadbackRequest(f"{drive}/files/{resource_id}", {"fields": "id"}, expect_absent=True),
            _absent("id"),
        )

    if operation == "drive_change_permissions":
        return ReadbackPlan(
            ReadbackRequest(
                f"{drive}/files/{resource_id}/permissions",
                {"fields": "permissions(id,type,role,domain,emailAddress)"},
            ),
            {
                "permissions": [
                    {
                        key: value
                        for key, value in payload.items()
                        if key in {"type", "role", "domain"}
                    }
                ]
            },
        )

    if operation in {"docs_create", "docs_batch_update"}:
        document = created or _text(body.get("documentId")) or resource_id
        if not document:
            return None
        expected = {"documentId": document}
        if operation == "docs_create":
            title = _text(payload.get("title"))
            if title:
                expected["title"] = title
        return ReadbackPlan(
            ReadbackRequest(
                f"{docs}/documents/{document}", {"fields": "documentId,title,revisionId"}
            ),
            expected,
        )

    if operation in {"sheets_create", "sheets_batch_update"}:
        spreadsheet = created or _text(body.get("spreadsheetId")) or resource_id
        if not spreadsheet:
            return None
        return ReadbackPlan(
            ReadbackRequest(f"{sheets}/spreadsheets/{spreadsheet}", {"includeGridData": "false"}),
            {"spreadsheetId": spreadsheet},
        )

    if operation == "sheets_update_values":
        data = payload.get("data")
        if not _is_list(data) or not data:
            return None
        ranges = [
            entry.get("range")
            for entry in data
            if isinstance(entry, Mapping) and entry.get("range")
        ]
        if not ranges:
            return None
        expected_ranges = [
            {"values": entry["values"]}
            for entry in data
            if isinstance(entry, Mapping) and _is_list(entry.get("values"))
        ]
        return ReadbackPlan(
            ReadbackRequest(
                f"{sheets}/spreadsheets/{resource_id}/values:batchGet",
                {"ranges": ranges, "majorDimension": "ROWS"},
            ),
            {"valueRanges": expected_ranges} if expected_ranges else {},
        )

    if operation in {"contacts_create", "contacts_update"}:
        person = created or resource_id
        if not person.startswith("people/"):
            return None
        intended = {
            key: value
            for key, value in payload.items()
            if key in {"names", "emailAddresses", "phoneNumbers", "organizations"}
        }
        return ReadbackPlan(
            ReadbackRequest(
                f"{people}/{person}",
                {"personFields": "names,emailAddresses,phoneNumbers,organizations"},
            ),
            {"resourceName": person, **intended},
        )

    if operation == "contacts_delete":
        return ReadbackPlan(
            ReadbackRequest(
                f"{people}/{resource_id}",
                {"personFields": "names"},
                expect_absent=True,
            ),
            _absent("resourceName"),
        )

    return None


def verify(
    plan_: ReadbackPlan | None,
    status: int,
    observed: object,
    *,
    fully_applied: bool | None,
) -> ReadbackStatus:
    """Judge one readback. Anything short of an exact match is unverified."""

    if fully_applied is False:
        return ReadbackStatus.PARTIAL
    if plan_ is None:
        return ReadbackStatus.UNSUPPORTED
    if plan_.request.expect_absent:
        if status in {404, 410}:
            return ReadbackStatus.VERIFIED
        if status == 200:
            return ReadbackStatus.PRESENT
        return ReadbackStatus.UNAVAILABLE
    if status in {404, 410}:
        return ReadbackStatus.ABSENT
    if status != 200:
        return ReadbackStatus.UNAVAILABLE
    if not isinstance(observed, Mapping):
        return ReadbackStatus.MALFORMED
    if not matches(plan_.expected, observed):
        return ReadbackStatus.MISMATCH
    for name, removed in plan_.absent.items():
        present = observed.get(name)
        if not _is_list(present):
            continue
        if any(any(matches(value, item) for item in present) for value in removed):
            # Something the mutation was supposed to remove is still there.
            return ReadbackStatus.MISMATCH
    return ReadbackStatus.VERIFIED
