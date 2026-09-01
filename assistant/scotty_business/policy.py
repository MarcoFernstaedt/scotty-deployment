from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

ADDON_CAP_RESPONSE = (
    "Scotty is capped at six add-ons for this VPS deployment. To add another, "
    "an existing add-on must be removed. Scotty cannot bypass this limit."
)
CODING_REFUSAL = (
    "I don’t build code, extensions, or integrations. Please contact Marco for that work."
)
FIXED_WIZARD_COMMAND = "Scotty, send Trent the setup wizard."


def setup_wizard(assistant_name: str = "your assistant") -> str:
    """The fixed onboarding text, addressed from this reader's own assistant."""

    return (
        f"Welcome. This is {assistant_name}, your private assistant.\n"
        "1. Use this private channel for your own requests and non-secret preferences.\n"
        "2. Confirm which configured Discord channels, Trello board, Google Workspace "
        "resources, GoHighLevel location, and RentCast reads you expect to use.\n"
        "3. Google Workspace access uses provider-owned browser consent; credentials and "
        "OAuth codes are entered only through local setup, never here.\n"
        f"4. {assistant_name} shows external sends and writes for approval before execution.\n"
        f"5. Never paste credentials here. {assistant_name} cannot accept them in this chat. "
        "If one appears, rotate it and use local setup.\n"
        "6. Property and financial analysis is preliminary; verify it with the appropriate "
        "qualified professional."
    )


def employee_summary(assistant_name: str = "your assistant") -> str:
    """What an employee's own assistant says it can do for them."""

    return (
        f"{assistant_name} can read configured business resources, prepare drafts and "
        "analysis, create private reminders, and propose changes. Employee proposals "
        "require main-operator or maintainer approval. Never paste credentials in this chat."
    )


#: Kept for the fixed maintainer-triggered wizard, whose reader is Trent.
SETUP_WIZARD = setup_wizard("Scotty")
EMPLOYEE_SUMMARY = employee_summary("your assistant")


class Role(StrEnum):
    MAINTAINER = "maintainer"
    MAIN_OPERATOR = "main_operator"
    EMPLOYEE = "employee"


@dataclass(frozen=True, slots=True)
class Principal:
    guild_id: str
    channel_id: str
    user_id: str
    role: Role

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.guild_id, self.channel_id, self.user_id, self.role.value)


_APPROVAL_ACTIONS = frozenset(
    {"trello_write", "ghl_sms", "discord_announcement", "google_workspace_consequence"}
)


def authorize_source(
    principals: Sequence[Principal],
    guild_id: object,
    channel_id: object,
    user_id: object,
    parent_channel_id: object | None,
) -> Principal | None:
    """Return the exact authorized principal or fail closed.

    A thread is authorized only when its parent is the principal's configured
    channel. The thread ID itself never broadens another tuple.
    """
    if not all(type(value) is str and value for value in (guild_id, channel_id, user_id)):
        return None
    if parent_channel_id is not None and (
        type(parent_channel_id) is not str or not parent_channel_id
    ):
        return None
    for principal in principals:
        expected_channel = parent_channel_id or channel_id
        if (
            principal.guild_id == guild_id
            and principal.channel_id == expected_channel
            and principal.user_id == user_id
        ):
            return principal
    return None


def can_approve(principal: Principal, action_class: object) -> bool:
    return (
        type(action_class) is str
        and action_class in _APPROVAL_ACTIONS
        and principal.role in {Role.MAINTAINER, Role.MAIN_OPERATOR}
    )


def enforce_addon_cap(installed: Iterable[str], requested: str) -> list[str]:
    current = list(installed)
    if requested in current:
        return current
    if len(current) >= 6:
        raise ValueError(ADDON_CAP_RESPONSE)
    return [*current, requested]
