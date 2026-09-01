from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from enum import StrEnum


class GoogleActionClass(StrEnum):
    ROUTINE = "routine"
    CONSEQUENCE = "consequence"
    FORBIDDEN = "forbidden"


ROUTINE_GOOGLE_OPERATIONS = frozenset(
    {
        "gmail_modify_labels",
        "gmail_create_draft",
        "gmail_update_draft",
        "calendar_create_event",
        "calendar_update_event",
        "calendar_cancel_event",
        "drive_create_file",
        "drive_update_file",
        "drive_move_file",
        "drive_trash_file",
        "docs_create",
        "docs_batch_update",
        "sheets_create",
        "sheets_batch_update",
        "sheets_update_values",
        "contacts_create",
        "contacts_update",
    }
)

CONSEQUENCE_GOOGLE_OPERATIONS = frozenset(
    {
        "gmail_send_draft",
        "drive_delete_permanently",
        "drive_change_permissions",
        "contacts_delete",
    }
)

#: Fields that introduce or widen an audience for a calendar action.
_NEW_AUDIENCE_FIELDS = frozenset(
    {"attendees", "anyoneCanAddSelf", "guestsCanInviteOthers", "conferenceData"}
)

#: Fields whose entries each name a separate target or grantee. Editing one
#: resource many times is ordinary work; naming many targets at once is bulk.
_TARGET_LIST_FIELDS = frozenset(
    {
        "ids",
        "messageIds",
        "threadIds",
        "fileIds",
        "resourceNames",
        "recipients",
        "permissions",
        "members",
        "attendees",
    }
)

#: More than this many named targets in one action is bulk mutation.
BULK_TARGET_THRESHOLD = 25

#: Operation-aware ceilings on the units each operation actually affects. A
#: small bounded edit stays routine; past the ceiling the edit is bulk and goes
#: to the approval ledger like any other high-impact mutation.
MAX_DOCS_REQUESTS = 25
MAX_SHEETS_REQUESTS = 25
MAX_SHEETS_RANGES = 10
MAX_SHEETS_CELLS = 5_000
MAX_DRIVE_PARENTS = 5
MAX_CONTACT_ENTRIES = 25

#: Batch request kinds that remove content or structure rather than add it.
_DESTRUCTIVE_REQUESTS = frozenset(
    {
        "deleteContentRange",
        "deleteFooter",
        "deleteHeader",
        "deleteNamedRange",
        "deletePositionedObject",
        "deleteTableColumn",
        "deleteTableRow",
        "deleteBanding",
        "deleteConditionalFormatRule",
        "deleteDeveloperMetadata",
        "deleteDimension",
        "deleteDimensionGroup",
        "deleteDuplicates",
        "deleteEmbeddedObject",
        "deleteFilterView",
        "deleteProtectedRange",
        "deleteRange",
        "deleteSheet",
        "cutPaste",
        "randomizeRange",
    }
)

#: Anything naming sharing or access, wherever it appears in a payload.
_PERMISSION_FIELDS = frozenset(
    {
        "permissions",
        "permissionIds",
        "permissionId",
        "role",
        "sharingUser",
        "writersCanShare",
        "copyRequiresWriterPermission",
        "viewersCanCopyContent",
        "addProtectedRange",
        "updateProtectedRange",
    }
)

#: A payload larger or deeper than this is not a bounded Scotty action.
MAX_PAYLOAD_BYTES = 262_144
MAX_PAYLOAD_DEPTH = 12
MAX_PAYLOAD_KEYS = 100


def _walk(value: object, depth: int) -> tuple[int, int]:
    """Return (max depth reached, largest named-target list length)."""

    if depth > MAX_PAYLOAD_DEPTH:
        return depth, 0
    deepest = depth
    targets = 0
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                type(key) is str
                and key in _TARGET_LIST_FIELDS
                and isinstance(item, Sequence)
                and not isinstance(item, str | bytes)
            ):
                targets = max(targets, len(item))
            child_depth, child_targets = _walk(item, depth + 1)
            deepest = max(deepest, child_depth)
            targets = max(targets, child_targets)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            child_depth, child_targets = _walk(item, depth + 1)
            deepest = max(deepest, child_depth)
            targets = max(targets, child_targets)
    return deepest, targets


def _contains_field(value: object, fields: frozenset[str], depth: int = 0) -> bool:
    if depth > MAX_PAYLOAD_DEPTH:
        return False
    if isinstance(value, Mapping):
        if fields & {key for key in value if type(key) is str}:
            return True
        return any(_contains_field(item, fields, depth + 1) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return any(_contains_field(item, fields, depth + 1) for item in value)
    return False


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return value
    return ()


def _sheets_cells(payload: Mapping[str, object]) -> int:
    """Total cells a values update would write across every named range."""

    total = 0
    for entry in _sequence(payload.get("data")):
        if not isinstance(entry, Mapping):
            continue
        for row in _sequence(entry.get("values")):
            total += max(len(_sequence(row)), 1)
    for row in _sequence(payload.get("values")):
        total += max(len(_sequence(row)), 1)
    return total


def affected_units(
    operation: str, payload: Mapping[str, object]
) -> tuple[tuple[str, int, int], ...]:
    """Report (unit, count, limit) for every unit this operation affects."""

    counts: list[tuple[str, int, int]] = []
    if operation == "docs_batch_update":
        counts.append(("requests", len(_sequence(payload.get("requests"))), MAX_DOCS_REQUESTS))
    elif operation == "sheets_batch_update":
        counts.append(("requests", len(_sequence(payload.get("requests"))), MAX_SHEETS_REQUESTS))
    elif operation == "sheets_update_values":
        counts.append(("ranges", len(_sequence(payload.get("data"))), MAX_SHEETS_RANGES))
        counts.append(("cells", _sheets_cells(payload), MAX_SHEETS_CELLS))
    elif operation in {"drive_move_file", "drive_update_file", "drive_create_file"}:
        parents = 0
        for field in ("addParents", "removeParents", "parents"):
            value = payload.get(field)
            if type(value) is str:
                parents = max(parents, len([item for item in value.split(",") if item]))
            else:
                parents = max(parents, len(_sequence(value)))
        counts.append(("parents", parents, MAX_DRIVE_PARENTS))
    elif operation in {"contacts_create", "contacts_update"}:
        entries = max(
            (len(_sequence(value)) for value in payload.values() if not isinstance(value, str)),
            default=0,
        )
        counts.append(("entries", entries, MAX_CONTACT_ENTRIES))
    return tuple(counts)


def _has_destructive_request(payload: Mapping[str, object]) -> bool:
    """Whether any batch request removes content or structure."""

    for request in _sequence(payload.get("requests")):
        if isinstance(request, Mapping) and _DESTRUCTIVE_REQUESTS & {
            key for key in request if type(key) is str
        }:
            return True
    return False


def _payload_bytes(payload: Mapping[str, object]) -> int | None:
    """Serialized payload size, or None when the payload is not serializable."""

    try:
        return len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return None


def classify_google_action(operation: object, payload: object) -> GoogleActionClass:
    """Classify one exact Workspace action before any provider call.

    Broad OAuth consent is not autonomous authority. Administrative, credential,
    billing, oversized, and unknown actions stay absent and fail closed.

    Bulk is measured two ways, because both matter: how many separate targets an
    action names, and how many units it changes inside one resource. A small
    bounded edit to a document or sheet stays routine so ordinary work is not
    interrupted; a large batch, a destructive batch request, or anything naming
    sharing or access goes to the approval ledger.
    """

    if type(operation) is not str or not isinstance(payload, Mapping):
        return GoogleActionClass.FORBIDDEN
    known = operation in CONSEQUENCE_GOOGLE_OPERATIONS or operation in ROUTINE_GOOGLE_OPERATIONS
    if not known:
        return GoogleActionClass.FORBIDDEN

    size = _payload_bytes(payload)
    if size is None or size > MAX_PAYLOAD_BYTES:
        return GoogleActionClass.FORBIDDEN
    depth, targets = _walk(payload, 0)
    if depth > MAX_PAYLOAD_DEPTH:
        return GoogleActionClass.FORBIDDEN

    if operation in CONSEQUENCE_GOOGLE_OPERATIONS:
        return GoogleActionClass.CONSEQUENCE
    if targets > BULK_TARGET_THRESHOLD:
        return GoogleActionClass.CONSEQUENCE
    if _contains_field(payload, _PERMISSION_FIELDS):
        # Sharing and access changes are gated wherever they appear, not only
        # through the dedicated permissions operation.
        return GoogleActionClass.CONSEQUENCE
    if _has_destructive_request(payload):
        return GoogleActionClass.CONSEQUENCE
    if any(count > limit for _, count, limit in affected_units(operation, payload)):
        return GoogleActionClass.CONSEQUENCE
    if operation.startswith("calendar_") and _contains_field(payload, _NEW_AUDIENCE_FIELDS):
        return GoogleActionClass.CONSEQUENCE
    if len(payload) > MAX_PAYLOAD_KEYS:
        return GoogleActionClass.CONSEQUENCE
    return GoogleActionClass.ROUTINE
