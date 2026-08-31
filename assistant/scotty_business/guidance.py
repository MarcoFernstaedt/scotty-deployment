"""Deterministic, credential-free provider setup guidance.

Every string here is fixed text. Scotty states whether a provider is connected,
explains the provider-side steps and the exact identifiers and scopes an operator
must gather, and points at the local setup command. It never asks for a
credential in Discord and never accepts one from chat.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

NOT_CONNECTED = "not connected"
CONNECTED = "connected"
LOCAL_SETUP_DIRECTIVE = (
    "Finish this by running the local setup command on the server. "
    "Never put a key, token, or password in Discord; if one appears here, rotate it."
)

PROVIDERS: tuple[str, ...] = ("discord", "trello", "ghl", "rentcast", "google_workspace")


@dataclass(frozen=True, slots=True)
class ProviderGuidance:
    provider: str
    display_name: str
    status: str
    summary: str
    required_ids: tuple[str, ...]
    required_scopes: tuple[str, ...]
    steps: tuple[str, ...]
    apis: tuple[str, ...] = ()
    callback: str = ""

    def as_text(self) -> str:
        lines = [f"{self.display_name}: {self.status}", self.summary]
        if self.required_ids:
            lines.append("Identifiers to collect:")
            lines.extend(f"  - {item}" for item in self.required_ids)
        if self.apis:
            lines.append("APIs or products to enable:")
            lines.extend(f"  - {item}" for item in self.apis)
        if self.required_scopes:
            lines.append("Permissions or scopes to grant:")
            lines.extend(f"  - {item}" for item in self.required_scopes)
        if self.callback:
            lines.append(f"Callback or redirect: {self.callback}")
        lines.append("Provider-side steps:")
        lines.extend(f"  {index}. {item}" for index, item in enumerate(self.steps, start=1))
        lines.append(LOCAL_SETUP_DIRECTIVE)
        return "\n".join(lines)


_DEFINITIONS: Mapping[str, Mapping[str, object]] = {
    "discord": {
        "display_name": "Discord",
        "summary": (
            "Discord carries every conversation. One application serves the configured "
            "private channels and nothing else."
        ),
        "required_ids": (
            "the server (guild) ID",
            "one private channel ID per person",
            "each person's Discord user ID",
        ),
        "required_scopes": (
            "bot scope when inviting the application",
            "Manage Channels in the server, so setup can create the private channels",
            "View Channel, Send Messages, Read Message History, and Embed Links in each channel",
            "Message Content Intent enabled on the application",
        ),
        "apis": ("the Discord bot application, with no OAuth2 redirect flow",),
        "callback": (
            "None. Scotty connects out to Discord as a bot and exposes no public port, "
            "webhook, or redirect URI."
        ),
        "steps": (
            "Create the application and its bot user in the Discord developer portal.",
            "Enable Message Content Intent on the bot page.",
            "Invite the bot to the server with the bot scope and Manage Channels only.",
            "Turn on Developer Mode in Discord to copy the server, channel, and user IDs.",
            "Keep each private channel closed to @everyone; setup verifies this and stops "
            "if a channel is visible.",
        ),
    },
    "trello": {
        "display_name": "Trello",
        "summary": (
            "Trello holds the working board. Scotty reads it and proposes changes; every "
            "write waits for an approval bound to the exact proposal."
        ),
        "required_ids": (
            "the Trello board ID",
            "each list ID Scotty may use",
            "any label IDs and custom-field IDs in scope",
        ),
        "required_scopes": (
            "read and write on the configured board only",
            "no workspace, member, or board administration",
        ),
        "apis": ("the Trello REST API, through a Power-Up API key and a matching token",),
        "callback": "None. The token is issued in the browser and entered only through setup.",
        "steps": (
            "Open the Power-Up admin page and create an API key for the workspace.",
            "Generate a token for that key from the account that owns the board.",
            "Open the board and append .json to its URL to read the board, list, label, "
            "and custom-field IDs.",
            "Keep the key and token off Discord and out of shell history.",
        ),
    },
    "ghl": {
        "display_name": "GoHighLevel",
        "summary": (
            "GoHighLevel is the messaging layer for one internal sub-account. Outbound SMS "
            "is always approval-bound and is verified from the conversation record."
        ),
        "required_ids": (
            "the sub-account location ID",
            "the contact IDs that are in scope",
        ),
        "required_scopes": (
            "a Private Integration Token for the single internal sub-account",
            "contacts read, conversations read, and conversations message write",
            "no marketplace OAuth application and no public webhook endpoint",
        ),
        "apis": ("the GoHighLevel v2 contacts and conversations APIs for one sub-account",),
        "callback": (
            "None. A Private Integration Token needs no redirect URI, and Scotty registers "
            "no inbound webhook."
        ),
        "steps": (
            "Open the sub-account settings and create a Private Integration.",
            "Grant only the contacts and conversations scopes listed above.",
            "Copy the location ID from the sub-account settings page.",
            "Keep the integration limited to the one internal sub-account.",
        ),
    },
    "rentcast": {
        "display_name": "RentCast",
        "summary": (
            "RentCast supplies property records and estimates. It is read-only, and every "
            "figure Scotty reports keeps its source and retrieval time."
        ),
        "required_ids": ("the exact endpoint paths that are in scope",),
        "required_scopes": (
            "a read-only API key",
            "property records, value estimates, rent estimates, and comparables only",
        ),
        "apis": ("the RentCast v1 property, value, and rent endpoints only",),
        "callback": "None. RentCast is a read-only key on outbound calls.",
        "steps": (
            "Create an account and issue an API key in the RentCast dashboard.",
            "Confirm the plan covers property data and the valuation endpoints.",
            "Record the endpoint paths Scotty is allowed to call.",
            "No other property website is ever used, and nothing is scraped.",
        ),
    },
    "google_workspace": {
        "display_name": "Google Workspace",
        "summary": (
            "Google Workspace is a bounded release capability over one configured account. "
            "Day-to-day Gmail, Calendar, Drive, Docs, Sheets, and Contacts work is ordinary "
            "and reversible, so it does not stop for approval. Exact sends, new audiences, "
            "permanent deletion, sharing or permission changes, admin, account-security and "
            "billing actions, and bulk mutation are each approval-bound."
        ),
        "required_ids": (
            "the Workspace account email Scotty is authorized to act in",
            "no per-file, per-label, or per-calendar list: consent covers the account, and "
            "code decides what is routine and what needs approval",
        ),
        "apis": (
            "Gmail API",
            "Google Calendar API",
            "Google Drive API",
            "Google Docs API",
            "Google Sheets API",
            "People API",
        ),
        "required_scopes": (
            "openid and email, to bind consent to the exact account",
            "the Gmail modify, Calendar, Drive, Docs, Sheets, and Contacts product scopes",
            "no Admin SDK, no directory, no billing, and no https://mail.google.com/ "
            "permanent-delete scope",
        ),
        "callback": (
            "Google's installed-app loopback. Consent opens in a browser on the server and "
            "returns to http://127.0.0.1:<port>/oauth2/callback, a port chosen at run time. "
            "Add no public redirect URI, and never copy an authorization code into Discord."
        ),
        "steps": (
            "Create or select a Google Cloud project for this deployment.",
            "Enable the six APIs listed above on that project.",
            "Configure the OAuth consent screen and add the Workspace account as a user.",
            "Create an OAuth client of type Desktop app and download its client material.",
            "Place that client material only in the documented owner-only local path.",
            "Run local setup and complete Google's browser consent as the exact account; "
            "Scotty refuses the result if the account or granted scopes differ.",
            "Keep authorization codes and token state out of Discord, logs, and model context.",
        ),
    },
}


def provider_guidance(provider: str, *, connected: bool = False) -> ProviderGuidance:
    """Return the fixed guidance for one release provider."""

    definition = _DEFINITIONS[provider]
    return ProviderGuidance(
        provider=provider,
        display_name=str(definition["display_name"]),
        status=CONNECTED if connected else NOT_CONNECTED,
        summary=str(definition["summary"]),
        required_ids=tuple(definition["required_ids"]),  # type: ignore[arg-type]
        required_scopes=tuple(definition["required_scopes"]),  # type: ignore[arg-type]
        steps=tuple(definition["steps"]),  # type: ignore[arg-type]
        apis=tuple(definition.get("apis", ())),  # type: ignore[arg-type]
        callback=str(definition.get("callback", "")),
    )


def provider_status(connected: Mapping[str, bool]) -> dict[str, bool]:
    """Normalize a connection map to exactly the release providers."""

    return {name: bool(connected.get(name, False)) for name in PROVIDERS}


def all_provider_guidance_text(connected: Mapping[str, bool] | None = None) -> str:
    """Render every provider's guidance in a fixed order."""

    status = provider_status(connected or {})
    return "\n\n".join(
        provider_guidance(name, connected=status[name]).as_text() for name in PROVIDERS
    )
