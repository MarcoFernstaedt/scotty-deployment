from __future__ import annotations

from collections.abc import Mapping
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

_NEW_AUDIENCE_FIELDS = frozenset(
    {"attendees", "anyoneCanAddSelf", "guestsCanInviteOthers", "conferenceData"}
)


def classify_google_action(operation: object, payload: object) -> GoogleActionClass:
    """Classify one exact Workspace action before any provider call.

    Broad OAuth consent is not autonomous authority. Administrative, credential,
    billing, bulk, and unknown actions stay absent and fail closed.
    """

    if type(operation) is not str or not isinstance(payload, Mapping):
        return GoogleActionClass.FORBIDDEN
    if operation in CONSEQUENCE_GOOGLE_OPERATIONS:
        return GoogleActionClass.CONSEQUENCE
    if operation not in ROUTINE_GOOGLE_OPERATIONS:
        return GoogleActionClass.FORBIDDEN
    if operation.startswith("calendar_") and _NEW_AUDIENCE_FIELDS & set(payload):
        return GoogleActionClass.CONSEQUENCE
    if len(payload) > 100:
        return GoogleActionClass.CONSEQUENCE
    return GoogleActionClass.ROUTINE
