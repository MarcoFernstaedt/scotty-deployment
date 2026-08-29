from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from .adapters import AmbiguousEffectError, ProviderError, ProviderRecord
from .approvals import ApprovalError, ApprovalStore, Proposal, ProposalStatus
from .calculations import preliminary_analysis
from .config import RuntimeConfig
from .policy import Principal, Role


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


class ScottyService:
    """Bounded business orchestration over typed provider adapters."""

    def __init__(
        self,
        config: RuntimeConfig,
        approvals: ApprovalStore,
        *,
        trello: TrelloPort,
        ghl: GHLPort,
        rentcast: RentCastPort | None,
        discord: DiscordPort,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.config = config
        self.approvals = approvals
        self.trello = trello
        self.ghl = ghl
        self.rentcast = rentcast
        self.discord = discord
        self.clock = clock

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

    def approve(self, principal: Principal, proposal_id: str, expected_version: int) -> Proposal:
        return self.approvals.approve(proposal_id, principal, expected_version)

    def deny(self, principal: Principal, proposal_id: str, expected_version: int) -> Proposal:
        return self.approvals.deny(proposal_id, principal, expected_version)

    def propose_trello_merge(
        self, requester: Principal, source_card_id: str, destination_card_id: str
    ) -> Proposal:
        if source_card_id == destination_card_id:
            raise ProviderError("merge source and destination must differ")
        source = self.trello.get_card(source_card_id)
        destination = self.trello.get_card(destination_card_id)
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

        merge_keys = ("name", "desc", "due", "dueComplete", "idLabels")
        conflicts: dict[str, dict[str, object]] = {}
        merged_fields: dict[str, object] = {}
        for field in merge_keys:
            source_value = source.fields.get(field)
            destination_value = destination.fields.get(field)
            if source_value != destination_value:
                conflicts[field] = {
                    "source": source_value,
                    "destination": destination_value,
                }
            if destination_value not in (None, "", []):
                merged_fields[field] = destination_value
            elif source_value not in (None, "", []):
                merged_fields[field] = source_value
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
            "merged_fields": merged_fields,
        }
        return self.approvals.propose(
            requester=requester,
            approver=self._approver_for(requester),
            action_class="trello_write",
            target_ids=(
                self.config.trello.board_id,
                source.source_id,
                destination.source_id,
            ),
            payload=payload,
            source_revision=(
                f"source={source.source_revision};destination={destination.source_revision}"
            ),
            expires_at=self._now() + timedelta(minutes=10),
        )

    def propose_trello_action(
        self,
        requester: Principal,
        operation: str,
        card_id: str,
        fields: Mapping[str, object] | None = None,
        destination_list_id: str | None = None,
    ) -> Proposal:
        if operation not in {"update", "move", "archive"}:
            raise ProviderError("Trello operation is not permitted")
        current = self.trello.get_card(card_id)
        payload = {
            "operation": operation,
            "card_id": current.source_id,
            "fields": dict(fields or {}),
            "destination_list_id": destination_list_id,
        }
        targets = [self.config.trello.board_id, current.source_id]
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
            target_ids=(self.config.trello.board_id, list_id),
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
        contact = self.ghl.get_contact(contact_id)
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
                self.config.ghl_location_id,
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
        return self.approvals.propose(
            requester=requester,
            approver=self._approver_for(requester),
            action_class="discord_announcement",
            target_ids=(channel_id,),
            payload={"operation": "announce", "channel_id": channel_id, "content": content},
            source_revision="configured-destination-v1",
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
        raise ApprovalError("proposal action class is unsupported")

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
        contact_id = _payload_text(proposal.payload, "contact_id")
        destination = _payload_text(proposal.payload, "normalized_destination")
        body = _payload_text(proposal.payload, "body")
        contact = self.ghl.get_contact(contact_id)
        executing = self._claim(
            principal, proposal, expected_version, nonce, contact.source_revision
        )
        try:
            send_receipt = self.ghl.send_sms(contact_id, destination, body)
        except AmbiguousEffectError:
            return self._unknown(
                executing,
                {"verified": False, "reason": "ambiguous provider send; reconciliation required"},
            )
        except ProviderError as exc:
            return self._failed(executing, str(exc))
        try:
            message = self.ghl.get_message(
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
                result = self.trello.create_card(list_id, fields)
            except AmbiguousEffectError:
                return self._unknown(
                    executing, {"verified": False, "reason": "ambiguous Trello create"}
                )
            except ProviderError as exc:
                return self._failed(executing, str(exc))
            return self.approvals.complete_execution(
                executing.proposal_id,
                ProposalStatus.VERIFIED,
                expected_version=executing.version,
                receipt={"verified": True, "resulting_card_id": result.source_id},
            )
        card_id = _payload_text(proposal.payload, "card_id")
        current = self.trello.get_card(card_id)
        executing = self._claim(
            principal, proposal, expected_version, nonce, current.source_revision
        )
        try:
            if operation == "update":
                fields = proposal.payload.get("fields")
                if not isinstance(fields, Mapping):
                    raise ProviderError("Trello update fields are malformed")
                result = self.trello.update_card(card_id, fields)
            elif operation == "move":
                result = self.trello.move_card(
                    card_id, _payload_text(proposal.payload, "destination_list_id")
                )
            elif operation == "archive":
                result = self.trello.archive_card(card_id)
            else:
                raise ProviderError("Trello operation is not permitted")
        except AmbiguousEffectError:
            return self._unknown(executing, {"verified": False, "reason": "ambiguous Trello write"})
        except ProviderError as exc:
            return self._failed(executing, str(exc))
        return self.approvals.complete_execution(
            executing.proposal_id,
            ProposalStatus.VERIFIED,
            expected_version=executing.version,
            receipt={"verified": True, "resulting_card_id": result.source_id},
        )

    def _execute_merge(
        self, principal: Principal, proposal: Proposal, expected_version: int, nonce: str
    ) -> Proposal:
        source_id = _payload_text(proposal.payload, "source_card_id")
        destination_id = _payload_text(proposal.payload, "destination_card_id")
        source = self.trello.get_card(source_id)
        destination = self.trello.get_card(destination_id)
        revision = f"source={source.source_revision};destination={destination.source_revision}"
        executing = self._claim(principal, proposal, expected_version, nonce, revision)
        merged_fields = proposal.payload.get("merged_fields")
        if not isinstance(merged_fields, dict):
            return self._failed(executing, "merge payload is malformed")
        try:
            self.trello.update_card(destination_id, merged_fields)
        except AmbiguousEffectError:
            return self._unknown(executing, {"verified": False, "reason": "ambiguous merge update"})
        except ProviderError as exc:
            return self._failed(executing, str(exc))
        try:
            readback = self.trello.get_card(destination_id)
            for field, expected in merged_fields.items():
                if readback.fields.get(field) != expected:
                    raise ProviderError("merge destination readback mismatch")
        except ProviderError:
            return self._unknown(
                executing,
                {"verified": False, "reason": "destination readback failed; source not archived"},
            )
        try:
            self.trello.archive_card(source_id)
            archived = self.trello.get_card(source_id)
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
