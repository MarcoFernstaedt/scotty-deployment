from __future__ import annotations

import json
import os
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping

from .policy import Principal, Role, can_approve


class ApprovalError(RuntimeError):
    """An approval transition was invalid or stale."""


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    EXECUTING = "executing"
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Proposal:
    proposal_id: str
    requester: Principal
    approver: Principal
    action_class: str
    target_ids: tuple[str, ...]
    payload: Mapping[str, object]
    payload_hash: str
    source_revision: str
    expires_at: datetime
    version: int
    execution_nonce: str
    status: ProposalStatus
    receipt: Mapping[str, object] | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _strict_json_value(value: object, path: str = "payload") -> None:
    if value is None or type(value) in {str, bool, int}:
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _strict_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str or not key:
                raise ApprovalError(f"{path} keys must be non-empty strings")
            _strict_json_value(item, f"{path}.{key}")
        return
    raise ApprovalError(f"{path} contains unsupported type {type(value).__name__}")


def _canonical_json(value: object) -> str:
    _strict_json_value(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _principal_json(principal: Principal) -> str:
    return _canonical_json(list(principal.as_tuple()))


def _principal_from_json(raw: str) -> Principal:
    value = json.loads(raw)
    if not isinstance(value, list) or len(value) != 4:
        raise ApprovalError("stored principal is malformed")
    if any(type(item) is not str or not item for item in value):
        raise ApprovalError("stored principal is malformed")
    try:
        role = Role(value[3])
    except ValueError as exc:
        raise ApprovalError("stored principal role is malformed") from exc
    return Principal(value[0], value[1], value[2], role)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    requester_json TEXT NOT NULL,
    approver_json TEXT NOT NULL,
    action_class TEXT NOT NULL,
    target_ids_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    execution_nonce TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN (
        'proposed','approved','denied','expired','executing','verified','failed','unknown'
    )),
    receipt_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS proposals_immutable_fields
BEFORE UPDATE OF requester_json, approver_json, action_class, target_ids_json,
                 payload_json, payload_hash, source_revision, expires_at,
                 execution_nonce, created_at
ON proposals
BEGIN
    SELECT RAISE(ABORT, 'immutable proposal field');
END;
"""


class ApprovalStore:
    def __init__(self, path: str | os.PathLike[str], *, clock: Callable[[], datetime] = _utc_now):
        self.path = Path(path)
        self.clock = clock

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        old_umask = os.umask(0o077)
        try:
            connection = self._connect()
            try:
                connection.executescript(_SCHEMA)
            finally:
                connection.close()
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)

    def _now(self) -> datetime:
        now = self.clock()
        if type(now) is not datetime or now.tzinfo is None:
            raise ApprovalError("clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    def propose(
        self,
        *,
        requester: Principal,
        approver: Principal,
        action_class: str,
        target_ids: tuple[str, ...],
        payload: Mapping[str, object],
        source_revision: str,
        expires_at: datetime,
    ) -> Proposal:
        if not can_approve(approver, action_class):
            raise ApprovalError("recorded approver is not permitted for this action")
        if type(action_class) is not str or not action_class:
            raise ApprovalError("action_class must be a non-empty string")
        if (
            not isinstance(target_ids, tuple)
            or not target_ids
            or any(type(item) is not str or not item for item in target_ids)
            or len(set(target_ids)) != len(target_ids)
        ):
            raise ApprovalError("target_ids must be unique non-empty strings")
        if not isinstance(payload, Mapping):
            raise ApprovalError("payload must be an object")
        if type(source_revision) is not str or not source_revision:
            raise ApprovalError("source_revision must be a non-empty string")
        if type(expires_at) is not datetime or expires_at.tzinfo is None:
            raise ApprovalError("expires_at must be timezone-aware")
        now = self._now()
        expiry = expires_at.astimezone(UTC)
        if expiry <= now:
            raise ApprovalError("proposal expiry must be in the future")
        payload_json = _canonical_json(dict(payload))
        target_json = _canonical_json(list(target_ids))
        import hashlib

        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        proposal_id = uuid.uuid4().hex
        nonce = secrets.token_hex(24)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO proposals (
                    proposal_id, requester_json, approver_json, action_class,
                    target_ids_json, payload_json, payload_hash, source_revision,
                    expires_at, version, execution_nonce, status, receipt_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'proposed', NULL, ?, ?)""",
                (
                    proposal_id,
                    _principal_json(requester),
                    _principal_json(approver),
                    action_class,
                    target_json,
                    payload_json,
                    payload_hash,
                    source_revision,
                    expiry.isoformat(),
                    nonce,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(proposal_id)

    def _row_to_proposal(self, row: sqlite3.Row) -> Proposal:
        try:
            target_ids = json.loads(row["target_ids_json"])
            payload = json.loads(row["payload_json"])
            receipt = json.loads(row["receipt_json"]) if row["receipt_json"] else None
            if not isinstance(target_ids, list) or any(type(item) is not str for item in target_ids):
                raise ValueError
            if not isinstance(payload, dict) or (receipt is not None and not isinstance(receipt, dict)):
                raise ValueError
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at.tzinfo is None:
                raise ValueError
            return Proposal(
                proposal_id=row["proposal_id"],
                requester=_principal_from_json(row["requester_json"]),
                approver=_principal_from_json(row["approver_json"]),
                action_class=row["action_class"],
                target_ids=tuple(target_ids),
                payload=payload,
                payload_hash=row["payload_hash"],
                source_revision=row["source_revision"],
                expires_at=expires_at.astimezone(UTC),
                version=row["version"],
                execution_nonce=row["execution_nonce"],
                status=ProposalStatus(row["status"]),
                receipt=receipt,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ApprovalError("stored proposal is malformed") from exc

    def get(self, proposal_id: str) -> Proposal:
        if type(proposal_id) is not str or not proposal_id:
            raise ApprovalError("proposal_id must be a non-empty string")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ApprovalError("proposal not found")
        return self._row_to_proposal(row)

    def _principal_matches(self, observed: Principal, expected: Principal) -> bool:
        return observed.as_tuple() == expected.as_tuple()

    def _simple_transition(
        self,
        proposal_id: str,
        actor: Principal,
        expected_version: int,
        from_status: ProposalStatus,
        to_status: ProposalStatus,
    ) -> Proposal:
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise ApprovalError("proposal not found")
            proposal = self._row_to_proposal(row)
            if not self._principal_matches(actor, proposal.approver):
                raise ApprovalError("caller does not match the exact approver tuple")
            if proposal.status != from_status or proposal.version != expected_version:
                raise ApprovalError("proposal state or version mismatch")
            if proposal.expires_at <= now and from_status in {
                ProposalStatus.PROPOSED,
                ProposalStatus.APPROVED,
            }:
                connection.execute(
                    "UPDATE proposals SET status='expired', version=version+1, updated_at=? WHERE proposal_id=?",
                    (now.isoformat(), proposal_id),
                )
                connection.commit()
                raise ApprovalError("proposal expired")
            changed = connection.execute(
                """UPDATE proposals SET status=?, version=version+1, updated_at=?
                   WHERE proposal_id=? AND status=? AND version=?""",
                (
                    to_status.value,
                    now.isoformat(),
                    proposal_id,
                    from_status.value,
                    expected_version,
                ),
            ).rowcount
            if changed != 1:
                raise ApprovalError("proposal transition lost a concurrent race")
            connection.commit()
        except ApprovalError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(proposal_id)

    def approve(self, proposal_id: str, approver: Principal, expected_version: int) -> Proposal:
        return self._simple_transition(
            proposal_id,
            approver,
            expected_version,
            ProposalStatus.PROPOSED,
            ProposalStatus.APPROVED,
        )

    def deny(self, proposal_id: str, approver: Principal, expected_version: int) -> Proposal:
        return self._simple_transition(
            proposal_id,
            approver,
            expected_version,
            ProposalStatus.PROPOSED,
            ProposalStatus.DENIED,
        )

    def claim_execution(
        self,
        proposal_id: str,
        approver: Principal,
        *,
        expected_version: int,
        execution_nonce: str,
        current_source_revision: str,
    ) -> Proposal:
        now = self._now()
        connection = self._connect()
        committed_expiry = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise ApprovalError("proposal not found")
            proposal = self._row_to_proposal(row)
            if not self._principal_matches(approver, proposal.approver):
                raise ApprovalError("caller does not match the exact approver tuple")
            if proposal.status != ProposalStatus.APPROVED or proposal.version != expected_version:
                raise ApprovalError("proposal state or version mismatch")
            if proposal.expires_at <= now:
                connection.execute(
                    "UPDATE proposals SET status='expired', version=version+1, updated_at=? WHERE proposal_id=?",
                    (now.isoformat(), proposal_id),
                )
                connection.commit()
                committed_expiry = True
                raise ApprovalError("proposal expired")
            if type(execution_nonce) is not str or not secrets.compare_digest(
                execution_nonce, proposal.execution_nonce
            ):
                raise ApprovalError("execution nonce mismatch")
            if current_source_revision != proposal.source_revision:
                raise ApprovalError("source revision changed")
            changed = connection.execute(
                """UPDATE proposals SET status='executing', version=version+1, updated_at=?
                   WHERE proposal_id=? AND status='approved' AND version=?""",
                (now.isoformat(), proposal_id, expected_version),
            ).rowcount
            if changed != 1:
                raise ApprovalError("execution claim lost a concurrent race")
            connection.commit()
        except ApprovalError:
            if connection.in_transaction and not committed_expiry:
                connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(proposal_id)

    def recover_interrupted(self) -> int:
        now = self._now().isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE proposals SET status='unknown', version=version+1, updated_at=?
                   WHERE status='executing'""",
                (now,),
            ).rowcount
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_execution(
        self,
        proposal_id: str,
        outcome: ProposalStatus,
        *,
        expected_version: int,
        receipt: Mapping[str, object],
    ) -> Proposal:
        if outcome not in {
            ProposalStatus.VERIFIED,
            ProposalStatus.FAILED,
            ProposalStatus.UNKNOWN,
        }:
            raise ApprovalError("illegal execution outcome")
        return self._terminal_transition(
            proposal_id,
            ProposalStatus.EXECUTING,
            outcome,
            expected_version,
            receipt,
        )

    def reconcile(
        self,
        proposal_id: str,
        outcome: ProposalStatus,
        *,
        expected_version: int,
        receipt: Mapping[str, object],
    ) -> Proposal:
        if outcome not in {ProposalStatus.VERIFIED, ProposalStatus.FAILED}:
            raise ApprovalError("reconciliation outcome must be verified or failed")
        return self._terminal_transition(
            proposal_id,
            ProposalStatus.UNKNOWN,
            outcome,
            expected_version,
            receipt,
        )

    def _terminal_transition(
        self,
        proposal_id: str,
        from_status: ProposalStatus,
        outcome: ProposalStatus,
        expected_version: int,
        receipt: Mapping[str, object],
    ) -> Proposal:
        if not isinstance(receipt, Mapping):
            raise ApprovalError("receipt must be an object")
        receipt_json = _canonical_json(dict(receipt))
        now = self._now().isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE proposals SET status=?, version=version+1, receipt_json=?, updated_at=?
                   WHERE proposal_id=? AND status=? AND version=?""",
                (
                    outcome.value,
                    receipt_json,
                    now,
                    proposal_id,
                    from_status.value,
                    expected_version,
                ),
            ).rowcount
            if changed != 1:
                raise ApprovalError("proposal state or version mismatch")
            connection.commit()
        except ApprovalError:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(proposal_id)
