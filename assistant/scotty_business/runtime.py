from __future__ import annotations

import json
import logging
import os
import queue
import threading
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, overload

from .adapters import (
    DiscordAdapter,
    GHLAdapter,
    HttpTransport,
    ProviderRecord,
    RentCastAdapter,
    TrelloAdapter,
)
from .approvals import ApprovalStore, Proposal
from .config import ConfigError, RuntimeConfig
from .identity import AuthorizedPrincipalResolver
from .ingress import IngressGuard
from .policy import Principal
from .reminders import Reminder, ReminderStore, ReminderWorker
from .service import ScottyService

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
        all_channels = tuple(
            dict.fromkeys(
                [
                    *(principal.channel_id for principal in self.config.principals),
                    *self.config.announcement_channel_ids,
                ]
            )
        )
        self.discord = DiscordAdapter(transport, _required_env("DISCORD_BOT_TOKEN"), all_channels)
        self.trello = TrelloAdapter(
            transport,
            _required_env("SCOTTY_TRELLO_API_KEY"),
            _required_env("SCOTTY_TRELLO_TOKEN"),
            self.config.trello,
        )
        self.ghl = GHLAdapter(
            transport,
            _required_env("SCOTTY_GHL_PRIVATE_TOKEN"),
            self.config.ghl_location_id,
        )
        self.rentcast = RentCastAdapter(
            transport,
            _required_env("SCOTTY_RENTCAST_API_KEY"),
            self.config.rentcast_endpoints,
        )
        state_dir = home / "scotty"
        self.approvals = ApprovalStore(state_dir / "approvals.db")
        self.approvals.initialize()
        self.approvals.recover_interrupted()
        self.reminders = ReminderStore(state_dir / "reminders.db")
        self.reminders.initialize()
        self.resolver = AuthorizedPrincipalResolver(home, self.config)
        self.service = ScottyService(
            self.config,
            self.approvals,
            trello=self.trello,
            ghl=self.ghl,
            rentcast=self.rentcast,
            discord=self.discord,
        )
        self.reminder_worker = ReminderWorker(self.reminders, self.discord.send_message)

    def principal(self, session_id: object) -> Principal:
        return self.resolver.resolve(session_id)

    def handle_read(self, principal: Principal, args: Mapping[str, object]) -> object:
        del principal
        operation = _text(args, "operation")
        if operation == "status":
            return {
                "identity": "Scotty by The Closing Room",
                "addons": list(self.config.addons),
                "addon_slots_remaining": 6 - len(self.config.addons),
            }
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
        return IngressGuard(runtime.config, self.enqueue)(event)

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
