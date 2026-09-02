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

#: Operations for which no authoritative read exists at all, whatever the
#: payload. Empty, and kept as a named set so that adding one is a deliberate
#: statement rather than a plan function quietly returning None forever.
#:
#: Note the difference from a `None` plan: that is payload-specific -- a batch
#: whose requests cannot be observed, a values write with no range -- and is
#: reported as `unknown` for that call rather than as a gap in the product.
UNPROVABLE_OPERATIONS: frozenset[str] = frozenset()


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
    #: Text that must appear somewhere in the resource afterwards, and text
    #: that must not. This is how a document batch is proved: the requests
    #: assert something about the content, and the content is what is read.
    contains_text: tuple[str, ...] = ()
    excludes_text: tuple[str, ...] = ()


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


def flatten_text(value: object, depth: int = 0) -> str:
    """Every string in a response body, run together.

    A Docs document is a tree of structural elements and text runs; asking
    whether an inserted sentence is in it means walking the whole thing rather
    than reading one field. Bounded, because a readback must not become a way
    to spend the process on a hostile response.
    """

    if depth > 24:
        return ""
    if type(value) is str:
        return value
    if isinstance(value, Mapping):
        return " ".join(flatten_text(item, depth + 1) for item in value.values())
    if _is_list(value):
        return " ".join(flatten_text(item, depth + 1) for item in value)
    return ""


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
    requests means part of the batch did not apply. An acknowledgement carrying
    no replies at all proves nothing either, and the resource-level readback for
    a batch only proves the document still exists — so a missing reply list is
    reported as not-fully-applied rather than as "not a batch".
    """

    if operation not in {"docs_batch_update", "sheets_batch_update"}:
        return None
    requests = payload.get("requests")
    if not _is_list(requests):
        return None
    if not requests:
        # A batch that asked for nothing changed nothing, which is consistent.
        return True
    replies = _reply_count(response)
    if replies is None:
        return False
    return replies >= len(requests)


#: Document-batch request kinds whose effect a later read can actually find.
#: Everything else -- deleting a range, restyling text, bulleting a paragraph --
#: leaves nothing to look for, so a batch containing one is not verifiable.
_OBSERVABLE_DOCS_REQUESTS = frozenset({"insertText", "replaceAllText"})

#: The same for spreadsheets. A metadata read can see a sheet appear and a
#: title change; it cannot see a cell format or a repeated range.
_OBSERVABLE_SHEETS_REQUESTS = frozenset({"addSheet", "updateSpreadsheetProperties"})


def _docs_observable(
    payload: Mapping[str, object],
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """What a document must and must not contain afterwards, or None.

    None means at least one request in the batch cannot be observed, which
    makes the whole batch unverifiable: the requests are applied together and
    there is no partial answer that would be honest.
    """

    requests = payload.get("requests")
    if not _is_list(requests) or not requests:
        return None
    wanted: list[str] = []
    gone: list[str] = []
    for request in requests:
        if not isinstance(request, Mapping) or len(request) != 1:
            return None
        ((kind, argument),) = request.items()
        if kind not in _OBSERVABLE_DOCS_REQUESTS or not isinstance(argument, Mapping):
            return None
        if kind == "insertText":
            text = _text(argument.get("text"))
            if not text:
                return None
            wanted.append(text)
        else:
            replacement = _text(argument.get("replaceText"))
            contains = argument.get("containsText")
            original = _text(contains.get("text")) if isinstance(contains, Mapping) else None
            if replacement is None or not original:
                return None
            if replacement:
                wanted.append(replacement)
            # A replacement is only proven when the old text has really gone.
            gone.append(original)
    if not wanted and not gone:
        return None
    return tuple(wanted), tuple(gone)


def _sheets_observable(payload: Mapping[str, object], spreadsheet: str) -> dict[str, object] | None:
    """What a spreadsheet must look like afterwards, or None if nobody can say."""

    requests = payload.get("requests")
    if not _is_list(requests) or not requests:
        return None
    expected: dict[str, object] = {"spreadsheetId": spreadsheet}
    sheets_expected: list[object] = []
    for request in requests:
        if not isinstance(request, Mapping) or len(request) != 1:
            return None
        ((kind, argument),) = request.items()
        if kind not in _OBSERVABLE_SHEETS_REQUESTS or not isinstance(argument, Mapping):
            return None
        properties = argument.get("properties")
        if not isinstance(properties, Mapping):
            return None
        title = _text(properties.get("title"))
        if not title:
            return None
        if kind == "addSheet":
            sheets_expected.append({"properties": {"title": title}})
        else:
            expected["properties"] = {"title": title}
    if sheets_expected:
        expected["sheets"] = sheets_expected
    return expected


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
        # The grantee is the whole point of a sharing change, so it is compared
        # too. Without it, an unrelated pre-existing permission of the same type
        # and role would satisfy a subset match and a grant that never landed —
        # or landed on the wrong person — would read as verified.
        grant = {
            key: value
            for key, value in payload.items()
            if key in {"type", "role", "domain", "emailAddress"}
        }
        if not {"type", "role"} <= set(grant):
            # A change that names neither who nor what cannot be proven at all.
            return None
        if grant.get("type") in {"user", "group"} and "emailAddress" not in grant:
            return None
        return ReadbackPlan(
            ReadbackRequest(
                f"{drive}/files/{resource_id}/permissions",
                {"fields": "permissions(id,type,role,domain,emailAddress)"},
            ),
            {"permissions": [grant]},
        )

    if operation == "docs_create":
        document = created or _text(body.get("documentId")) or resource_id
        if not document:
            return None
        expected = {"documentId": document}
        title = _text(payload.get("title"))
        if title:
            expected["title"] = title
        return ReadbackPlan(
            ReadbackRequest(
                f"{docs}/documents/{document}", {"fields": "documentId,title,revisionId"}
            ),
            expected,
        )

    if operation == "docs_batch_update":
        document = created or _text(body.get("documentId")) or resource_id
        if not document:
            return None
        observable = _docs_observable(payload)
        if observable is None:
            # At least one request in the batch changes something nothing can
            # look for afterwards. Reading the document would prove the
            # document exists, which is not what the batch asserted, so there
            # is no authoritative read and the caller must report `unknown`.
            return None
        wanted, replaced = observable
        return ReadbackPlan(
            ReadbackRequest(
                f"{docs}/documents/{document}",
                {"fields": "documentId,title,revisionId,body"},
            ),
            {"documentId": document},
            contains_text=wanted,
            excludes_text=replaced,
        )

    if operation == "sheets_create":
        spreadsheet = created or _text(body.get("spreadsheetId")) or resource_id
        if not spreadsheet:
            return None
        return ReadbackPlan(
            ReadbackRequest(f"{sheets}/spreadsheets/{spreadsheet}", {"includeGridData": "false"}),
            {"spreadsheetId": spreadsheet},
        )

    if operation == "sheets_batch_update":
        spreadsheet = created or _text(body.get("spreadsheetId")) or resource_id
        if not spreadsheet:
            return None
        expected_sheet = _sheets_observable(payload, spreadsheet)
        if expected_sheet is None:
            # Same reasoning as a document batch: most spreadsheet requests
            # change cell formatting or structure that a metadata read cannot
            # confirm, and confirming the spreadsheet still exists is not
            # verification of what was asked for.
            return None
        return ReadbackPlan(
            ReadbackRequest(f"{sheets}/spreadsheets/{spreadsheet}", {"includeGridData": "false"}),
            expected_sheet,
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
            {"range": entry["range"], "values": entry["values"]}
            for entry in data
            if isinstance(entry, Mapping) and _is_list(entry.get("values"))
        ]
        if len(expected_ranges) != len(data):
            # An entry whose values could not be read is an entry this readback
            # cannot prove, so the whole write stays unverified rather than
            # being judged on the part that happened to parse.
            return None
        return ReadbackPlan(
            ReadbackRequest(
                f"{sheets}/spreadsheets/{resource_id}/values:batchGet",
                {"ranges": ranges, "majorDimension": "ROWS"},
            ),
            # The range travels with the values, so a write that landed in the
            # wrong place no longer satisfies the comparison.
            {"valueRanges": expected_ranges},
        )

    if operation in {"contacts_create", "contacts_update"}:
        # A People resource is named by `resourceName`, so that is preferred
        # over the generic `id` the other APIs use: taking `id` first turned a
        # perfectly readable contact into an unprovable one.
        person = _text(body.get("resourceName")) or resource_id
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
    if plan_.contains_text or plan_.excludes_text:
        content = flatten_text(observed)
        if any(wanted not in content for wanted in plan_.contains_text):
            return ReadbackStatus.MISMATCH
        if any(gone in content for gone in plan_.excludes_text):
            return ReadbackStatus.MISMATCH
    for name, removed in plan_.absent.items():
        present = observed.get(name)
        if not _is_list(present):
            continue
        if any(any(matches(value, item) for item in present) for value in removed):
            # Something the mutation was supposed to remove is still there.
            return ReadbackStatus.MISMATCH
    return ReadbackStatus.VERIFIED
