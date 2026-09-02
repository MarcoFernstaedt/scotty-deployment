from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from .adapters import AmbiguousEffectError, ProviderError, ProviderRecord
from .adapters.discord_admin import DiscordAdminAdapter
from .approvals import ApprovalError, ApprovalStore, Proposal, ProposalStatus
from .calculations import preliminary_analysis
from .config import RuntimeConfig, TrelloScope
from .discord_policy import (
    DiscordActionClass,
    announcement_is_safe,
    classify_discord_action,
    protected_channels,
)
from .google_policy import GoogleActionClass, classify_google_action
from .policy import Principal, Role


def _optional_text(payload: Mapping[str, object], field: str) -> str:
    """A payload string that may legitimately be absent."""

    value = payload.get(field)
    if value is None:
        return ""
    if type(value) is not str:
        raise ApprovalError(f"proposal {field} is malformed")
    return value


def _utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_address(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ProviderError("property address must be a non-empty string")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def _string_field(record: ProviderRecord, *names: str) -> str | None:
    for name in names:
        value = record.fields.get(name)
        if type(value) is str and value.strip():
            return value.strip()
    return None


def _payload_text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if type(value) is not str or not value:
        raise ApprovalError(f"proposal {field} is malformed")
    return value


class TrelloPort(Protocol):
    def get_card(self, card_id: str) -> ProviderRecord: ...
    def create_card(self, list_id: str, fields: Mapping[str, object]) -> ProviderRecord: ...
    def update_card(self, card_id: str, fields: Mapping[str, object]) -> ProviderRecord: ...
    def move_card(self, card_id: str, list_id: str) -> ProviderRecord: ...
    def archive_card(self, card_id: str) -> ProviderRecord: ...
    def unarchive_card(self, card_id: str) -> ProviderRecord: ...
    def set_labels(self, card_id: str, label_ids: Sequence[str]) -> ProviderRecord: ...


class GHLPort(Protocol):
    def get_contact(self, contact_id: str) -> ProviderRecord: ...
    def send_sms(
        self, contact_id: str, normalized_destination: str, body: str
    ) -> Mapping[str, str]: ...
    def get_message(
        self, conversation_id: str, message_id: str, contact_id: str
    ) -> ProviderRecord: ...


class DiscordPort(Protocol):
    def send_message(self, channel_id: str, content: str) -> Mapping[str, str]: ...
    def get_message(self, channel_id: str, message_id: str) -> Mapping[str, object]: ...


class RentCastPort(Protocol):
    def fetch(self, endpoint: str, query: Mapping[str, object]) -> ProviderRecord: ...


class GoogleWorkspacePort(Protocol):
    def mutate(
        self, operation: str, resource_id: str, payload: Mapping[str, object]
    ) -> ProviderRecord: ...


#: The payload keys an administration proposal is allowed to carry, so an
#: approval records exactly what it authorizes and nothing else.
_ADMIN_PAYLOAD_KEYS = frozenset(
    {
        "channel_id",
        "channel_ids",
        "name",
        "topic",
        "parent_id",
        "changes",
        "overwrites",
        "user_id",
        "role_id",
        "start",
        "description",
        "positions",
        "content",
    }
)


class ScottyService:
    """Bounded business orchestration over typed provider adapters."""

    def __init__(
        self,
        config: RuntimeConfig,
        approvals: ApprovalStore,
        *,
        trello: TrelloPort | Callable[[Principal], TrelloPort],
        ghl: GHLPort | Callable[[Principal], GHLPort],
        rentcast: RentCastPort | None | Callable[[Principal], RentCastPort],
        discord: DiscordPort,
        discord_admin: DiscordAdminAdapter | None = None,
        google_workspace: GoogleWorkspacePort
        | None
        | Callable[[Principal], GoogleWorkspacePort | None] = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.config = config
        self.approvals = approvals
        self.trello = trello
        self.ghl = ghl
        self.rentcast = rentcast
        self.discord = discord
        self.discord_admin = discord_admin
        self.google_workspace = google_workspace
        self.clock = clock

    def _trello_for(self, actor: Principal) -> TrelloPort:
        """This exact person's Trello connector, never the deployment's first.

        Holding one adapter for the service meant every approved effect left
        through whichever actor happened to build it -- in practice the main
        operator, for the employee's cards as well as their own.
        """

        return self.trello(actor) if callable(self.trello) else self.trello

    def _ghl_for(self, actor: Principal) -> GHLPort:
        return self.ghl(actor) if callable(self.ghl) else self.ghl

    def _rentcast_for(self, actor: Principal) -> RentCastPort | None:
        return self.rentcast(actor) if callable(self.rentcast) else self.rentcast

    def _workspace_for(self, actor: Principal) -> GoogleWorkspacePort | None:
        """The Workspace this exact actor may act on, and no other.

        Each client user connects their own account, so the adapter is chosen
        by the authenticated actor rather than held once for the deployment.
        """

        workspace = self.google_workspace
        if callable(workspace):
            # The whole actor, not just their role: the adapter it builds
            # carries a token minted against this person's own citation.
            return workspace(actor)
        return workspace

    def _now(self) -> datetime:
        now = self.clock()
        if type(now) is not datetime or now.tzinfo is None:
            raise ApprovalError("clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    def _approver_for(self, requester: Principal) -> Principal:
        if requester.role in {Role.MAINTAINER, Role.MAIN_OPERATOR}:
            return requester
        for principal in self.config.principals:
            if principal.role == Role.MAIN_OPERATOR:
                return principal
        raise ApprovalError("main-operator approver is not configured")

    def _trello_scope(self) -> TrelloScope:
        scope = self.config.trello
        if scope is None:
            raise ProviderError("Trello is not connected")
        return scope

    def _ghl_location(self) -> str:
        location = self.config.ghl_location_id
        if location is None:
            raise ProviderError("GoHighLevel is not connected")
        return location

    def approve(self, principal: Principal, proposal_id: str, expected_version: int) -> Proposal:
        return self.approvals.approve(proposal_id, principal, expected_version)

    def deny(self, principal: Principal, proposal_id: str, expected_version: int) -> Proposal:
        return self.approvals.deny(proposal_id, principal, expected_version)

    def propose_trello_merge(
        self,
        requester: Principal,
        source_card_id: str,
        destination_card_id: str,
        *,
        resolutions: Mapping[str, str] | None = None,
    ) -> Proposal:
        """Propose one duplicate merge, with its conflicts already decided.

        Where the two cards disagree, the destination used to win by default
        and the choice was never anybody's. `resolutions` names the side to
        keep per field -- "source" or "destination" -- and travels in the
        payload, so what an approver reads and approves is the resolution
        rather than an instruction to merge and decide afterwards.

        An unresolved conflict is not fatal: the default still applies and the
        payload says which fields took it, so a reviewer can see what they are
        agreeing to.
        """

        if source_card_id == destination_card_id:
            raise ProviderError("merge source and destination must differ")
        trello = self._trello_for(requester)
        source = trello.get_card(source_card_id)
        destination = trello.get_card(destination_card_id)
        source_address = _string_field(source, "normalized_address", "address", "name")
        destination_address = _string_field(destination, "normalized_address", "address", "name")
        source_provider_id = _string_field(
            source, "provider_property_id", "rentcast_id", "property_id"
        )
        destination_provider_id = _string_field(
            destination, "provider_property_id", "rentcast_id", "property_id"
        )
        exact_address = (
            source_address is not None
            and destination_address is not None
            and normalize_address(source_address) == normalize_address(destination_address)
        )
        exact_provider = (
            source_provider_id is not None
            and destination_provider_id is not None
            and source_provider_id == destination_provider_id
        )
        if not exact_address and not exact_provider:
            raise ProviderError(
                "duplicate merge requires an exact normalized address or provider property identifier"
            )

        chosen = self._resolutions(resolutions)
        merge_keys = ("name", "desc", "due", "dueComplete", "idLabels")
        conflicts: dict[str, dict[str, object]] = {}
        merged_fields: dict[str, object] = {}
        defaulted: list[str] = []
        for field in merge_keys:
            source_value = source.fields.get(field)
            destination_value = destination.fields.get(field)
            disagrees = source_value != destination_value
            side = chosen.get(field)
            if side is not None:
                # Somebody chose. Their choice is what merges, including a
                # choice of a value the default would have discarded.
                picked = source_value if side == "source" else destination_value
                if picked not in (None, "", []):
                    merged_fields[field] = picked
                kept = side
            else:
                if disagrees:
                    defaulted.append(field)
                # The default: the destination wins unless it has nothing.
                if destination_value not in (None, "", []):
                    merged_fields[field] = destination_value
                    kept = "destination"
                elif source_value not in (None, "", []):
                    merged_fields[field] = source_value
                    kept = "source"
                else:
                    kept = "neither"
            if disagrees:
                conflicts[field] = {
                    "source": source_value,
                    "destination": destination_value,
                    # Which side the merged value actually came from, rather
                    # than which side the rule nominally prefers: a default
                    # that says "destination" over an empty destination would
                    # describe a merge that did not happen.
                    "resolved_to": kept,
                    "chosen_by_reviewer": side is not None,
                }
        payload: dict[str, object] = {
            "operation": "merge",
            "source_card_id": source.source_id,
            "destination_card_id": destination.source_id,
            "duplicate_evidence": {
                "exact_normalized_address": exact_address,
                "exact_provider_property_id": exact_provider,
            },
            "source_fields": dict(source.fields),
            "destination_fields": dict(destination.fields),
            "conflicts": conflicts,
            "resolutions": dict(chosen),
            # Named so an approver can see which disagreements nobody decided
            # and the destination simply won.
            "defaulted_conflicts": sorted(defaulted),
            "merged_fields": merged_fields,
        }
        return self.approvals.propose(
            requester=requester,
            approver=self._approver_for(requester),
            action_class="trello_write",
            target_ids=(
                self._trello_scope().board_id,
                source.source_id,
                destination.source_id,
            ),
            payload=payload,
            source_revision=(
                f"source={source.source_revision};destination={destination.source_revision}"
            ),
            expires_at=self._now() + timedelta(minutes=10),
        )

    @staticmethod
    def _resolutions(requested: Mapping[str, str] | None) -> dict[str, str]:
        """The conflict choices, checked before they reach a payload."""

        chosen: dict[str, str] = {}
        for field, side in dict(requested or {}).items():
            if side not in {"source", "destination"}:
                raise ProviderError("a merge conflict is resolved to source or destination")
            chosen[str(field)] = side
        return chosen

    def propose_trello_action(
        self,
        requester: Principal,
        operation: str,
        card_id: str,
        fields: Mapping[str, object] | None = None,
        destination_list_id: str | None = None,
    ) -> Proposal:
        if operation not in {"update", "move", "archive", "unarchive"}:
            raise ProviderError("Trello operation is not permitted")
        current = self._trello_for(requester).get_card(card_id)
        payload = {
            "operation": operation,
            "card_id": current.source_id,
            "fields": dict(fields or {}),
            "destination_list_id": destination_list_id,
        }
        targets = [self._trello_scope().board_id, current.source_id]
        if destination_list_id:
            targets.append(destination_list_id)
        return self.approvals.propose(
            requester=requester,
            approver=self._approver_for(requester),
            action_class="trello_write",
            target_ids=tuple(targets),
            payload=payload,
            source_revision=current.source_revision,
            expires_at=self._now() + timedelta(minutes=10),
        )

    def propose_trello_bulk(self, requester: Principal, plan: object) -> Proposal:
        """Propose one previewed batch, bound to that exact preview.

        A bulk operation was classified as a consequence and then had no
        executable path at all: the dry run described a change nobody could
        make. This is the missing half, and it is deliberately narrow -- what
        is approved is this plan's hash, so an approval cannot be carried over
        to a batch assembled afterwards or to a board that has since moved.
        """

        changes = getattr(plan, "changes", ())
        operation = str(getattr(plan, "operation", ""))
        payload_hash = plan.payload_hash()  # type: ignore[attr-defined]
        card_ids = [str(change.get("card_id", "")) for change in changes]
        if not card_ids:
            raise ProviderError("a bulk operation covers at least one card")
        return self.approvals.propose(
            requester=requester,
            approver=self._approver_for(requester),
            action_class="trello_write",
            target_ids=(self._trello_scope().board_id, *card_ids),
            payload={
                "operation": "bulk",
                "card_operation_target": operation,
                "card_ids": card_ids,
                "payload": dict(getattr(plan, "arguments", {}) or {}),
                "payload_hash": payload_hash,
                "affected": len(card_ids),
                "unreadable": list(getattr(plan, "unreadable", ())),
            },
            source_revision=f"bulk={payload_hash}",
            expires_at=self._now() + timedelta(minutes=10),
        )

    def propose_trello_create(
        self,
        requester: Principal,
        list_id: str,
        fields: Mapping[str, object],
    ) -> Proposal:
        return self.approvals.propose(
            requester=requester,
            approver=self._approver_for(requester),
            action_class="trello_write",
            target_ids=(self._trello_scope().board_id, list_id),
            payload={"operation": "create", "list_id": list_id, "fields": dict(fields)},
            source_revision="configured-board-v1",
            expires_at=self._now() + timedelta(minutes=10),
        )

    def propose_ghl_sms(
        self,
        requester: Principal,
        contact_id: str,
        normalized_destination: str,
        body: str,
    ) -> Proposal:
        contact = self._ghl_for(requester).get_contact(contact_id)
        authoritative_phone = _string_field(contact, "phone")
        if authoritative_phone != normalized_destination:
            raise ProviderError("SMS destination does not match the configured contact")
        if type(body) is not str or not body.strip() or len(body) > 1600:
            raise ProviderError("SMS body must contain 1-1600 characters")
        payload = {
            "operation": "send_sms",
            "contact_id": contact.source_id,
            "normalized_destination": normalized_destination,
            "body": body,
        }
        return self.approvals.propose(
            requester=requester,
            approver=self._approver_for(requester),
            action_class="ghl_sms",
            target_ids=(
                self._ghl_location(),
                contact.source_id,
                normalized_destination,
            ),
            payload=payload,
            source_revision=contact.source_revision,
            expires_at=self._now() + timedelta(minutes=10),
        )

    def propose_discord_announcement(
        self, requester: Principal, channel_id: str, content: str
    ) -> Proposal:
        if channel_id not in self.config.announcement_channel_ids:
            raise ProviderError("Discord announcement destination is not configured")
        if type(content) is not str or not content.strip() or len(content) > 2000:
            raise ProviderError("Discord announcement must contain 1-2000 characters")
        if (
            classify_discord_action(
                "announce",
                {"channel_id": channel_id, "content": content},
                destinations=(channel_id,),
            )
            is not DiscordActionClass.CONSEQUENCE
        ):
            raise ProviderError("that Discord announcement is not permitted")
        if not announcement_is_safe(content, self.config):
            # Refused without repeating the offending text anywhere.
            raise ProviderError(
                "an announcement may not carry private channel, user, maintainer, "
                "or credential details"
            )
        return self.approvals.propose(
            requester=requester,
            approver=self._approver_for(requester),
            action_class="discord_announcement",
            target_ids=(channel_id,),
            payload={"operation": "announce", "channel_id": channel_id, "content": content},
            source_revision="configured-destination-v1",
            expires_at=self._now() + timedelta(minutes=10),
        )

    def propose_discord_administration(
        self, requester: Principal, operation: str, payload: Mapping[str, object]
    ) -> Proposal:
        """Propose one guild administration action. Never executed here.

        The guild, the private channels, and the classification are all decided
        from configuration, so an approval can only ever authorize something the
        deployment already considers administrable.
        """

        guild_id = self.config.principals[0].guild_id
        private = protected_channels(self.config)
        classified = classify_discord_action(
            operation,
            {**payload, "guild_id": guild_id},
            destinations=(),
            guild_id=guild_id,
            private_channels=private,
        )
        if classified is not DiscordActionClass.CONSEQUENCE:
            raise ProviderError("that Discord administration action is not permitted")
        target = next(
            (
                value
                for key in ("channel_id", "user_id")
                if type(value := payload.get(key)) is str and value
            ),
            "",
        )
        return self.approvals.propose(
            requester=requester,
            approver=self._approver_for(requester),
            action_class="discord_administration",
            target_ids=(guild_id, target or operation),
            payload={
                "operation": operation,
                **{key: value for key, value in payload.items() if key in _ADMIN_PAYLOAD_KEYS},
            },
            source_revision="configured-guild-v1",
            expires_at=self._now() + timedelta(minutes=10),
        )

    def propose_google_workspace_write(
        self,
        requester: Principal,
        operation: str,
        resource_id: str,
        payload: Mapping[str, object],
    ) -> Proposal:
        scope = self.config.google_for(requester.role)
        if scope is None or self._workspace_for(requester) is None:
            raise ProviderError("Google Workspace is not connected")
        if (
            not resource_id
            or classify_google_action(operation, payload) is not GoogleActionClass.CONSEQUENCE
        ):
            raise ProviderError("Google Workspace consequence is not permitted")
        return self.approvals.propose(
            requester=requester,
            approver=self._approver_for(requester),
            action_class="google_workspace_consequence",
            target_ids=(scope.account_email, resource_id),
            payload={
                "operation": operation,
                "resource_id": resource_id,
                "payload": dict(payload),
            },
            source_revision="configured-google-resource-v1",
            expires_at=self._now() + timedelta(minutes=10),
        )

    def execute(
        self,
        principal: Principal,
        proposal_id: str,
        *,
        expected_version: int,
        execution_nonce: str,
    ) -> Proposal:
        proposal = self.approvals.get(proposal_id)
        if proposal.action_class == "ghl_sms":
            return self._execute_sms(principal, proposal, expected_version, execution_nonce)
        if proposal.action_class == "trello_write":
            return self._execute_trello(principal, proposal, expected_version, execution_nonce)
        if proposal.action_class == "discord_announcement":
            return self._execute_announcement(
                principal, proposal, expected_version, execution_nonce
            )
        if proposal.action_class == "discord_administration":
            return self._execute_discord_administration(
                principal, proposal, expected_version, execution_nonce
            )
        if proposal.action_class == "google_workspace_consequence":
            return self._execute_google(principal, proposal, expected_version, execution_nonce)
        raise ApprovalError("proposal action class is unsupported")

    def _execute_discord_administration(
        self, principal: Principal, proposal: Proposal, expected_version: int, nonce: str
    ) -> Proposal:
        """Run one approved administration action against the configured guild."""

        if self.discord_admin is None:
            raise ApprovalError("Discord administration is not available")
        operation = _payload_text(proposal.payload, "operation")
        guild_id = self.config.principals[0].guild_id
        if guild_id not in proposal.target_ids:
            raise ApprovalError("this proposal is bound to another guild")
        executing = self._claim(principal, proposal, expected_version, nonce, "configured-guild-v1")
        try:
            self.discord_admin.require_permission(operation)
            receipt = self._run_administration(operation, proposal.payload)
        except AmbiguousEffectError as exc:
            return self._unknown(executing, {"verified": False, "reason": str(exc)})
        except (ProviderError, ValueError, ApprovalError) as exc:
            # A malformed payload discovered after the claim still has to settle
            # the proposal, or it stays executing until the next recovery pass.
            return self._failed(executing, str(exc))
        return self.approvals.complete_execution(
            executing.proposal_id,
            ProposalStatus.VERIFIED,
            expected_version=executing.version,
            receipt={"verified": True, **receipt},
        )

    def _run_administration(
        self, operation: str, payload: Mapping[str, object]
    ) -> dict[str, object]:
        """Dispatch one administration operation to its typed adapter call."""

        admin = self.discord_admin
        assert admin is not None  # noqa: S101 - guarded by the caller
        # Most administration names no existing channel, so this is read
        # optionally: requiring it here would strand every approved creation.
        channel_id = _optional_text(payload, "channel_id")
        if operation in {"create_channel", "create_category"}:
            created = admin.create_channel(
                _payload_text(payload, "name"),
                kind="category" if operation == "create_category" else "text",
                parent_id=_optional_text(payload, "parent_id"),
                topic=_optional_text(payload, "topic"),
                overwrites=overwrites
                if isinstance(overwrites := payload.get("overwrites"), list)
                else None,
            )
            return {"channel_id": str(created.get("id", ""))}
        if operation == "edit_channel":
            changes = payload.get("changes")
            if not isinstance(changes, Mapping):
                raise ProviderError("a channel edit needs the changes to make")
            admin.edit_channel(channel_id, changes)
            return {"channel_id": channel_id}
        if operation == "archive_channel":
            admin.archive_channel(channel_id)
            return {"channel_id": channel_id, "archived": True}
        if operation == "set_channel_permissions":
            overwrites = payload.get("overwrites")
            if not isinstance(overwrites, list):
                raise ProviderError("a permission change needs its overwrites")
            admin.set_channel_permissions(channel_id, overwrites)
            return {"channel_id": channel_id}
        if operation in {"assign_role", "remove_role"}:
            user_id = _payload_text(payload, "user_id")
            role_id = _payload_text(payload, "role_id")
            if operation == "remove_role":
                return admin.remove_role(user_id, role_id).as_json()
            # The role's own position, managed flag and permission bits are read
            # from the guild inside the adapter, never taken from the proposal.
            return admin.assign_role(user_id, role_id).as_json()
        if operation == "create_event":
            return dict(
                admin.create_event(
                    _payload_text(payload, "name"),
                    _payload_text(payload, "start"),
                    channel_id=channel_id,
                    description=_optional_text(payload, "description"),
                )
            )
        if operation == "reorder_channels":
            positions = payload.get("positions")
            if not isinstance(positions, list):
                raise ProviderError("a reorder needs each channel and the position to put it in")
            return {"reordered": list(admin.reorder_channels(positions))}
        if operation == "create_forum_post":
            return dict(
                admin.create_forum_post(
                    channel_id,
                    _payload_text(payload, "name"),
                    _payload_text(payload, "content"),
                )
            )
        if operation == "create_webhook":
            return dict(admin.create_webhook(channel_id, _payload_text(payload, "name")))
        if operation == "kick_member":
            return dict(admin.kick_member(_payload_text(payload, "user_id")))
        if operation == "ban_member":
            return dict(admin.ban_member(_payload_text(payload, "user_id")))
        if operation == "read_member_permissions":
            return admin.member_permissions(_payload_text(payload, "user_id")).as_json()
        raise ProviderError("that Discord administration action is not permitted")

    def _execute_google(
        self, principal: Principal, proposal: Proposal, expected_version: int, nonce: str
    ) -> Proposal:
        # The approver authorizes the requester's action on the requester's own
        # Workspace. Approving never moves an action onto the approver's account.
        workspace = self._workspace_for(proposal.requester)
        scope = self.config.google_for(proposal.requester.role)
        if workspace is None or scope is None:
            raise ApprovalError("Google Workspace is no longer connected")
        if scope.account_email not in proposal.target_ids:
            raise ApprovalError("Google Workspace proposal is bound to another account")
        operation = _payload_text(proposal.payload, "operation")
        resource_id = _payload_text(proposal.payload, "resource_id")
        payload = proposal.payload.get("payload")
        if not isinstance(payload, Mapping):
            raise ApprovalError("Google Workspace proposal payload is malformed")
        executing = self._claim(
            principal,
            proposal,
            expected_version,
            nonce,
            "configured-google-resource-v1",
        )
        try:
            result = workspace.mutate(operation, resource_id, payload)
        except AmbiguousEffectError:
            return self._unknown(
                executing,
                {"verified": False, "reason": "ambiguous Google Workspace write"},
            )
        except ProviderError as exc:
            return self._failed(executing, str(exc))
        return self.approvals.complete_execution(
            executing.proposal_id,
            ProposalStatus.VERIFIED,
            expected_version=executing.version,
            receipt={"verified": True, "resource_id": result.source_id},
        )

    def _claim(
        self,
        principal: Principal,
        proposal: Proposal,
        expected_version: int,
        nonce: str,
        source_revision: str,
    ) -> Proposal:
        return self.approvals.claim_execution(
            proposal.proposal_id,
            principal,
            expected_version=expected_version,
            execution_nonce=nonce,
            current_source_revision=source_revision,
        )

    def _unknown(self, executing: Proposal, receipt: Mapping[str, object]) -> Proposal:
        return self.approvals.complete_execution(
            executing.proposal_id,
            ProposalStatus.UNKNOWN,
            expected_version=executing.version,
            receipt=receipt,
        )

    def _failed(self, executing: Proposal, reason: str) -> Proposal:
        return self.approvals.complete_execution(
            executing.proposal_id,
            ProposalStatus.FAILED,
            expected_version=executing.version,
            receipt={"verified": False, "reason": reason[:200]},
        )

    def _execute_sms(
        self, principal: Principal, proposal: Proposal, expected_version: int, nonce: str
    ) -> Proposal:
        # The requester's connector, not the approver's. Approving somebody's
        # message does not move it onto the approver's account.
        ghl = self._ghl_for(proposal.requester)
        contact_id = _payload_text(proposal.payload, "contact_id")
        destination = _payload_text(proposal.payload, "normalized_destination")
        body = _payload_text(proposal.payload, "body")
        contact = ghl.get_contact(contact_id)
        executing = self._claim(
            principal, proposal, expected_version, nonce, contact.source_revision
        )
        try:
            send_receipt = ghl.send_sms(contact_id, destination, body)
        except AmbiguousEffectError:
            return self._unknown(
                executing,
                {"verified": False, "reason": "ambiguous provider send; reconciliation required"},
            )
        except ProviderError as exc:
            return self._failed(executing, str(exc))
        try:
            message = ghl.get_message(
                send_receipt["conversation_id"], send_receipt["message_id"], contact_id
            )
            if message.fields.get("body") != body:
                raise ProviderError("authoritative SMS body mismatch")
        except ProviderError:
            return self._unknown(
                executing,
                {
                    "verified": False,
                    "message_id": send_receipt.get("message_id", "unknown"),
                    "reason": "authoritative conversation readback failed",
                },
            )
        return self.approvals.complete_execution(
            executing.proposal_id,
            ProposalStatus.VERIFIED,
            expected_version=executing.version,
            receipt={
                "verified": True,
                "message_id": message.source_id,
                "conversation_id": send_receipt["conversation_id"],
                "contact_id": contact_id,
            },
        )

    def _execute_announcement(
        self, principal: Principal, proposal: Proposal, expected_version: int, nonce: str
    ) -> Proposal:
        channel_id = _payload_text(proposal.payload, "channel_id")
        content = _payload_text(proposal.payload, "content")
        if channel_id not in self.config.announcement_channel_ids:
            raise ApprovalError("announcement destination is no longer configured")
        executing = self._claim(
            principal,
            proposal,
            expected_version,
            nonce,
            "configured-destination-v1",
        )
        try:
            sent = self.discord.send_message(channel_id, content)
        except AmbiguousEffectError:
            return self._unknown(executing, {"verified": False, "reason": "ambiguous Discord send"})
        except ProviderError as exc:
            return self._failed(executing, str(exc))
        try:
            observed = self.discord.get_message(channel_id, sent["message_id"])
            if observed.get("content") != content:
                raise ProviderError("Discord readback content mismatch")
        except ProviderError:
            return self._unknown(
                executing, {"verified": False, "reason": "Discord readback failed"}
            )
        return self.approvals.complete_execution(
            executing.proposal_id,
            ProposalStatus.VERIFIED,
            expected_version=executing.version,
            receipt={"verified": True, **sent},
        )

    def _execute_trello(
        self, principal: Principal, proposal: Proposal, expected_version: int, nonce: str
    ) -> Proposal:
        # The requester's connector, not the approver's. Approving somebody's
        # card does not move it onto the approver's Trello identity.
        trello = self._trello_for(proposal.requester)
        operation = proposal.payload.get("operation")
        if operation == "merge":
            return self._execute_merge(principal, proposal, expected_version, nonce)
        if operation == "create":
            executing = self._claim(
                principal, proposal, expected_version, nonce, proposal.source_revision
            )
            try:
                list_id = _payload_text(proposal.payload, "list_id")
                fields = proposal.payload.get("fields")
                if not isinstance(fields, Mapping):
                    raise ProviderError("Trello create fields are malformed")
                result = trello.create_card(list_id, fields)
            except AmbiguousEffectError:
                return self._unknown(
                    executing, {"verified": False, "reason": "ambiguous Trello create"}
                )
            except ProviderError as exc:
                return self._failed(executing, str(exc))
            return self._settle_by_readback(
                trello,
                executing,
                result.source_id,
                {**dict(fields), "idList": list_id},
                "the created card could not be read back",
            )
        card_id = _payload_text(proposal.payload, "card_id")
        current = trello.get_card(card_id)
        executing = self._claim(
            principal, proposal, expected_version, nonce, current.source_revision
        )
        try:
            if operation == "update":
                fields = proposal.payload.get("fields")
                if not isinstance(fields, Mapping):
                    raise ProviderError("Trello update fields are malformed")
                intended: dict[str, object] = dict(fields)
                trello.update_card(card_id, fields)
            elif operation == "move":
                destination = _payload_text(proposal.payload, "destination_list_id")
                intended = {"idList": destination}
                trello.move_card(card_id, destination)
            elif operation == "archive":
                intended = {"closed": True}
                trello.archive_card(card_id)
            elif operation == "unarchive":
                intended = {"closed": False}
                trello.unarchive_card(card_id)
            else:
                raise ProviderError("Trello operation is not permitted")
        except AmbiguousEffectError:
            return self._unknown(executing, {"verified": False, "reason": "ambiguous Trello write"})
        except ProviderError as exc:
            return self._failed(executing, str(exc))
        return self._settle_by_readback(
            trello, executing, card_id, intended, "the card could not be read back"
        )

    def _settle_by_readback(
        self,
        trello: TrelloPort,
        executing: Proposal,
        card_id: str,
        intended: Mapping[str, object],
        unreadable: str,
    ) -> Proposal:
        """Settle one approved Trello write on what a second read says.

        The write's own reply is Trello describing what it believes it did. It
        is not evidence that it did it: a proxy, a retry, a partial apply or a
        rule on the board can all produce a successful acknowledgement over an
        unchanged card. The merge path already read back; these did not, and an
        acknowledged no-op was recorded as `verified` -- the exact thing the
        contract says never to do.

        So the state is taken from an independent read afterwards. Anything the
        read cannot settle is `unknown`, which is a state somebody reconciles,
        rather than `failed`, which says the change is not there.
        """

        try:
            observed = trello.get_card(card_id)
        except (AmbiguousEffectError, ProviderError):
            return self._unknown(
                executing,
                {"verified": False, "resulting_card_id": card_id, "reason": unreadable},
            )
        disagreed = [
            field for field, expected in intended.items() if observed.fields.get(field) != expected
        ]
        if disagreed:
            return self._unknown(
                executing,
                {
                    "verified": False,
                    "resulting_card_id": card_id,
                    "reason": "the card does not carry the intended change",
                    "unverified_fields": sorted(disagreed),
                },
            )
        return self.approvals.complete_execution(
            executing.proposal_id,
            ProposalStatus.VERIFIED,
            expected_version=executing.version,
            receipt={
                "verified": True,
                "resulting_card_id": observed.source_id,
                "resulting_revision": observed.source_revision,
                "changed_fields": sorted(intended),
            },
        )

    def _execute_merge(
        self, principal: Principal, proposal: Proposal, expected_version: int, nonce: str
    ) -> Proposal:
        trello = self._trello_for(proposal.requester)
        source_id = _payload_text(proposal.payload, "source_card_id")
        destination_id = _payload_text(proposal.payload, "destination_card_id")
        source = trello.get_card(source_id)
        destination = trello.get_card(destination_id)
        revision = f"source={source.source_revision};destination={destination.source_revision}"
        executing = self._claim(principal, proposal, expected_version, nonce, revision)
        merged_fields = proposal.payload.get("merged_fields")
        if not isinstance(merged_fields, dict):
            return self._failed(executing, "merge payload is malformed")
        try:
            trello.update_card(destination_id, merged_fields)
        except AmbiguousEffectError:
            return self._unknown(executing, {"verified": False, "reason": "ambiguous merge update"})
        except ProviderError as exc:
            return self._failed(executing, str(exc))
        try:
            readback = trello.get_card(destination_id)
            for field, expected in merged_fields.items():
                if readback.fields.get(field) != expected:
                    raise ProviderError("merge destination readback mismatch")
        except ProviderError:
            return self._unknown(
                executing,
                {"verified": False, "reason": "destination readback failed; source not archived"},
            )
        try:
            trello.archive_card(source_id)
            archived = trello.get_card(source_id)
            if archived.fields.get("closed") is not True:
                raise ProviderError("duplicate archive readback mismatch")
        except (AmbiguousEffectError, ProviderError):
            return self._unknown(
                executing,
                {
                    "verified": False,
                    "resulting_card_id": readback.source_id,
                    "reason": "duplicate archive requires reconciliation",
                },
            )
        return self.approvals.complete_execution(
            executing.proposal_id,
            ProposalStatus.VERIFIED,
            expected_version=executing.version,
            receipt={
                "verified": True,
                "source_card_id": source_id,
                "resulting_card_id": readback.source_id,
                "resulting_revision": readback.source_revision,
            },
        )

    def analyze_property(
        self,
        asking_price: Decimal,
        estimated_value: Decimal,
        estimated_monthly_rent: Decimal,
    ) -> dict[str, str]:
        return preliminary_analysis(asking_price, estimated_value, estimated_monthly_rent)
