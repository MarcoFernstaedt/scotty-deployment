from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Protocol, overload

from .adapters import (
    MAX_ATTACHMENT_BYTES,
    AmbiguousEffectError,
    Attachment,
    DiscordAdapter,
    GHLAdapter,
    GoogleWorkspaceAdapter,
    HttpTransport,
    ProviderRecord,
    RentCastAdapter,
    TrelloAdapter,
)
from .adapters.discord_admin import DiscordAdminAdapter
from .approvals import ApprovalError, ApprovalStore, Proposal, ProposalStatus
from .backup import backup_state, restorable, rollback_guidance, verify_backup
from .brokered_transport import BrokeredTransport
from .budgets import BudgetLedger, BudgetPolicy
from .config import CLIENT_ROLES, ConfigError, RuntimeConfig
from .credential_intake import BROKER_SOCKET, CredentialIntake, UnixSocketBroker
from .discord_policy import (
    BULK_WINDOW_SECONDS,
    DiscordActionClass,
    classify_discord_action,
    permitted_destinations,
    protected_channels,
    redacted_refusal,
    shared_destinations,
)
from .google_oauth import (
    GoogleOAuthError,
    GoogleTokenStore,
    ensure_access_token,
    google_prompt_path,
    google_token_path,
    read_consent_prompt,
)
from .guidance import PROVIDERS, provider_guidance, provider_status
from .identity import AuthorizedPrincipalResolver
from .ingress import IngressGuard
from .persona import (
    DEFAULT_ASSISTANT_NAME,
    PersonaError,
    PersonaStore,
    resolve_persona,
)
from .policy import Principal, Role
from .progress import ProgressReporter
from .property_cards import (
    compare,
    find_duplicates,
    merge_preview,
    normalize_address,
    parse_card,
)
from .property_engine import EffectLog, PropertyCardEngine
from .provider_identity import (
    ProviderIdentity,
    ProviderIdentityResolver,
    reject_identity_override,
)
from .reminders import Reminder, ReminderStore, ReminderWorker
from .self_repair import SelfRepairError, SelfRepairManager
from .service import GHLPort, GoogleWorkspacePort, RentCastPort, ScottyService, TrelloPort
from .setup_flow import (
    LOCAL_SETUP_COMMAND,
    ProviderProgress,
    SetupFlowError,
    SetupStagingStore,
    diagnose,
    first_unfinished,
    setup_progress,
)
from .supervisor import ConsumerLease, HealthState, IncidentLog, Supervisor
from .workflow_runs import (
    Run,
    RunError,
    RunLedger,
    Runner,
    StepOutcome,
    StepState,
    due_trigger,
)
from .workflows import (
    Workflow,
    WorkflowError,
    WorkflowState,
    WorkflowStore,
    parse_workflow,
)

logger = logging.getLogger(__name__)

#: How much of the run ledger one supervision pass walks, and in what pages.
#: Bounded so a very large ledger cannot make one pass take forever, and paged
#: so a page of blocked runs cannot hide the ones behind it.
OPEN_RUN_PAGE = 50
MAX_RUNS_PER_PASS = 500

#: Exactly the property-card operations this tool serves. An operation absent
#: from here is refused for being absent, whatever else is or is not connected.
_CARD_OPERATIONS = frozenset(
    {
        "normalize_address",
        "compare",
        "preview_merge",
        "duplicates",
        "reformat",
        "apply_template",
        "dry_run",
        "create",
        "update",
        "move",
    }
)

#: The run controls a workflow's owner has. Everything else about a workflow is
#: a declaration; these are the ones that make something happen.
_WORKFLOW_RUN_ACTIONS = frozenset(
    {"run", "runs", "run_status", "pause_run", "resume_run", "cancel_run"}
)

#: The provider credentials the root-owned broker can be asked about. Google is
#: absent: it uses provider-owned browser consent, not a stored key.
BROKER_CREDENTIALS: Mapping[str, str] = {
    # The credential that carries an identity, not the one that carries the
    # application's. Trello's api_key says which product is calling; the token
    # says who. "Is Trello connected for you" is a question about the token.
    "trello": "token",
    "ghl": "private_token",
    "rentcast": "api_key",
}

#: The content types Scotty may attach, keyed by the approved suffix.
_ATTACHMENT_TYPES: Mapping[str, str] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
}


#: Operations that put a new message in a channel, and so count toward volume.
_SENDING_OPERATIONS = frozenset(
    {"send_message", "reply_message", "attach_file", "send_thread_message"}
)


def _required(value: str | None, field: str) -> str:
    if not value:
        raise ValueError(f"{field} is required")
    return value


#: Only these roles may change the configured Workspace account's own data.
_WORKSPACE_WRITE_ROLES: frozenset[Role] = frozenset({Role.MAINTAINER, Role.MAIN_OPERATOR})


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


class GoogleWorkspaceReadPort(GoogleWorkspacePort, Protocol):
    """Everything the client read tool may ask one user's Workspace to do."""

    def search_gmail(self, query: str, *, max_results: int = 50) -> Sequence[ProviderRecord]: ...
    def search_drive(self, query: str, *, max_results: int = 50) -> Sequence[ProviderRecord]: ...
    def list_calendar_events(
        self,
        calendar_id: str,
        *,
        query: str = "",
        time_min: str | None = None,
        time_max: str | None = None,
        max_results: int = 50,
    ) -> Sequence[ProviderRecord]: ...
    def list_contacts(self, *, page_size: int = 50) -> Sequence[ProviderRecord]: ...
    def read_drive_file(self, file_id: str) -> ProviderRecord: ...
    def get_sheet_values(self, spreadsheet_id: str, a1_range: str) -> ProviderRecord: ...
    def batch_get_sheet_values(
        self, spreadsheet_id: str, ranges: Sequence[str]
    ) -> ProviderRecord: ...
    def get_gmail_message(self, message_id: str) -> ProviderRecord: ...
    def create_gmail_draft(self, raw_base64url: str) -> ProviderRecord: ...
    def get_calendar_event(self, calendar_id: str, event_id: str) -> ProviderRecord: ...
    def get_drive_file(self, file_id: str) -> ProviderRecord: ...
    def get_document(self, document_id: str) -> ProviderRecord: ...
    def get_spreadsheet(self, spreadsheet_id: str) -> ProviderRecord: ...
    def get_contact(self, resource_name: str) -> ProviderRecord: ...
    def execute_routine(
        self, operation: str, resource_id: str, payload: Mapping[str, object]
    ) -> ProviderRecord: ...


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

    def read_drive_file(self, file_id: str) -> ProviderRecord:
        raise self._deny()

    def get_sheet_values(self, spreadsheet_id: str, range_: str) -> ProviderRecord:
        raise self._deny()

    def batch_get_sheet_values(self, spreadsheet_id: str, ranges: Sequence[str]) -> ProviderRecord:
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
        "apis": list(item.apis),
        "callback": item.callback,
        "guidance": item.as_text(),
    }


def _progress_json(item: ProviderProgress) -> dict[str, object]:
    return {
        "provider": item.provider,
        "name": item.display_name,
        "status": item.status,
        "identifiers_complete": item.configured,
        "missing_identifiers": list(item.missing),
        "next_action": item.next_action,
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


#: The exact shape a backup directory is created with, so a name argument can
#: only ever select one of this deployment's own backups.
_BACKUP_NAME = re.compile(r"[0-9]{8}T[0-9]{6}")


#: The adapters require a non-empty credential argument to construct. Nothing
#: in this container has one, and nothing needs one: the broker replaces it
#: with the material it holds. This placeholder is never sent anywhere — the
#: brokered transport drops the credential query names before it forwards.
_BROKERED = "brokered"


class Runtime:
    def __init__(self, home: Path, *, broker_socket: Path | None = None):
        self.home = home
        # The socket is fixed in the deployment; the parameter exists so a test
        # can stand up a real broker of its own rather than mock the boundary.
        self.broker_socket = broker_socket or Path(BROKER_SOCKET)
        self.config = _load_private_config(home)
        transport = HttpTransport()
        # Client-visible tools may only ever reach configured client destinations.
        # The private full-profile route is deliberately absent from this allowlist.
        client_channels = self.config.client_discord_destinations()
        bot_token = _required_env("DISCORD_BOT_TOKEN")
        self.discord = DiscordAdapter(transport, bot_token, client_channels)
        # Guild administration is a separate adapter bound to the one configured
        # guild. It is reachable only through an approved proposal.
        self.discord_admin = DiscordAdminAdapter(
            transport, bot_token, self.config.principals[0].guild_id
        )
        # Provider credentials are resolved per actor: a suffixed variable is
        # that user's own, the unsuffixed one is the deployment's single shared
        # business identity, and neither user can reach the other's.
        # Provider calls leave through the broker, which holds the credential
        # outside this container. Nothing here has one to give an adapter.
        self.provider_broker = UnixSocketBroker(self.broker_socket)
        self.identities = ProviderIdentityResolver(self.config, self.provider_broker)
        self.trello_scope = self.config.trello
        self.ghl_location_id = self.config.ghl_location_id
        self.rentcast_endpoints = self.config.rentcast_endpoints
        # Adapters are no longer built once and shared. They are built for the
        # principal making the call, carrying that principal's own citation, so
        # an employee's effect can never leave through the main operator's
        # connector -- which is exactly what a single shared adapter allowed.
        self._adapter_cache: dict[tuple[str, str], object] = {}
        self.actor_connected: dict[Role, dict[str, bool]] = {
            role: {"trello": False, "ghl": False, "rentcast": False} for role in CLIENT_ROLES
        }
        # Deployment-level readiness: whether the broker holds anything at all
        # for this provider. Whether a given person may use it is a different
        # question, answered per call against that person's own citation.
        broker_available = self.provider_broker.available()
        self.connected = {
            "discord": True,
            "trello": broker_available
            and self.trello_scope is not None
            and self.provider_broker.status("trello", "api_key"),
            "ghl": broker_available
            and self.ghl_location_id is not None
            and self.provider_broker.status("ghl", "private_token"),
            "rentcast": broker_available
            and bool(self.rentcast_endpoints)
            and self.provider_broker.status("rentcast", "api_key"),
            "google_workspace": False,
        }
        state_dir = home / "scotty"
        # Consent is personal, so each client user has their own token record
        # and their own adapter. Nothing here is shared between the two.
        self.google_stores: dict[Role, GoogleTokenStore] = {
            role: GoogleTokenStore(google_token_path(role, state_dir)) for role in CLIENT_ROLES
        }
        self.google_connected: dict[Role, bool] = {}
        self.google_adapters: dict[Role, GoogleWorkspaceAdapter] = {}
        for role in CLIENT_ROLES:
            scope = self.config.google_for(role)
            # Connected means consent exists for this exact account and scope
            # set. An hour-old access token refreshes in place; it never means
            # "not connected".
            linked = bool(
                scope is not None
                and self.google_stores[role].ready(scope.oauth_scopes, scope.account_email)
            )
            self.google_connected[role] = linked
            if linked and scope is not None:
                self.google_adapters[role] = GoogleWorkspaceAdapter(
                    transport, self._google_token_provider(role), scope
                )
        self.connected["google_workspace"] = any(self.google_connected.values())
        self.state_dir = state_dir
        self.approvals = ApprovalStore(state_dir / "approvals.db")
        self.approvals.initialize()
        self.approvals.recover_interrupted()
        self.reminders = ReminderStore(state_dir / "reminders.db")
        self.reminders.initialize()
        self.setup_staging = SetupStagingStore(state_dir / "setup-staging.json")
        self.personas = PersonaStore(state_dir / "personas.json")
        self.workflows = WorkflowStore(state_dir / "workflows.json")
        self.workflow_runs = RunLedger(state_dir / "workflow-runs.db")
        self.workflow_runs.initialize()
        # A step recorded as running is one nobody watched finish. Coming back
        # from a restart, those become unknown and stop their run rather than
        # being repeated into a second card or a second message.
        self.workflow_runs.recover_interrupted()
        self.workflow_runner = Runner(self.workflow_runs, self._run_workflow_step)
        self.budgets = BudgetLedger(state_dir / "budgets.db", BudgetPolicy.from_mapping({}))
        self.budgets.initialize()
        self.supervisor = Supervisor(state_dir)
        self.incidents = IncidentLog(state_dir / "incidents.json")
        self.consumer_lease = ConsumerLease(state_dir / "consumer.lease")
        self.property_effects = EffectLog(state_dir / "property-effects.db")
        self.property_effects.initialize()

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
            # The service resolves each provider from the principal executing
            # the proposal, so an approved effect leaves through that person's
            # own connector rather than the deployment's first one.
            trello=self._trello,
            ghl=self._ghl,
            rentcast=self._rentcast,
            discord=self.discord,
            discord_admin=self.discord_admin,
            google_workspace=self.google_workspace_for,
        )
        self.reminder_worker = ReminderWorker(self.reminders, self.discord.send_message)
        self._reporters: dict[tuple[str, str], ProgressReporter] = {}
        self._sends: dict[tuple[str, str], list[float]] = {}

    def credential_store_status(self) -> dict[str, str]:
        """What the root-owned broker holds, as fixed words and nothing more."""

        broker = UnixSocketBroker(BROKER_SOCKET)
        if not broker.available():
            return dict.fromkeys(BROKER_CREDENTIALS, "unavailable")
        status: dict[str, str] = {}
        for provider, credential_class in BROKER_CREDENTIALS.items():
            status[provider] = "present" if broker.status(provider, credential_class) else "absent"
        return status

    def _google_token_provider(self, role: Role) -> Callable[[], str]:
        """A token provider bound to exactly one client user's own account."""

        def provide() -> str:
            scope = self.config.google_for(role)
            if scope is None:
                raise GoogleOAuthError("Google Workspace is not configured")
            return ensure_access_token(
                self.google_stores[role], scope.oauth_scopes, scope.account_email
            )

        return provide

    def google_workspace_for(self, role: Role) -> GoogleWorkspaceAdapter | None:
        """The Workspace adapter for exactly this client user, or none."""

        return self.google_adapters.get(role)

    def _workspace(self, principal: Principal) -> GoogleWorkspaceReadPort:
        """This actor's own Workspace, or a provider that explains it is absent.

        An actor who has not connected their own account never falls through to
        the other user's adapter; they are told to connect their own.
        """

        adapter = self.google_adapters.get(principal.role)
        if adapter is None:
            return UnconnectedProvider("Google Workspace")
        return adapter

    def principal(self, session_id: object) -> Principal:
        return self.resolver.resolve(session_id)

    def provider_connection_status(self) -> dict[str, bool]:
        return provider_status(self.connected)

    def assistant_name(self, role: Role) -> str:
        """What this user's assistant is called right now."""

        try:
            return resolve_persona(self.config, role, self.personas.read()).assistant_name
        except PersonaError:
            # The maintainer route has no client persona; it is served by its
            # own profile and never borrows a client's assistant name.
            return DEFAULT_ASSISTANT_NAME

    def _persona(self, principal: Principal, args: Mapping[str, object]) -> dict[str, object]:
        """Show or change this caller's own assistant name, and only theirs."""

        action = _text(args, "action", optional=True) or "show"
        if action == "show":
            return resolve_persona(self.config, principal.role, self.personas.read()).as_json()
        if action != "set":
            raise ValueError("persona action is not permitted")
        try:
            # The role comes from the authorized origin, so a caller can only
            # ever rename their own assistant.
            chosen = self.personas.set(principal.role, args.get("name"))
        except PersonaError as exc:
            return {"accepted": False, "correction": str(exc)}
        return {"accepted": True, "assistant_name": chosen, "role": principal.role.value}

    def _property_card(self, principal: Principal, args: Mapping[str, object]) -> object:
        """One typed property-card operation, bound to the calling actor.

        Reading, comparing and previewing never touch the provider. Creating
        and changing do, and each is read back before it counts as done.
        """

        operation = _text(args, "card_operation")
        if operation not in _CARD_OPERATIONS:
            # Refused for being unknown, before anything is asked about
            # connectivity: "Trello is not connected" is a confusing answer to
            # an operation that would not exist if it were.
            raise ValueError("property-card operation is not permitted")
        if operation == "normalize_address":
            return normalize_address(_text(args, "address")).as_json()
        if operation in {"compare", "preview_merge"}:
            # Two cards somebody pasted, compared arithmetically. No provider
            # is touched, so none needs to be connected.
            left = parse_card(_object(args, "card"))
            right = parse_card(_object(args, "other_card"))
            if operation == "compare":
                return compare(left, right).as_json()
            return merge_preview(left, right).as_json()
        engine = self._property_engine(principal)
        if engine is None:
            raise ProviderNotConnected("Trello is not connected")
        if operation == "duplicates":
            candidate = parse_card(_object(args, "card"))
            return [
                {"card_id": match.card_id, **result.as_json()}
                for match, result in find_duplicates(candidate, engine.existing())
            ]
        if operation == "reformat":
            return engine.reformat(parse_card(_object(args, "card"))).as_json()
        if operation == "apply_template":
            template = {str(name): str(value) for name, value in _object(args, "template").items()}
            return engine.apply_template(parse_card(_object(args, "card")), template).as_json()
        if operation == "dry_run":
            identifiers = args.get("card_ids")
            if not isinstance(identifiers, list) or not all(
                type(item) is str for item in identifiers
            ):
                raise ValueError("a bulk review needs a list of card identifiers")
            return engine.dry_run(
                principal,
                _text(args, "card_operation_target", optional=True) or "move",
                identifiers,
                _object(args, "payload", optional=True),
            ).as_json()
        if operation == "create":
            return engine.create(principal, parse_card(_object(args, "card"))).as_json()
        if operation == "update":
            card = parse_card(_object(args, "card"))
            return engine.update(principal, _text(args, "card_id"), card).as_json()
        if operation == "move":
            return engine.routine(
                principal, "move", _text(args, "card_id"), _object(args, "payload")
            ).as_json()
        raise ValueError("property-card operation is not permitted")

    #: How a workflow step reaches the operation it names. Every one of these
    #: goes back through the handler that serves a person asking directly, so a
    #: workflow gets exactly the authority its owner already has and not a step
    #: more. A step is a way to ask, never a way to be allowed.
    _WORKFLOW_READS: Mapping[str, Mapping[str, object]] = {
        "property_card.create": {"operation": "property_card", "card_operation": "create"},
        "property_card.update": {"operation": "property_card", "card_operation": "update"},
        "property_card.move": {"operation": "property_card", "card_operation": "move"},
        "property_card.reformat": {"operation": "property_card", "card_operation": "reformat"},
        "property_card.apply_template": {
            "operation": "property_card",
            "card_operation": "apply_template",
        },
        "property_card.duplicates": {"operation": "property_card", "card_operation": "duplicates"},
        "trello.list_cards": {"operation": "trello_cards"},
        "ghl.read_contact": {"operation": "ghl_contact"},
        "rentcast.lookup": {"operation": "rentcast"},
    }

    #: Steps whose effect is not freely reversible never happen inside a run.
    #: The run raises the same proposal a person would and stops there; someone
    #: with the authority approves and executes it through the ordinary path.
    _WORKFLOW_PROPOSALS: Mapping[str, str] = {
        "property_card.archive": "trello_archive",
        "discord.announce": "discord_announcement",
        "google.send_draft": "google_workspace_write",
        "ghl.send_sms": "ghl_sms",
    }

    def _workflow_request(
        self, operation: str, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        """The exact request one workflow step makes, in the runtime's own shape."""

        fixed = self._WORKFLOW_READS.get(operation)
        if fixed is not None:
            return {**dict(arguments), **fixed}
        if operation == "discord.post_update":
            return {
                "operation": "discord",
                "discord_operation": "update_progress",
                "payload": dict(arguments),
            }
        google = {
            "google.create_draft": "gmail_create_draft",
            "google.create_event": "calendar_create_event",
        }.get(operation)
        if google is not None:
            return {
                "operation": "google_workspace",
                "google_operation": google,
                "resource_id": str(arguments.get("resource_id", "new")),
                "payload": {
                    name: value for name, value in arguments.items() if name != "resource_id"
                },
            }
        raise RunError(f"{operation} is not a step this deployment can carry out")

    def _workflow_proposal(
        self, operation: str, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        proposal = self._WORKFLOW_PROPOSALS[operation]
        if proposal == "google_workspace_write":
            return {
                "operation": proposal,
                "google_operation": "gmail_send_draft",
                "resource_id": str(arguments.get("resource_id", "")),
                "payload": {
                    name: value for name, value in arguments.items() if name != "resource_id"
                },
            }
        return {**dict(arguments), "operation": proposal}

    def _run_workflow_step(
        self, principal: Principal, operation: str, arguments: Mapping[str, object]
    ) -> StepOutcome:
        """Carry out one step, and say honestly what became of it.

        The three answers are the ones the ledger records: it happened, it did
        not, or nobody can tell. An ambiguous provider outcome is never reported
        as a failure, because a failed step is retried and an ambiguous one must
        not be.
        """

        try:
            if operation in self._WORKFLOW_PROPOSALS:
                raised = self.handle_propose(
                    principal, self._workflow_proposal(operation, arguments)
                )
                proposal_id = raised.get("proposal_id", "") if isinstance(raised, Mapping) else ""
                return StepOutcome(
                    StepState.AWAITING_APPROVAL,
                    "this step needs an approval before it can happen",
                    str(proposal_id),
                )
            if operation in {"reminder.create", "reminder.cancel"}:
                request = {**dict(arguments), "action": operation.removeprefix("reminder.")}
                self.handle_reminder(principal, request)
                return StepOutcome(StepState.DONE, "done")
            result = self.handle_read(principal, self._workflow_request(operation, arguments))
        except AmbiguousEffectError as exc:
            return StepOutcome(StepState.UNKNOWN, str(exc))
        except (PermissionError, ProviderNotConnected, ApprovalError, RunError, ValueError) as exc:
            # A refusal, a provider that is not connected, or a malformed step:
            # each is a plain failure, and the run's own retry rule decides
            # whether to try again or stop.
            return StepOutcome(StepState.FAILED, str(exc))
        if isinstance(result, Mapping) and result.get("status") == "unknown":
            # The runtime already decided this one cannot be told either way.
            return StepOutcome(StepState.UNKNOWN, str(result.get("reason", "")))
        return StepOutcome(StepState.DONE, "done")

    def _workflow(self, principal: Principal, args: Mapping[str, object]) -> object:
        """Build, review, and run this user's own workflows. Only their own."""

        action = _text(args, "workflow_action", optional=True) or "list"
        owner = principal.role
        if action == "list":
            return [item.preview() for item in self.workflows.list(owner)]
        if action == "save":
            workflow = parse_workflow(_object(args, "definition"), owner=owner)
            saved = self.workflows.save(workflow)
            return {"workflow_id": saved.workflow_id, **saved.preview()}
        if action in _WORKFLOW_RUN_ACTIONS:
            # A run is addressed by its own identifier once it exists, so only
            # starting one needs to name the workflow it comes from.
            named = (
                _text(args, "workflow_id")
                if action == "run"
                else _text(args, "workflow_id", optional=True) or ""
            )
            return self._workflow_run(principal, action, named, args)
        workflow_id = _text(args, "workflow_id")
        if action in {"get", "preview"}:
            return self.workflows.get(workflow_id, owner).preview()
        if action == "revise":
            revision = parse_workflow(_object(args, "definition"), owner=owner)
            revised = self.workflows.revise(workflow_id, owner, revision)
            return {"workflow_id": revised.workflow_id, **revised.preview()}
        states = {
            "activate": WorkflowState.ACTIVE,
            "pause": WorkflowState.PAUSED,
            "retire": WorkflowState.RETIRED,
        }
        if action not in states:
            raise ValueError("workflow action is not permitted")
        moved = self.workflows.transition(workflow_id, owner, states[action])
        return {"workflow_id": moved.workflow_id, **moved.preview()}

    def _workflow_run(
        self,
        principal: Principal,
        action: str,
        workflow_id: str,
        args: Mapping[str, object],
    ) -> object:
        """Start, continue, or control one run of this user's own workflow.

        Running is one pass rather than a loop that owns the process: it goes as
        far as it honestly can, writes down where it stopped, and returns that.
        A run waiting on an approval, paused, or holding an effect nobody can
        see is a state somebody reads, not a thread somebody waits on.
        """

        owner = principal.role
        try:
            if action == "runs":
                return [item.preview() for item in self.workflow_runs.list(owner)]
            if action == "resume_run":
                resumed = self.workflow_runs.resume(_text(args, "run_id"), owner)
                # The workflow is read from the run rather than from the
                # caller: resuming must continue the work that was started,
                # not a different workflow named now.
                workflow = self.workflows.get(resumed.workflow_id, owner)
                return self.workflow_runner.advance(resumed.run_id, workflow, principal).preview()
            if action == "run":
                workflow = self.workflows.get(workflow_id, owner)
                if workflow.state is not WorkflowState.ACTIVE:
                    raise ValueError("only an active workflow runs")
                run = self.workflow_runs.start(
                    workflow, principal, _object(args, "trigger", optional=True)
                )
                return self.workflow_runner.advance(run.run_id, workflow, principal).preview()
            run_id = _text(args, "run_id")
            if action == "run_status":
                return self.workflow_runs.get(run_id, owner).preview()
            if action == "pause_run":
                return self.workflow_runs.pause(run_id, owner).preview()
            return self.workflow_runs.cancel(
                run_id, owner, _text(args, "reason", optional=True) or ""
            ).preview()
        except RunError as exc:
            # A run refusing is ordinary: the trigger repeated, the deadline
            # passed, the run is somebody else's. It is explained, not raised
            # as an internal failure.
            raise ValueError(str(exc)) from exc

    def actor_connection_status(self, principal: Principal) -> dict[str, bool]:
        """What this exact user is connected to, not what the deployment has.

        Asked of the broker, for this person, at the moment of asking. The
        broker answers for whoever Discord says wrote the message this work is
        for -- so "connected" here means "connected for you", and a shared
        business identity nobody granted this person reads as not authorized
        rather than as connected.
        """

        connected = dict(self.connected)
        for provider, credential_class in BROKER_CREDENTIALS.items():
            if provider not in connected:
                continue
            connected[provider] = connected[provider] and self._held_for(
                principal, provider, credential_class
            )
        connected["google_workspace"] = self.google_connected.get(principal.role, False)
        return provider_status(connected)

    def _held_for(self, principal: Principal, provider: str, credential_class: str) -> bool:
        """Whether the broker will act for this person on that provider."""

        citation = principal.citation()
        if citation is None:
            # Nothing to cite means nobody has asked for anything, so there is
            # no person to be connected on behalf of.
            return False
        reply = self.provider_broker.status_for(provider, credential_class, citation)
        return reply

    def _maintenance(self, principal: Principal, args: Mapping[str, object]) -> object:
        """Backup, restore-preview, rollback-plan and health. Maintainer only.

        These reach the deployment's own state rather than any one user's work,
        so they are not client operations. Rollback is not among them: releases
        are root-owned on the host and the host supervisor selects them, so what
        comes back from here is the operator's command, never an executed one.
        """

        if principal.role is not Role.MAINTAINER:
            raise PermissionError("that operation is not available on this route")
        action = _text(args, "maintenance_action")
        backups = self.state_dir / "backups"

        def named_backup() -> Path:
            """One backup this deployment took, by its own name and no other.

            The name is an argument, so it is matched against the fixed shape a
            backup is created with and the resolved path is required to sit
            directly in the backups directory: a name of "../.." would otherwise
            read a manifest from anywhere on disk.
            """

            name = _text(args, "backup")
            if not _BACKUP_NAME.fullmatch(name):
                raise ValueError("that is not the name of a backup")
            candidate = (backups / name).resolve()
            if candidate.parent != backups.resolve():
                raise ValueError("that is not the name of a backup")
            return candidate

        if action == "backup":
            destination = backups / datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            backup_state(self.state_dir, destination)
            return {
                "backup": destination.name,
                "files": list(restorable(destination)),
                "intact": not verify_backup(destination),
            }
        if action in {"verify_backup", "restorable"}:
            destination = named_backup()
            mismatched = verify_backup(destination)
            return {
                "backup": destination.name,
                "intact": not mismatched,
                "mismatched": list(mismatched),
                "would_restore": list(restorable(destination)),
            }
        if action == "rollback_plan":
            # Releases are root-owned and outside every mount this process has,
            # so this hands back the operator step rather than reading a
            # directory it cannot see and reporting an empty one as the truth.
            return rollback_guidance()
        if action == "provider_watch":
            return self.watch_providers()
        if action == "supervision":
            return {
                "consumer_lease_holder": self.consumer_lease.holder(),
                "open_incidents": list(self.incidents.open_incidents()),
                "restart_allowed": self.supervisor.should_restart().allowed,
            }
        raise ValueError("maintenance action is not permitted")

    def advance_workflow_runs(self) -> dict[str, int]:
        """One supervision pass over every client user's workflows.

        Two things happen here and nothing else: a scheduled workflow whose
        window has come is started, and a run that is open is carried forward.
        Both are safe to repeat, because the window is the run's identity and a
        finished step is never claimed twice — so a pass that ran a second time,
        or a restart in the middle of one, does no work over again.

        A run waiting on an approval, paused, or holding an ambiguous effect is
        left exactly where it is. None of those are waiting on a loop.
        """

        started = 0
        advanced = 0
        for role in (Role.MAIN_OPERATOR, Role.EMPLOYEE):
            principal = self.config.principal_for(role)
            if principal is None:
                continue
            for workflow in self.workflows.list(role):
                if workflow.state is not WorkflowState.ACTIVE:
                    continue
                trigger = due_trigger(workflow, datetime.now(UTC))
                if trigger is None or self.workflow_runs.find(workflow, trigger) is not None:
                    # Not due, or this window's run already exists. The loop
                    # comes round every second; a window is what stops that
                    # from being a run every second.
                    continue
                try:
                    run = self.workflow_runs.start(workflow, principal, trigger)
                except RunError:
                    # Past the workflow's own daily limit, or the trigger does
                    # not carry what makes it unique. Nothing is owed.
                    continue
                started += 1
                self._advance_quietly(run.run_id, workflow, principal)
            for run in self._open_runs(role):
                try:
                    workflow = self.workflows.get(run.workflow_id, role)
                except WorkflowError:
                    continue
                advanced += int(self._advance_quietly(run.run_id, workflow, principal))
        return {"started": started, "advanced": advanced}

    def _open_runs(self, role: Role) -> list[Run]:
        """Every run with somewhere to go, walked a page at a time.

        Asking for the oldest page each pass would mean a page of runs that
        cannot move hides every run behind it forever. The cursor is what makes
        this a walk rather than a repeated look at the same fifty.
        """

        collected: list[Run] = []
        cursor = ""
        while len(collected) < MAX_RUNS_PER_PASS:
            page = self.workflow_runs.open_runs(role, after=cursor, limit=OPEN_RUN_PAGE)
            if not page:
                break
            collected.extend(page)
            cursor = page[-1].cursor
        return collected[:MAX_RUNS_PER_PASS]

    def settle_workflow_approval(
        self, approval_id: str, outcome: StepState, *, detail: str = ""
    ) -> str | None:
        """Tell the run what became of the step it was waiting on.

        Without this the link was missing entirely: a consequence step raised a
        proposal, somebody approved it, the effect happened, and the run stayed
        parked forever because nothing carried the answer back.
        """

        try:
            return self.workflow_runs.settle_approval(approval_id, outcome, detail=detail)
        except RunError:
            return None

    def _advance_quietly(self, run_id: str, workflow: Workflow, principal: Principal) -> bool:
        """Carry one run forward, letting a failure stay in the ledger."""

        try:
            self.workflow_runner.advance(run_id, workflow, principal)
        except (RunError, WorkflowError, ValueError) as exc:
            logger.warning("Workflow run could not advance: %s", type(exc).__name__)
            return False
        return True

    def watch_providers(self) -> dict[str, str]:
        """Check each provider, open or close its breaker, and alert once.

        The supervisor does this rather than the model, so a provider that goes
        away is noticed whether or not anyone happens to be talking to Scotty.
        """

        report: dict[str, str] = {}
        for provider, connected in self.provider_connection_status().items():
            if not connected:
                report[provider] = HealthState.NOT_CONFIGURED.value
                continue
            state = self.budgets.breaker(provider)
            if state.open:
                report[provider] = HealthState.BLOCKED.value
                if self.incidents.should_alert(f"{provider}_unavailable"):
                    self._alert_maintainer(
                        f"{provider} is failing repeatedly and is being left to recover."
                    )
                continue
            report[provider] = HealthState.HEALTHY.value
            if self.incidents.should_alert_recovery(f"{provider}_unavailable"):
                self._alert_maintainer(f"{provider} is answering again.")
        return report

    def record_provider_failure(self, provider: str) -> None:
        """One failed provider call, counted toward that provider's breaker."""

        self.budgets.record_failure(provider)

    def record_provider_success(self, provider: str) -> None:
        self.budgets.record_success(provider)

    def _alert_maintainer(self, message: str) -> None:
        """One redacted line to the maintainer's own channel, budget-bound."""

        maintainer = Principal(
            guild_id=self.config.maintainer_route.guild_id,
            channel_id=self.config.maintainer_route.channel_id,
            user_id=self.config.maintainer_route.user_id,
            role=Role.MAINTAINER,
        )
        if not self.budgets.spend(maintainer, "incident_alert").allowed:
            return
        with suppress(Exception):
            self.discord.send_message(self.config.maintainer_route.channel_id, message)

    def identity_for(self, principal: Principal) -> ProviderIdentity:
        """This caller's provider identity, for attribution on every effect."""

        return self.identities.resolve(principal)

    def _transport_for(self, principal: Principal) -> BrokeredTransport:
        """One transport, carrying this exact person's own citation.

        Built per principal rather than per role, because the citation is per
        message. Two people's work therefore cannot share a transport even by
        accident, which is what let an employee's effect leave through the main
        operator's connector before.
        """

        return BrokeredTransport(self.provider_broker, provenance=principal.citation())

    def _reaches(self, principal: Principal, provider: str) -> bool:
        """Whether this exact person has a usable route to that provider.

        Asked of the broker, per person, per call. The deployment holding a
        credential is not the same question, and answering the deployment's
        question for a person is what let one user's work leave through
        another's connector.
        """

        if not self.connected.get(provider):
            return False
        credential_class = BROKER_CREDENTIALS.get(provider)
        if credential_class is None:  # pragma: no cover - table is exhaustive
            return False
        return self._held_for(principal, provider, credential_class)

    def _trello(self, principal: Principal) -> TrelloReadPort:
        if not self._reaches(principal, "trello") or self.trello_scope is None:
            return UnconnectedProvider("Trello")
        return TrelloAdapter(
            self._transport_for(principal), _BROKERED, _BROKERED, self.trello_scope
        )

    def _ghl(self, principal: Principal) -> GHLReadPort:
        if not self._reaches(principal, "ghl") or self.ghl_location_id is None:
            return UnconnectedProvider("GoHighLevel")
        return GHLAdapter(self._transport_for(principal), _BROKERED, self.ghl_location_id)

    def _rentcast(self, principal: Principal) -> RentCastPort:
        if not self._reaches(principal, "rentcast") or not self.rentcast_endpoints:
            return UnconnectedProvider("RentCast")
        return RentCastAdapter(self._transport_for(principal), _BROKERED, self.rentcast_endpoints)

    def _property_engine(self, principal: Principal) -> PropertyCardEngine | None:
        """The card engine, bound to whoever is asking.

        One shared engine meant one shared connector: an employee creating a
        card sent it through the main operator's Trello identity and recorded it
        against the main operator's effects. The engine is per principal now,
        over that principal's own transport.
        """

        trello = self._trello(principal)
        if isinstance(trello, UnconnectedProvider):
            return None
        return PropertyCardEngine(self.config, trello, self.property_effects)

    def _provider_setup(
        self, principal: Principal, args: Mapping[str, object]
    ) -> dict[str, object]:
        """Answer one guided setup turn: explain, validate, diagnose, or resume."""

        # Setup state is personal: this user's own Google account, their own
        # connected providers, and their own next step.
        status = self.actor_connection_status(principal)
        staged = self.setup_staging.read()
        progress = setup_progress(self.config, status, staged, role=principal.role)
        name = _text(args, "provider", optional=True)
        if name is None:
            resume = first_unfinished(progress)
            return {
                "providers": {
                    provider: _guidance_json(provider, status[provider]) for provider in PROVIDERS
                },
                # What the root-owned broker holds. A provider can be connected
                # from the process environment without the broker holding
                # anything, so this is reported separately rather than merged.
                "broker_held_credentials": self.credential_store_status(),
                "progress": [_progress_json(item) for item in progress],
                "resume_at": resume.provider if resume is not None else None,
                "next_action": (
                    resume.next_action
                    if resume is not None
                    else "Every integration is connected. Nothing further is required."
                ),
            }
        if name not in PROVIDERS:
            raise ValueError("provider is not part of this deployment")
        current = next(item for item in progress if item.provider == name)
        failure = _text(args, "setup_failure", optional=True)
        if failure is not None:
            return {
                "provider": name,
                "diagnosis": diagnose(name, failure),
                "next_action": current.next_action,
            }
        field = _text(args, "setup_field", optional=True)
        raw = _text(args, "raw", optional=True)
        if field is not None and raw is not None:
            if name != "google_workspace" and principal.role not in _WORKSPACE_WRITE_ROLES:
                # Staged values become root setup's prefill, so shared
                # deployment identifiers stay with the operator. A user's own
                # Workspace account is theirs to give.
                raise PermissionError("only the main operator may supply a setup identifier")
            # Only non-secret identifiers are ever collected here, and they are
            # staged for local setup rather than applied to live configuration.
            try:
                staged = self.setup_staging.stage(name, field, raw, role=principal.role)
            except SetupFlowError as exc:
                return {"provider": name, "accepted": False, "correction": str(exc)}
            refreshed = setup_progress(self.config, status, staged, role=principal.role)
            resume = first_unfinished(refreshed)
            return {
                "provider": name,
                "field": field,
                "accepted": True,
                "next_action": resume.next_action if resume is not None else current.next_action,
            }
        answer = {**_guidance_json(name, status[name]), **_progress_json(current)}
        if name == "google_workspace" and not status[name]:
            prompt = read_consent_prompt(google_prompt_path(principal.role, self.state_dir))
            if prompt is not None:
                # Presenting the URL is safe: it carries the client id and the
                # scopes, never the client secret, the verifier, or a token.
                answer["consent"] = prompt
                answer["next_action"] = (
                    "Open the authorization URL as the configured Workspace account, "
                    "approve it, then give the address you land on to the operator for "
                    "the local setup command. Scotty cannot accept it here. Then run "
                    f"{LOCAL_SETUP_COMMAND}."
                )
        return answer

    def handle_read(self, principal: Principal, args: Mapping[str, object]) -> object:
        # Nothing the model wrote may name whose identity this call runs as.
        # The actor is the authorized Discord origin, resolved before any
        # provider is touched.
        reject_identity_override(args)
        # Every provider this call may touch is chosen by the authenticated
        # actor, so one user's session can never reach another's identity.
        workspace = self._workspace(principal)
        trello = self._trello(principal)
        ghl = self._ghl(principal)
        rentcast = self._rentcast(principal)
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
                # This caller's own assistant, never the other user's and never
                # the software underneath.
                "identity": self.assistant_name(principal.role),
                "addons": list(self.config.addons),
                "addon_slots_remaining": 6 - len(self.config.addons),
            }
        if operation == "persona":
            return self._persona(principal, args)
        if operation == "property_card":
            return self._property_card(principal, args)
        if operation == "workflow":
            return self._workflow(principal, args)
        if operation == "maintenance":
            return self._maintenance(principal, args)
        if operation == "provider_setup":
            return self._provider_setup(principal, args)
        if operation == "discord":
            return self.handle_discord(principal, args)
        if operation == "google_workspace":
            google_operation = _text(args, "google_operation")
            payload = _object(args, "payload", optional=True)
            resource_id = _text(args, "resource_id", optional=True) or "new"
            # Each client user works in their own Workspace, so routine
            # reversible work needs no approval from anyone else. Consequence
            # actions are still gated, and an employee still cannot approve one.
            if google_operation in {"search_gmail", "search_drive"}:
                query = payload.get("query", "")
                maximum = payload.get("max_results", 50)
                if type(query) is not str or type(maximum) is not int:
                    raise ValueError("Google search query or max_results is malformed")
                records = (
                    workspace.search_gmail(query, max_results=maximum)
                    if google_operation == "search_gmail"
                    else workspace.search_drive(query, max_results=maximum)
                )
                return [_record_json(item) for item in records]
            if google_operation == "list_calendar_events":
                calendar_id = payload.get("calendar_id", "primary")
                maximum = payload.get("max_results", 50)
                if type(calendar_id) is not str or type(maximum) is not int:
                    raise ValueError("Google Calendar list request is malformed")
                return [
                    _record_json(item)
                    for item in workspace.list_calendar_events(
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
                return _record_json(workspace.get_calendar_event(calendar_id, event_id))
            if google_operation == "read_drive_file":
                return _record_json(workspace.read_drive_file(resource_id))
            if google_operation == "get_sheet_values":
                target = payload.get("range")
                if type(target) is not str:
                    raise ValueError("spreadsheet range is malformed")
                return _record_json(workspace.get_sheet_values(resource_id, target))
            if google_operation == "batch_get_sheet_values":
                ranges = payload.get("ranges")
                if not isinstance(ranges, list):
                    raise ValueError("spreadsheet ranges are malformed")
                return _record_json(workspace.batch_get_sheet_values(resource_id, ranges))
            if google_operation == "list_contacts":
                maximum = payload.get("max_results", 100)
                if type(maximum) is not int:
                    raise ValueError("Google Contacts max_results is malformed")
                return [_record_json(item) for item in workspace.list_contacts(page_size=maximum)]
            getters = {
                "get_gmail_message": workspace.get_gmail_message,
                "get_drive_file": workspace.get_drive_file,
                "get_document": workspace.get_document,
                "get_spreadsheet": workspace.get_spreadsheet,
                "get_contact": workspace.get_contact,
            }
            getter = getters.get(google_operation)
            if getter is not None:
                return _record_json(getter(resource_id))
            try:
                return _record_json(
                    workspace.execute_routine(google_operation, resource_id, payload)
                )
            except AmbiguousEffectError as exc:
                # The write may or may not have landed. Say so plainly so the
                # caller reconciles rather than repeating the mutation.
                return {
                    "status": "unknown",
                    "operation": google_operation,
                    "reconcile_before_retry": True,
                    "reason": str(exc),
                }
        if operation == "trello_card":
            return _record_json(trello.get_card(_text(args, "card_id")))
        if operation == "trello_cards":
            return [_record_json(item) for item in trello.list_cards()]
        if operation == "ghl_contact":
            return _record_json(ghl.get_contact(_text(args, "contact_id")))
        if operation == "ghl_conversations":
            return [
                _record_json(item) for item in ghl.search_conversations(_text(args, "contact_id"))
            ]
        if operation == "ghl_message":
            return _record_json(
                ghl.get_message(
                    _text(args, "conversation_id"),
                    _text(args, "message_id"),
                    _text(args, "contact_id"),
                )
            )
        if operation == "rentcast":
            endpoint = _text(args, "endpoint")
            return _record_json(rentcast.fetch(endpoint, _object(args, "query")))
        if operation == "google_gmail_message":
            return _record_json(workspace.get_gmail_message(_text(args, "message_id")))
        if operation == "google_gmail_draft":
            return _record_json(workspace.create_gmail_draft(_text(args, "raw")))
        if operation == "google_calendar_event":
            return _record_json(
                workspace.get_calendar_event(_text(args, "calendar_id"), _text(args, "event_id"))
            )
        if operation == "google_drive_file":
            return _record_json(workspace.get_drive_file(_text(args, "file_id")))
        if operation == "google_document":
            return _record_json(workspace.get_document(_text(args, "document_id")))
        if operation == "google_spreadsheet":
            return _record_json(workspace.get_spreadsheet(_text(args, "spreadsheet_id")))
        if operation == "google_contact":
            return _record_json(workspace.get_contact(_text(args, "resource_name")))
        raise ValueError("read operation is not permitted")

    def _reporter(self, channel_id: str, task_id: str) -> ProgressReporter:
        """One coalescing reporter per task, evicted by least recent use."""

        key = (channel_id, task_id)
        reporter = self._reporters.pop(key, None)
        if reporter is None:
            reporter = ProgressReporter(
                channel_id,
                self.discord.send_message,
                self.discord.edit_own_message,
                clock=time.monotonic,
            )
        while len(self._reporters) >= 64:
            # Drop the least recently used task, never the one being written to.
            self._reporters.pop(next(iter(self._reporters)))
        self._reporters[key] = reporter
        return reporter

    def _record_send(self, principal: Principal, channel_id: str) -> int:
        """Count what Scotty actually sent for this caller inside the window."""

        now = time.monotonic()
        key = (principal.user_id, channel_id)
        recent = [stamp for stamp in self._sends.get(key, ()) if now - stamp < BULK_WINDOW_SECONDS]
        recent.append(now)
        self._sends[key] = recent
        return len(recent)

    def handle_discord(self, principal: Principal, args: Mapping[str, object]) -> object:
        """Perform one typed, classified Discord action for this exact caller."""

        discord_operation = _text(args, "discord_operation")
        payload = _object(args, "payload", optional=True)
        destinations = permitted_destinations(self.config, principal)
        channel_id = _text(payload, "channel_id", optional=True) or principal.channel_id
        content = _text(payload, "content", optional=True) or ""
        sending = discord_operation in _SENDING_OPERATIONS
        classified = classify_discord_action(
            discord_operation,
            {
                **payload,
                "channel_id": channel_id,
                "content": content,
                # Volume is measured from what Scotty actually sent, never from
                # a count the caller supplies.
                "message_count": self._record_send(principal, channel_id) if sending else 0,
            },
            destinations=destinations,
            shared=shared_destinations(self.config),
            guild_id=self.config.principals[0].guild_id,
            private_channels=protected_channels(self.config),
        )
        if classified is not DiscordActionClass.ROUTINE:
            # Consequence work goes through a proposal; everything else is absent.
            raise PermissionError(redacted_refusal(discord_operation, classified))

        if sending:
            # Volume limits are per person and explained rather than silent.
            decision = self.budgets.spend(principal, "chat_message")
            if not decision.allowed:
                raise PermissionError(decision.reason)
        message_id = _text(payload, "message_id", optional=True)
        if discord_operation == "read_channel":
            limit = payload.get("limit", 20)
            if type(limit) is not int:
                raise ValueError("Discord read limit is malformed")
            return [dict(item) for item in self.discord.read_channel(channel_id, limit=limit)]
        if discord_operation == "read_message":
            return dict(self.discord.get_message(channel_id, _required(message_id, "message id")))
        if discord_operation == "send_message":
            return dict(self.discord.send_message(channel_id, content))
        if discord_operation == "reply_message":
            return dict(
                self.discord.reply_message(channel_id, _required(message_id, "message id"), content)
            )
        if discord_operation == "edit_own_message":
            return dict(
                self.discord.edit_own_message(
                    channel_id, _required(message_id, "message id"), content
                )
            )
        if discord_operation == "delete_own_message":
            return {
                "deleted": self.discord.delete_own_message(
                    channel_id, _required(message_id, "message id")
                )
            }
        if discord_operation in {"add_reaction", "remove_own_reaction"}:
            emoji = _text(payload, "emoji")
            reaction = (
                self.discord.add_reaction
                if discord_operation == "add_reaction"
                else self.discord.remove_own_reaction
            )
            return {"reacted": reaction(channel_id, _required(message_id, "message id"), emoji)}
        if discord_operation == "attach_file":
            return dict(self.discord.attach_file(channel_id, content, self._attachment(payload)))
        if discord_operation == "create_thread":
            return {
                "thread_id": self.discord.create_thread(
                    channel_id, _text(payload, "name"), message_id or None
                )
            }
        if discord_operation == "send_thread_message":
            return dict(
                self.discord.send_thread_message(
                    _text(payload, "thread_id"), content, allowed_parents=destinations
                )
            )
        if discord_operation == "archive_own_thread":
            return {
                "archived": self.discord.archive_own_thread(
                    _text(payload, "thread_id"), allowed_parents=destinations
                )
            }
        reporter = self._reporter(channel_id, _text(payload, "task_id"))
        # A finished task always writes its final state, whatever the rate limit
        # and the edit budget would otherwise have deferred.
        outcome = (
            reporter.finish(content) if payload.get("final") is True else reporter.update(content)
        )
        return {"progress": outcome.state.value, "message_id": outcome.message_id}

    def _attachment(self, payload: Mapping[str, object]) -> Attachment:
        """Read one approved file from Scotty's own outbox, never anywhere else."""

        name = _text(payload, "filename")
        if name != Path(name).name or name.startswith("."):
            raise ValueError("attachment filename is malformed")
        path = self.state_dir / "outbox" / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("attachment is not present in Scotty's outbox")
        data = path.read_bytes()
        if not data or len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment size is outside the permitted range")
        return Attachment(name, _ATTACHMENT_TYPES.get(path.suffix.lower(), ""), data)

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
        elif operation == "discord_administration":
            proposal = self.service.propose_discord_administration(
                principal,
                _text(args, "discord_operation"),
                _object(args, "payload", optional=True),
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
        # A proposal a workflow raised is a step some run is parked on. Carrying
        # the outcome back is what lets that run finish; without it the effect
        # happened and the workflow waited forever.
        self._settle_waiting_step(result)
        return _proposal_json(result)

    def _settle_waiting_step(self, proposal: Proposal) -> None:
        """Tell whatever run raised this proposal what became of it."""

        outcome = {
            ProposalStatus.VERIFIED: StepState.DONE,
            ProposalStatus.DENIED: StepState.FAILED,
            ProposalStatus.FAILED: StepState.FAILED,
            ProposalStatus.EXPIRED: StepState.FAILED,
            ProposalStatus.UNKNOWN: StepState.UNKNOWN,
        }.get(proposal.status)
        if outcome is None:
            # Still proposed or approved but not executed: the run is still
            # waiting, and correctly so.
            return
        self.settle_workflow_approval(proposal.proposal_id, outcome, detail=proposal.status.value)

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
        # Identifies this process to the consumer lease. Not a secret and not
        # an identity: it says which process holds the singleton, nothing more.
        self._process_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
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
        with suppress(Exception):
            if self._runtime is not None:
                self._runtime.consumer_lease.release(self._process_id)

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
        first_pass = True
        while not self._stop.wait(1.0):
            try:
                runtime = self.runtime()
            except Exception:  # noqa: S112 - setup may not have published state yet
                # Setup may not have published private state yet.
                continue
            if first_pass:
                # One process consumes Discord. A restart that overlaps its
                # predecessor, or a second container, is refused the lease and
                # is told so rather than quietly doubling every message.
                runtime.supervisor.record_restart()
                first_pass = False
            if not runtime.consumer_lease.claim(self._process_id):
                logger.warning("Another consumer holds the Discord lease; standing down.")
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
            try:
                runtime.watch_providers()
            except Exception as exc:
                logger.warning("Provider watch failed: %s", type(exc).__name__)
            try:
                # Only the process holding the consumer lease does this, so a
                # second container cannot fire the same schedule alongside it.
                runtime.advance_workflow_runs()
            except Exception as exc:
                logger.warning("Workflow pass failed: %s", type(exc).__name__)
