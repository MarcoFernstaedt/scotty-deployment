"""The exact Discord permissions this deployment needs, and never more.

`Administrator` is the one bit the bot must not hold. It bypasses channel
overwrites, which are the whole mechanism keeping Trent's private channel
private from Mikey and both of them out of the maintainer route. A bot with
Administrator can read every private channel in the guild by construction, so
holding it would make the isolation this product promises unenforceable no
matter what the code above it does.

Everything the assistant needs is therefore expressed as ordinary named
permissions, one set per typed operation, and the required total is computed
from those sets rather than written down separately — so an operation cannot
quietly need something the invite link never asked for.
"""

from __future__ import annotations

from collections.abc import Mapping

from .config import RuntimeConfig
from .policy import Role

#: Discord permission bits, by their documented names. `ADMINISTRATOR` is
#: deliberately absent from this table: it has no legitimate use here.
PERMISSION_BITS: Mapping[str, int] = {
    "CREATE_INSTANT_INVITE": 1 << 0,
    "KICK_MEMBERS": 1 << 1,
    "BAN_MEMBERS": 1 << 2,
    "MANAGE_CHANNELS": 1 << 4,
    "MANAGE_GUILD": 1 << 5,
    "ADD_REACTIONS": 1 << 6,
    "VIEW_AUDIT_LOG": 1 << 7,
    "VIEW_CHANNEL": 1 << 10,
    "SEND_MESSAGES": 1 << 11,
    "MANAGE_MESSAGES": 1 << 13,
    "EMBED_LINKS": 1 << 14,
    "ATTACH_FILES": 1 << 15,
    "READ_MESSAGE_HISTORY": 1 << 16,
    "MENTION_EVERYONE": 1 << 17,
    "MANAGE_ROLES": 1 << 28,
    "MANAGE_WEBHOOKS": 1 << 29,
    "MANAGE_EVENTS": 1 << 33,
    "MODERATE_MEMBERS": 1 << 40,
    "SEND_MESSAGES_IN_THREADS": 1 << 38,
    "CREATE_PUBLIC_THREADS": 1 << 35,
    "CREATE_PRIVATE_THREADS": 1 << 36,
    "MANAGE_THREADS": 1 << 34,
}

#: Named only so it can be excluded and refused, never requested.
ADMINISTRATOR = 1 << 3

MANAGE_CHANNELS = PERMISSION_BITS["MANAGE_CHANNELS"]
MANAGE_ROLES = PERMISSION_BITS["MANAGE_ROLES"]
MANAGE_WEBHOOKS = PERMISSION_BITS["MANAGE_WEBHOOKS"]
MANAGE_EVENTS = PERMISSION_BITS["MANAGE_EVENTS"]
KICK_MEMBERS = PERMISSION_BITS["KICK_MEMBERS"]
BAN_MEMBERS = PERMISSION_BITS["BAN_MEMBERS"]
VIEW_CHANNEL = PERMISSION_BITS["VIEW_CHANNEL"]
SEND_MESSAGES = PERMISSION_BITS["SEND_MESSAGES"]
READ_MESSAGE_HISTORY = PERMISSION_BITS["READ_MESSAGE_HISTORY"]

#: Permissions that would let whoever holds them undo this deployment's own
#: isolation. They are never granted through an overwrite Scotty writes.
DANGEROUS_PERMISSIONS = (
    ADMINISTRATOR
    | MANAGE_ROLES
    | PERMISSION_BITS["MANAGE_GUILD"]
    | MANAGE_WEBHOOKS
    | PERMISSION_BITS["MENTION_EVERYONE"]
    | BAN_MEMBERS
    | KICK_MEMBERS
)


def _bits(*names: str) -> int:
    total = 0
    for name in names:
        total |= PERMISSION_BITS[name]
    return total


#: What each typed operation actually needs. Nothing here is a guess: an
#: operation that is not in this table has no permission requirement because
#: it is not an operation this deployment performs.
OPERATION_PERMISSIONS: Mapping[str, int] = {
    # Ordinary assistant work.
    "read_channel": _bits("VIEW_CHANNEL", "READ_MESSAGE_HISTORY"),
    "read_message": _bits("VIEW_CHANNEL", "READ_MESSAGE_HISTORY"),
    "send_message": _bits("VIEW_CHANNEL", "SEND_MESSAGES"),
    "reply_message": _bits("VIEW_CHANNEL", "SEND_MESSAGES", "READ_MESSAGE_HISTORY"),
    "edit_own_message": _bits("VIEW_CHANNEL", "SEND_MESSAGES"),
    "delete_own_message": _bits("VIEW_CHANNEL", "SEND_MESSAGES"),
    "add_reaction": _bits("VIEW_CHANNEL", "ADD_REACTIONS", "READ_MESSAGE_HISTORY"),
    "remove_own_reaction": _bits("VIEW_CHANNEL", "ADD_REACTIONS"),
    "attach_file": _bits("VIEW_CHANNEL", "SEND_MESSAGES", "ATTACH_FILES", "EMBED_LINKS"),
    "create_thread": _bits("VIEW_CHANNEL", "CREATE_PUBLIC_THREADS", "SEND_MESSAGES_IN_THREADS"),
    "send_thread_message": _bits("VIEW_CHANNEL", "SEND_MESSAGES_IN_THREADS"),
    "archive_own_thread": _bits("VIEW_CHANNEL", "MANAGE_THREADS"),
    "update_progress": _bits("VIEW_CHANNEL", "SEND_MESSAGES"),
    "announce": _bits("VIEW_CHANNEL", "SEND_MESSAGES"),
    "bulk_message": _bits("VIEW_CHANNEL", "SEND_MESSAGES"),
    # Administration, all of it consequence-gated above this layer.
    "create_channel": _bits("MANAGE_CHANNELS"),
    "edit_channel": _bits("MANAGE_CHANNELS"),
    "archive_channel": _bits("MANAGE_CHANNELS"),
    "create_category": _bits("MANAGE_CHANNELS"),
    "reorder_channels": _bits("MANAGE_CHANNELS"),
    "set_channel_permissions": _bits("MANAGE_CHANNELS", "MANAGE_ROLES"),
    "create_forum_post": _bits("VIEW_CHANNEL", "CREATE_PUBLIC_THREADS", "SEND_MESSAGES_IN_THREADS"),
    "assign_role": _bits("MANAGE_ROLES"),
    "remove_role": _bits("MANAGE_ROLES"),
    "create_event": _bits("MANAGE_EVENTS"),
    "create_webhook": _bits("MANAGE_WEBHOOKS"),
    "kick_member": _bits("KICK_MEMBERS"),
    "ban_member": _bits("BAN_MEMBERS"),
    "read_member_permissions": _bits("VIEW_CHANNEL"),
    "delete_message": _bits("VIEW_CHANNEL", "MANAGE_MESSAGES"),
}


def required_permissions() -> int:
    """Exactly the bits the bot invite needs, and no more.

    Computed from the operation table, so an operation can never depend on a
    permission the deployment did not ask for.
    """

    total = 0
    for bits in OPERATION_PERMISSIONS.values():
        total |= bits
    return total & ~ADMINISTRATOR


def permission_names(bits: int) -> tuple[str, ...]:
    """The documented names for a permission integer, in a fixed order."""

    return tuple(name for name, bit in sorted(PERMISSION_BITS.items()) if bits & bit)


def missing_permissions(granted: int, operation: str) -> tuple[str, ...]:
    """What this operation still needs, by name.

    `Administrator` is never accepted as a substitute: a guild that granted only
    that bit is reported as missing everything, because this deployment refuses
    to depend on the one permission that would break its own isolation.
    """

    needed = OPERATION_PERMISSIONS.get(operation, 0)
    if not needed:
        return ()
    return permission_names(needed & ~(granted & ~ADMINISTRATOR))


def role_is_assignable(
    *, bot_position: int, role_position: int, managed: bool, permissions: int = 0
) -> bool:
    """Whether the bot may hand out this role.

    Discord will not let a bot grant a role at or above its own highest role, and
    this deployment will not grant a managed role or one carrying a permission
    that could undo the isolation, whatever the hierarchy allows.
    """

    if managed or role_position >= bot_position:
        return False
    return not permissions & DANGEROUS_PERMISSIONS


def isolation_overwrites(config: RuntimeConfig, role: Role) -> tuple[dict[str, str], ...]:
    """The overwrites that make one client user's channel private.

    Everyone is denied by default and exactly one member is allowed, which is
    why the bot must not hold `Administrator`: the bit would bypass precisely
    this. The other client user and the maintainer route are never named, so a
    channel created this way cannot leak either of them.
    """

    principal = config.principal_for(role)
    member_allow = VIEW_CHANNEL | SEND_MESSAGES | READ_MESSAGE_HISTORY
    return (
        # In Discord's model the @everyone role carries the guild's own id.
        {"id": principal.guild_id, "type": "0", "allow": "0", "deny": str(member_allow)},
        {"id": principal.user_id, "type": "1", "allow": str(member_allow), "deny": "0"},
    )


__all__ = [
    "ADMINISTRATOR",
    "DANGEROUS_PERMISSIONS",
    "MANAGE_CHANNELS",
    "MANAGE_ROLES",
    "OPERATION_PERMISSIONS",
    "PERMISSION_BITS",
    "isolation_overwrites",
    "missing_permissions",
    "permission_names",
    "required_permissions",
    "role_is_assignable",
]
