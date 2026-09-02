# Discord permissions this deployment needs

The bot runs **without** the `Administrator` permission, deliberately.
`Administrator` bypasses channel permission overwrites, and those overwrites are
the whole mechanism keeping each client user's private channel private from the
other and both of them out of the maintainer route. A bot holding it could read
every private channel in the guild by construction, so the isolation this
product promises would be unenforceable no matter what the code above it did.

Everything the assistant does is therefore reachable with ordinary named
permissions. When one is missing, the assistant says which one rather than
failing with an opaque error.

## The exact permission set

Invite the bot with this permission integer:

    335812881494

which is exactly:

| Permission |
| --- |
| `ADD_REACTIONS` |
| `ATTACH_FILES` |
| `BAN_MEMBERS` |
| `CREATE_PUBLIC_THREADS` |
| `EMBED_LINKS` |
| `KICK_MEMBERS` |
| `MANAGE_CHANNELS` |
| `MANAGE_EVENTS` |
| `MANAGE_MESSAGES` |
| `MANAGE_ROLES` |
| `MANAGE_THREADS` |
| `MANAGE_WEBHOOKS` |
| `READ_MESSAGE_HISTORY` |
| `SEND_MESSAGES` |
| `SEND_MESSAGES_IN_THREADS` |
| `VIEW_CHANNEL` |

Nothing else is requested, and `Administrator` is not in the permission table
this deployment can even name.

## What each thing needs

| Work | Permissions |
| --- | --- |
| Reading a channel, replying, editing or deleting its own message | `VIEW_CHANNEL`, `SEND_MESSAGES`, `READ_MESSAGE_HISTORY` |
| Reactions | `ADD_REACTIONS` |
| Attachments | `ATTACH_FILES`, `EMBED_LINKS` |
| Threads and forum posts | `CREATE_PUBLIC_THREADS`, `SEND_MESSAGES_IN_THREADS`, `MANAGE_THREADS` |
| Creating, editing, archiving and ordering channels and categories | `MANAGE_CHANNELS` |
| Setting channel permissions | `MANAGE_CHANNELS`, `MANAGE_ROLES` |
| Assigning or removing a role | `MANAGE_ROLES` |
| Scheduled events and livestream reminders | `MANAGE_EVENTS` |
| Webhooks | `MANAGE_WEBHOOKS` |
| Moderation | `KICK_MEMBERS`, `BAN_MEMBERS` |
| Deleting someone else's message during approved cleanup | `MANAGE_MESSAGES` |

## The exact operations

These are the names the code dispatches, not a description of them. Anything
not on one of these lists is refused as unknown rather than attempted.

**Routine — no approval.** `read_channel`, `read_message`, `send_message`,
`edit_own_message`, `delete_own_message`, `reply_message`, `add_reaction`,
`remove_own_reaction`, `attach_file`, `create_thread`, `send_thread_message`,
`archive_own_thread`, `update_progress`.

**Consequence — approved first.** `announce`, `bulk_message`.

**Administration — approved first, and every one read back afterwards.**
`create_channel`, `edit_channel`, `archive_channel`, `create_category`,
`reorder_channels`, `set_channel_permissions`, `create_forum_post`,
`assign_role`, `remove_role`, `create_event`, `create_webhook`, `kick_member`,
`ban_member`, `read_member_permissions`.

Timing a member out is deliberately absent: `MODERATE_MEMBERS` is not among the
permissions this deployment requests, so there is no operation for it. Adding
one means widening the invite, which is an operator decision.

`tests/test_documented_truth.py` holds these three lists against the policy's
own sets, so an operation added to the code and not to this page fails there.

## Role hierarchy

Discord will not let a bot grant a role at or above its own highest role, so
place the bot's role above any role it should be able to assign. On top of that,
this deployment refuses to assign a managed role, or any role carrying
`Administrator`, `MANAGE_ROLES`, `MANAGE_GUILD`, `MANAGE_WEBHOOKS`,
`MENTION_EVERYONE`, `KICK_MEMBERS`, or `BAN_MEMBERS` — a role that could undo
the isolation is never handed out, whatever the hierarchy allows.

## Private channels

A client user's private channel is created denying `@everyone` and allowing
exactly that one member. Those channels are not administrable through the
assistant at all: renaming, archiving, or repermissioning one is refused
outright rather than sent for approval, because an approval that could reach
another user's private channel would defeat the isolation it is meant to guard.

## Administration is always approval-bound

Every administrative action — creating or editing a channel, setting
permissions, assigning a role, scheduling an event, creating a webhook, kicking
or banning — is a consequence action. It is proposed, approved by the main
operator or the maintainer, and then executed and read back. Ordinary
conversation, reading, replying, drafting and thread work stays low-friction and
needs no approval at all.
