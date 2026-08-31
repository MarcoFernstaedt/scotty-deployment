from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Protocol, overload

from .adapters import (
    DiscordAdapter,
    GHLAdapter,
    GoogleWorkspaceAdapter,
    HttpTransport,
    ProviderRecord,
    RentCastAdapter,
    TrelloAdapter,
)
from .approvals import ApprovalStore, Proposal
from .config import ConfigError, RuntimeConfig
from .credential_intake import BROKER_SOCKET, CredentialIntake, UnixSocketBroker
from .google_oauth import GoogleOAuthError, GoogleTokenStore, ensure_access_token
from .guidance import PROVIDERS, provider_guidance, provider_status
from .identity import AuthorizedPrincipalResolver
from .ingress import IngressGuard
from .policy import Principal
from .reminders import Reminder, ReminderStore, ReminderWorker
from .self_repair import SelfRepairError, SelfRepairManager
from .service import GHLPort, RentCastPort, ScottyService, TrelloPort

logger = logging.getLogger(__name__)


class RuntimeUnavailable(RuntimeError):
    pass


def _home_path() -> Path:
    configured = os.environ.get("HERMES_HOME")
    return Path(configured) if configured else Path.home() / ".hermes"


def _load_private_config(home: Path) -> RuntimeConfig:
    path = home / "scotty" / "private.json"
    if path.is_symlink() or not path.is_file():
        raise RuntimeUnavailable("private runtime configuration is unavailable")
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise RuntimeUnavailable("private runtime configuration is unavailable") from exc
    if len(raw_bytes) > 65_536:
        raise RuntimeUnavailable("private runtime configuration is oversized")
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeUnavailable("private runtime configuration is malformed") from exc
    if not isinstance(raw, dict):
        raise RuntimeUnavailable("private runtime configuration is malformed")
    try:
        return RuntimeConfig.from_mapping(raw)
    except ConfigError as exc:
        raise RuntimeUnavailable("private runtime configuration is invalid") from exc


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeUnavailable("a required credential is unavailable")
    return value


class ProviderNotConnected(RuntimeError):
    """A provider has no configured credential, so no call is attempted."""


class TrelloReadPort(TrelloPort, Protocol):
    def list_cards(self) -> Sequence[ProviderRecord]: ...


class GHLReadPort(GHLPort, Protocol):
    def search_conversations(self, contact_id: str) -> Sequence[ProviderRecord]: ...


class UnconnectedProvider:
    """Stand-in adapter for a provider whose credential is not configured.

    Every bounded operation fails before any network call, so an unconfigured
    provider degrades to `not connected` instead of taking the assistant down.
    """

    def __init__(self, provider: str):
        self.provider = provider

    def _deny(self) -> ProviderNotConnected:
        return ProviderNotConnected(f"{self.provider} is not connected")

    def get_card(self, card_id: str) -> ProviderRecord:
        raise self._deny()

    def list_cards(self) -> Sequence[ProviderRecord]:
        raise self._deny()

    def create_card(self, list_id: str, fields: Mapping[str, object]) -> ProviderRecord:
        raise self._deny()

    def update_card(self, card_id: str, fields: Mapping[str, object]) -> ProviderRecord:
        raise self._deny()

    def move_card(self, card_id: str, list_id: str) -> ProviderRecord:
        raise self._deny()

    def archive_card(self, card_id: str) -> ProviderRecord:
        raise self._deny()

    def get_contact(self, contact_id: str) -> ProviderRecord:
        raise self._deny()

    def search_conversations(self, contact_id: str) -> Sequence[ProviderRecord]:
        raise self._deny()

    def get_message(self, conversation_id: str, message_id: str, contact_id: str) -> ProviderRecord:
        raise self._deny()

    def send_sms(
        self, contact_id: str, normalized_destination: str, body: str
    ) -> Mapping[str, str]:
        raise self._deny()

    def fetch(self, endpoint: str, query: Mapping[str, object]) -> ProviderRecord:
        raise self._deny()

    def get_gmail_message(self, message_id: str) -> ProviderRecord:
        raise self._deny()

    def search_gmail(self, query: str, *, max_results: int = 50) -> Sequence[ProviderRecord]:
        raise self._deny()

    def create_gmail_draft(self, raw_base64url: str) -> ProviderRecord:
        raise self._deny()

    def get_calendar_event(self, calendar_id: str, event_id: str) -> ProviderRecord:
        raise self._deny()

    def list_calendar_events(
        self,
        calendar_id: str,
        *,
        query: str = "",
        time_min: str | None = None,
        time_max: str | None = None,
        max_results: int = 50,
    ) -> Sequence[ProviderRecord]:
        raise self._deny()

    def get_drive_file(self, file_id: str) -> ProviderRecord:
        raise self._deny()

    def search_drive(self, query: str, *, max_results: int = 50) -> Sequence[ProviderRecord]:
        raise self._deny()

    def get_document(self, document_id: str) -> ProviderRecord:
        raise self._deny()

    def get_spreadsheet(self, spreadsheet_id: str) -> ProviderRecord:
        raise self._deny()

    def get_google_contact(self, resource_name: str) -> ProviderRecord:
        raise self._deny()

    def list_contacts(self, *, page_size: int = 100) -> Sequence[ProviderRecord]:
        raise self._deny()

    def mutate(
        self, operation: str, resource_id: str, payload: Mapping[str, object]
    ) -> ProviderRecord:
        raise self._deny()

    def execute_routine(
        self, operation: str, resource_id: str, payload: Mapping[str, object]
    ) -> ProviderRecord:
        raise self._deny()


def _record_json(record: ProviderRecord) -> dict[str, object]:
    return {
        "provider": record.provider,
        "source_id": record.source_id,
        "retrieved_at": record.retrieved_at.isoformat(),
        "source_revision": record.source_revision,
        "fields": dict(record.fields),
        "missing_attributes": list(record.missing_attributes),
    }


def _proposal_json(proposal: Proposal) -> dict[str, object]:
    return {
        "proposal_id": proposal.proposal_id,
        "requester_role": proposal.requester.role.value,
        "approver_role": proposal.approver.role.value,
        "action_class": proposal.action_class,
        "target_ids": list(proposal.target_ids),
        "payload_hash": proposal.payload_hash,
        "source_revision": proposal.source_revision,
        "expires_at": proposal.expires_at.isoformat(),
        "version": proposal.version,
        "execution_nonce": proposal.execution_nonce,
        "status": proposal.status.value,
        "payload": dict(proposal.payload),
        "receipt": dict(proposal.receipt) if proposal.receipt else None,
    }


def _guidance_json(provider: str, connected: bool) -> dict[str, object]:
    item = provider_guidance(provider, connected=connected)
    return {
        "provider": item.provider,
        "name": item.display_name,
        "status": item.status,
        "summary": item.summary,
        "required_ids": list(item.required_ids),
        "required_scopes": list(item.required_scopes),
        "steps": list(item.steps),
        "guidance": item.as_text(),
    }


def _reminder_json(reminder: Reminder) -> dict[str, object]:
    return {
        "reminder_id": reminder.reminder_id,
        "due_at": reminder.due_at.isoformat(),
        "status": reminder.status.value,
        "version": reminder.version,
        "text": reminder.text,
        "receipt": dict(reminder.receipt) if reminder.receipt else None,
    }


@overload
def _text(args: Mapping[str, object], name: str, *, optional: Literal[False] = False) -> str: ...


@overload
def _text(args: Mapping[str, object], name: str, *, optional: Literal[True]) -> str | None: ...


def _text(args: Mapping[str, object], name: str, *, optional: bool = False) -> str | None:
    value = args.get(name)
    if optional and value is None:
        return None
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _integer(args: Mapping[str, object], name: str) -> int:
    value = args.get(name)
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _object(
    args: Mapping[str, object], name: str, *, optional: bool = False
) -> Mapping[str, object]:
    value = args.get(name)
    if optional and value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


class Runtime:
    def __init__(self, home: Path):
        self.home = home
        self.config = _load_private_config(home)
        transport = HttpTransport()
        # Client-visible tools may only ever reach configured client destinations.
        # The private full-profile route is deliberately absent from this allowlist.
        client_channels = self.config.client_discord_destinations()
        self.discord = DiscordAdapter(
            transport, _required_env("DISCORD_BOT_TOKEN"), client_channels
        )
        trello_key = os.environ.get("SCOTTY_TRELLO_API_KEY")
        trello_token = os.environ.get("SCOTTY_TRELLO_TOKEN")
        ghl_token = os.environ.get("SCOTTY_GHL_PRIVATE_TOKEN")
        rentcast_key = os.environ.get("SCOTTY_RENTCAST_API_KEY")
        # A provider is connected only when both its credential and its
        # configured resource scope are present. No placeholder ever counts.
        self.connected = {
            "discord": True,
            "trello": bool(trello_key and trello_token and self.config.trello is not None),
            "ghl": bool(ghl_token and self.config.ghl_location_id is not None),
            "rentcast": bool(rentcast_key and self.config.rentcast_endpoints),
            "google_workspace": False,
        }
        trello_scope = self.config.trello
        location_id = self.config.ghl_location_id
        endpoints = self.config.rentcast_endpoints
        self.trello: TrelloReadPort = (
            TrelloAdapter(transport, trello_key or "", trello_token or "", trello_scope)
            if self.connected["trello"] and trello_scope is not None
            else UnconnectedProvider("Trello")
        )
        self.ghl: GHLReadPort = (
            GHLAdapter(transport, ghl_token or "", location_id)
            if self.connected["ghl"] and location_id is not None
            else UnconnectedProvider("GoHighLevel")
        )
        self.rentcast: RentCastPort = (
            RentCastAdapter(transport, rentcast_key or "", endpoints)
            if self.connected["rentcast"] and endpoints
            else UnconnectedProvider("RentCast")
        )
        state_dir = home / "scotty"
        google_scope = self.config.google_workspace
        self.google_store = GoogleTokenStore(state_dir / "google-oauth.json")
        # Connected means consent exists for this exact account and scope set. An
        # hour-old access token refreshes in place; it never means "not connected".
        self.connected["google_workspace"] = bool(
            google_scope is not None
            and self.google_store.ready(google_scope.oauth_scopes, google_scope.account_email)
        )
        self.google_workspace = (
            GoogleWorkspaceAdapter(transport, self._google_access_token, google_scope)
            if google_scope is not None and self.connected["google_workspace"]
            else UnconnectedProvider("Google Workspace")
        )
        self.state_dir = state_dir
        self.approvals = ApprovalStore(state_dir / "approvals.db")
        self.approvals.initialize()
        self.approvals.recover_interrupted()
        self.reminders = ReminderStore(state_dir / "reminders.db")
        self.reminders.initialize()
        self.self_repair = SelfRepairManager(
            state_dir,
            state_dir / "private.json",
            self.approvals,
            self.reminders,
            provider_status=self.provider_connection_status,
        )
        self.resolver = AuthorizedPrincipalResolver(home, self.config)
        self.service = ScottyService(
            self.config,
            self.approvals,
            trello=self.trello,
            ghl=self.ghl,
            rentcast=self.rentcast,
            discord=self.discord,
            google_workspace=(
                self.google_workspace if self.connected["google_workspace"] else None
            ),
        )
        self.reminder_worker = ReminderWorker(self.reminders, self.discord.send_message)

    def _google_access_token(self) -> str:
        """Return a valid Workspace access token, refreshing it when it expires."""

        scope = self.config.google_workspace
        if scope is None:
            raise GoogleOAuthError("Google Workspace is not configured")
        return ensure_access_token(self.google_store, scope.oauth_scopes, scope.account_email)

    def principal(self, session_id: object) -> Principal:
        return self.resolver.resolve(session_id)

    def provider_connection_status(self) -> dict[str, bool]:
        return provider_status(self.connected)

    def _provider_setup(self, args: Mapping[str, object]) -> dict[str, object]:
        status = self.provider_connection_status()
        name = _text(args, "provider", optional=True)
        if name is None:
            return {
                "providers": {
                    provider: _guidance_json(provider, status[provider]) for provider in PROVIDERS
                }
            }
        if name not in PROVIDERS:
            raise ValueError("provider is not part of this deployment")
        return _guidance_json(name, status[name])

    def handle_read(self, principal: Principal, args: Mapping[str, object]) -> object:
        operation = _text(args, "operation")
        if operation == "self_health":
            return self.self_repair.health()
        if operation == "self_repair":
            # A refused repair returns its fixed redacted diagnosis so Scotty can
            # explain the next step instead of answering with a bare denial.
            try:
                return self.self_repair.repair(principal, _text(args, "repair_action"))
            except SelfRepairError as exc:
                return {"status": "refused", "reason": str(exc)}
        if operation == "status":
            return {
                "identity": "Scotty by The Closing Room",
                "addons": list(self.config.addons),
                "addon_slots_remaining": 6 - len(self.config.addons),
            }
        if operation == "provider_setup":
            return self._provider_setup(args)
        if operation == "google_workspace":
            google_operation = _text(args, "google_operation")
            payload = _object(args, "payload", optional=True)
            resource_id = _text(args, "resource_id", optional=True) or "new"
            if google_operation in {"search_gmail", "search_drive"}:
                query = payload.get("query", "")
                maximum = payload.get("max_results", 50)
                if type(query) is not str or type(maximum) is not int:
                    raise ValueError("Google search query or max_results is malformed")
                records = (
                    self.google_workspace.search_gmail(query, max_results=maximum)
                    if google_operation == "search_gmail"
                    else self.google_workspace.search_drive(query, max_results=maximum)
                )
                return [_record_json(item) for item in records]
            if google_operation == "list_calendar_events":
                calendar_id = payload.get("calendar_id", "primary")
                maximum = payload.get("max_results", 50)
                if type(calendar_id) is not str or type(maximum) is not int:
                    raise ValueError("Google Calendar list request is malformed")
                return [
                    _record_json(item)
                    for item in self.google_workspace.list_calendar_events(
                        calendar_id,
                        query=str(payload.get("query", "")),
                        time_min=(str(payload["time_min"]) if "time_min" in payload else None),
                        time_max=(str(payload["time_max"]) if "time_max" in payload else None),
                        max_results=maximum,
                    )
                ]
            if google_operation == "get_calendar_event":
                if "/" not in resource_id:
                    raise ValueError("calendar event resource must be calendar/event")
                calendar_id, event_id = resource_id.split("/", 1)
                return _record_json(self.google_workspace.get_calendar_event(calendar_id, event_id))
            if google_operation == "list_contacts":
                maximum = payload.get("max_results", 100)
                if type(maximum) is not int:
                    raise ValueError("Google Contacts max_results is malformed")
                return [
                    _record_json(item)
                    for item in self.google_workspace.list_contacts(page_size=maximum)
                ]
            getters = {
                "get_gmail_message": self.google_workspace.get_gmail_message,
                "get_drive_file": self.google_workspace.get_drive_file,
                "get_document": self.google_workspace.get_document,
                "get_spreadsheet": self.google_workspace.get_spreadsheet,
                "get_contact": self.google_workspace.get_contact,
            }
            getter = getters.get(google_operation)
            if getter is not None:
                return _record_json(getter(resource_id))
            return _record_json(
                self.google_workspace.execute_routine(google_operation, resource_id, payload)
            )
        if operation == "trello_card":
            return _record_json(self.trello.get_card(_text(args, "card_id")))
        if operation == "trello_cards":
            return [_record_json(item) for item in self.trello.list_cards()]
        if operation == "ghl_contact":
            return _record_json(self.ghl.get_contact(_text(args, "contact_id")))
        if operation == "ghl_conversations":
            return [
                _record_json(item)
                for item in self.ghl.search_conversations(_text(args, "contact_id"))
            ]
        if operation == "ghl_message":
            return _record_json(
                self.ghl.get_message(
                    _text(args, "conversation_id"),
                    _text(args, "message_id"),
                    _text(args, "contact_id"),
                )
            )
        if operation == "rentcast":
            endpoint = _text(args, "endpoint")
            return _record_json(self.rentcast.fetch(endpoint, _object(args, "query")))
        if operation == "google_gmail_message":
            return _record_json(self.google_workspace.get_gmail_message(_text(args, "message_id")))
        if operation == "google_gmail_draft":
            return _record_json(self.google_workspace.create_gmail_draft(_text(args, "raw")))
        if operation == "google_calendar_event":
            return _record_json(
                self.google_workspace.get_calendar_event(
                    _text(args, "calendar_id"), _text(args, "event_id")
                )
            )
        if operation == "google_drive_file":
            return _record_json(self.google_workspace.get_drive_file(_text(args, "file_id")))
        if operation == "google_document":
            return _record_json(self.google_workspace.get_document(_text(args, "document_id")))
        if operation == "google_spreadsheet":
            return _record_json(
                self.google_workspace.get_spreadsheet(_text(args, "spreadsheet_id"))
            )
        if operation == "google_contact":
            return _record_json(self.google_workspace.get_contact(_text(args, "resource_name")))
        raise ValueError("read operation is not permitted")

    def handle_propose(self, principal: Principal, args: Mapping[str, object]) -> object:
        operation = _text(args, "operation")
        if operation == "trello_merge":
            proposal = self.service.propose_trello_merge(
                principal, _text(args, "source_card_id"), _text(args, "destination_card_id")
            )
        elif operation == "trello_create":
            proposal = self.service.propose_trello_create(
                principal,
                _text(args, "list_id"),
                _object(args, "fields"),
            )
        elif operation in {"trello_update", "trello_move", "trello_archive"}:
            proposal = self.service.propose_trello_action(
                principal,
                operation.removeprefix("trello_"),
                _text(args, "card_id"),
                _object(args, "fields", optional=True),
                _text(args, "destination_list_id", optional=True),
            )
        elif operation == "ghl_sms":
            proposal = self.service.propose_ghl_sms(
                principal,
                _text(args, "contact_id"),
                _text(args, "normalized_destination"),
                _text(args, "body"),
            )
        elif operation == "discord_announcement":
            proposal = self.service.propose_discord_announcement(
                principal, _text(args, "channel_id"), _text(args, "content")
            )
        elif operation == "google_workspace_write":
            proposal = self.service.propose_google_workspace_write(
                principal,
                _text(args, "google_operation"),
                _text(args, "resource_id"),
                _object(args, "payload"),
            )
        else:
            raise ValueError("proposal operation is not permitted")
        return _proposal_json(proposal)

    def handle_approval(self, principal: Principal, args: Mapping[str, object]) -> object:
        action = _text(args, "action")
        proposal_id = _text(args, "proposal_id")
        version = _integer(args, "expected_version")
        if action == "approve":
            result = self.service.approve(principal, proposal_id, version)
        elif action == "deny":
            result = self.service.deny(principal, proposal_id, version)
        elif action == "execute":
            result = self.service.execute(
                principal,
                proposal_id,
                expected_version=version,
                execution_nonce=_text(args, "execution_nonce"),
            )
        else:
            raise ValueError("approval action is not permitted")
        return _proposal_json(result)

    def handle_reminder(self, principal: Principal, args: Mapping[str, object]) -> object:
        action = _text(args, "action")
        if action == "create":
            due_text = _text(args, "due_at")
            try:
                due_at = datetime.fromisoformat(due_text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("due_at must be an ISO 8601 timestamp with timezone") from exc
            if due_at.tzinfo is None:
                raise ValueError("due_at must include a timezone")
            return _reminder_json(self.reminders.create(principal, _text(args, "text"), due_at))
        if action == "list":
            return [_reminder_json(item) for item in self.reminders.list_for(principal)]
        if action == "cancel":
            return _reminder_json(self.reminders.cancel(principal, _text(args, "reminder_id")))
        raise ValueError("reminder action is not permitted")

    def handle_calculate(self, principal: Principal, args: Mapping[str, object]) -> object:
        del principal
        try:
            asking = Decimal(_text(args, "asking_price"))
            value = Decimal(_text(args, "estimated_value"))
            rent = Decimal(_text(args, "estimated_monthly_rent"))
        except InvalidOperation as exc:
            raise ValueError("calculation values must be decimal strings") from exc
        return self.service.analyze_property(asking, value, rent)


class Controller:
    """Owns lazy runtime state, fixed outbound queue, and reminder polling."""

    def __init__(self) -> None:
        self.home = _home_path()
        self._runtime: Runtime | None = None
        self._intake: CredentialIntake | None = None
        self._lock = threading.RLock()
        self._outbound: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=100)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._serve,
            name="scotty-bounded-reminders",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def runtime(self) -> Runtime:
        with self._lock:
            if self._runtime is None:
                self._runtime = Runtime(self.home)
            return self._runtime

    def intake(self, runtime: Runtime) -> CredentialIntake:
        """One protected intake per runtime, so a window survives between events."""

        with self._lock:
            if self._intake is None:
                self._intake = CredentialIntake(
                    runtime.config,
                    self.enqueue,
                    broker=UnixSocketBroker(BROKER_SOCKET),
                    deleter=runtime.discord,
                    clock=lambda: int(time.time()),
                )
            return self._intake

    def enqueue(self, channel_id: str, text: str) -> bool:
        try:
            self._outbound.put_nowait((channel_id, text))
            return True
        except queue.Full:
            return False

    def ingress(self, event: object, **kwargs: object) -> Mapping[str, str]:
        del kwargs
        try:
            runtime = self.runtime()
        except Exception:
            return {"action": "skip", "reason": "unavailable"}
        return IngressGuard(
            runtime.config, self.enqueue, runtime.state_dir, intake=self.intake(runtime)
        )(event)

    def tool(self, kind: str, args: object, **kwargs: object) -> str:
        try:
            if not isinstance(args, dict):
                raise ValueError("tool arguments must be an object")
            runtime = self.runtime()
            principal = runtime.principal(kwargs.get("session_id"))
            if kind == "read":
                result = runtime.handle_read(principal, args)
            elif kind == "propose":
                result = runtime.handle_propose(principal, args)
            elif kind == "approval":
                result = runtime.handle_approval(principal, args)
            elif kind == "reminder":
                result = runtime.handle_reminder(principal, args)
            elif kind == "calculate":
                result = runtime.handle_calculate(principal, args)
            else:
                raise ValueError("tool is not permitted")
            return json.dumps({"ok": True, "result": result}, sort_keys=True, separators=(",", ":"))
        except Exception:
            return json.dumps(
                {"ok": False, "error": "Request denied or unavailable."},
                sort_keys=True,
                separators=(",", ":"),
            )

    def _serve(self) -> None:
        while not self._stop.wait(1.0):
            try:
                runtime = self.runtime()
            except Exception:  # noqa: S112 - setup may not have published state yet
                # Setup may not have published private state yet.
                continue
            for _ in range(20):
                try:
                    channel_id, text = self._outbound.get_nowait()
                except queue.Empty:
                    break
                try:
                    runtime.discord.send_message(channel_id, text)
                except Exception as exc:
                    logger.warning("Fixed outbound delivery failed: %s", type(exc).__name__)
            try:
                runtime.reminder_worker.run_once()
            except Exception as exc:
                logger.warning("Reminder poll failed: %s", type(exc).__name__)
