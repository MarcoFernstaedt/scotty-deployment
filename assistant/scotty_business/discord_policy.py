"""Code-enforced classification for every Discord action Scotty may take.

Scotty is an ordinary, useful assistant inside the configured client guild, so
reading its channels, talking, replying, reacting, attaching an approved file,
and running task threads are routine and low-friction. Publishing to a shared
destination reaches a wider audience, and messaging in bulk is high-impact, so
both are approval-bound.

Everything else is absent rather than gated. Channel creation or deletion, role
and permission changes, moderation, webhooks, bot installation, destructive
history cleanup, and anything outside the configured guild have no operation
name here at all, so they classify as forbidden and fail closed. There is no
generic REST path to reach them by.

Two places deliberately keep the narrower accepted boundary rather than the
looser reading of the operating model: a mass mention is forbidden outright,
because the accepted release never parses mentions and therefore cannot ping a
role or a server; and a destination outside the caller's own permitted set is
forbidden rather than approvable, because one client user must never reach the
other's private channel by any route, including an approval.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from enum import StrEnum

from .config import RuntimeConfig
from .policy import Principal


class DiscordActionClass(StrEnum):
    ROUTINE = "routine"
    CONSEQUENCE = "consequence"
    FORBIDDEN = "forbidden"


#: Ordinary reversible assistant work inside the configured guild.
ROUTINE_DISCORD_OPERATIONS = frozenset(
    {
        "read_channel",
        "read_message",
        "send_message",
        "edit_own_message",
        "delete_own_message",
        "reply_message",
        "add_reaction",
        "remove_own_reaction",
        "attach_file",
        "create_thread",
        "send_thread_message",
        "archive_own_thread",
        "update_progress",
    }
)

#: Wider audience or high-impact volume: allowed, but only through approval.
CONSEQUENCE_DISCORD_OPERATIONS = frozenset({"announce", "bulk_message"})

#: The file types Scotty may attach, and the size it may attach.
APPROVED_ATTACHMENT_SUFFIXES = frozenset({".txt", ".md", ".csv", ".json", ".pdf", ".png", ".jpg"})
MAX_ATTACHMENT_BYTES = 8_000_000
MAX_MESSAGE_CHARS = 2000

#: More individual mentions than this is a new audience, not a reply.
MAX_MENTIONS = 10

#: More messages than this from one caller inside the window below is bulk
#: messaging, whatever the caller claims.
BULK_MESSAGE_THRESHOLD = 5
BULK_WINDOW_SECONDS = 60.0

_MASS_MENTION = re.compile(r"@(?:everyone|here)\b")
_USER_MENTION = re.compile(r"<@[!&]?[0-9]{17,20}>")


def permitted_destinations(config: RuntimeConfig, principal: Principal) -> frozenset[str]:
    """The channels this caller may act in routinely: their own, and only theirs.

    A shared destination is deliberately absent. Reaching one is publishing, and
    publishing goes through the approval path so it cannot skip the leak check
    that guards it. The other client user's private channel is never included, so
    neither can view, post into, or cross-route into the other's session.
    """

    del config
    return frozenset({principal.channel_id})


def shared_destinations(config: RuntimeConfig) -> frozenset[str]:
    """The configured shared destinations, reachable only by an approved publish."""

    return frozenset(config.announcement_channel_ids)


def _text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    return value if type(value) is str else ""


#: Exactly the filenames the multipart transport will accept, so an attachment
#: is refused during classification instead of failing opaquely at upload.
ATTACHMENT_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _attachment_ok(payload: Mapping[str, object]) -> bool:
    name = _text(payload, "filename")
    size = payload.get("size_bytes")
    if not ATTACHMENT_FILENAME.fullmatch(name):
        return False
    if not any(name.lower().endswith(suffix) for suffix in APPROVED_ATTACHMENT_SUFFIXES):
        return False
    return type(size) is int and 0 < size <= MAX_ATTACHMENT_BYTES


def classify_discord_action(
    operation: object,
    payload: object,
    *,
    destinations: Iterable[str],
    shared: Iterable[str] = (),
) -> DiscordActionClass:
    """Classify one exact Discord action before any provider call."""

    if type(operation) is not str or not isinstance(payload, Mapping):
        return DiscordActionClass.FORBIDDEN
    known = operation in ROUTINE_DISCORD_OPERATIONS or operation in CONSEQUENCE_DISCORD_OPERATIONS
    if not known:
        return DiscordActionClass.FORBIDDEN

    allowed = frozenset(destinations)
    publishable = frozenset(shared)
    channel_id = _text(payload, "channel_id")
    if not channel_id:
        return DiscordActionClass.FORBIDDEN
    # A shared destination is reachable only by publishing to it, never by an
    # ordinary send that would skip the announcement leak check.
    publishing = channel_id in publishable and operation in CONSEQUENCE_DISCORD_OPERATIONS
    if channel_id not in allowed and not publishing:
        return DiscordActionClass.FORBIDDEN

    content = _text(payload, "content")
    if len(content) > MAX_MESSAGE_CHARS:
        return DiscordActionClass.FORBIDDEN
    if _MASS_MENTION.search(content):
        # The accepted boundary never parses mentions, so this is absent, not
        # approvable: an approval would authorize something Scotty cannot do.
        return DiscordActionClass.FORBIDDEN
    if operation == "attach_file" and not _attachment_ok(payload):
        return DiscordActionClass.FORBIDDEN

    if operation in CONSEQUENCE_DISCORD_OPERATIONS:
        return DiscordActionClass.CONSEQUENCE
    count = payload.get("message_count")
    if type(count) is int and count > BULK_MESSAGE_THRESHOLD:
        return DiscordActionClass.CONSEQUENCE
    if len(_USER_MENTION.findall(content)) > MAX_MENTIONS:
        return DiscordActionClass.CONSEQUENCE
    return DiscordActionClass.ROUTINE


def private_identifiers(config: RuntimeConfig) -> tuple[str, ...]:
    """Every identifier that must never appear in a shared destination."""

    return (
        config.maintainer_route.guild_id,
        config.maintainer_route.channel_id,
        config.maintainer_route.user_id,
        config.maintainer_route.profile,
        *(principal.channel_id for principal in config.principals),
        *(principal.user_id for principal in config.principals),
    )


#: Shapes that mean a credential reached text that is about to be published.
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b[0-9]{8,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:api[_ -]?key|token|secret|password)\s*(?:is|=|:)\s*\S{12,}", re.I),
    re.compile(r"\bya29\.[A-Za-z0-9._\-]{20,}\b"),
)


def announcement_is_safe(content: object, config: RuntimeConfig) -> bool:
    """Whether this text may leave a private channel for a shared destination.

    Private-channel identifiers, the maintainer route, and anything credential
    shaped are refused. Nothing that fails this check is published, and the
    reason reported to the caller never repeats the offending text.
    """

    if type(content) is not str or not content.strip():
        return False
    if any(pattern.search(content) for pattern in _CREDENTIAL_PATTERNS):
        return False
    return all(identifier not in content for identifier in private_identifiers(config))


def redacted_refusal(operation: object, classified: DiscordActionClass) -> str:
    """A fixed refusal that never repeats the payload it refused.

    The class decides the wording, not the operation name: an ordinary send that
    became bulk still needs an approval, and saying so is more useful than
    claiming Scotty cannot send at all.
    """

    del operation
    if classified is DiscordActionClass.CONSEQUENCE:
        return "that Discord action needs an approved proposal"
    return "that Discord action is not one Scotty performs"
