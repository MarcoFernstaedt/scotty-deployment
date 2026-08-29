from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta

from assistant.scotty_business.approvals import (
    ApprovalError,
    ApprovalStore,
    ProposalStatus,
)
from assistant.scotty_business.policy import Principal, Role


class ApprovalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="scotty-approval-test-")
        self.db_path = os.path.join(self.tempdir.name, "approvals.db")
        self.now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
        self.requester = Principal("100", "201", "301", Role.MAIN_OPERATOR)
        self.approver = self.requester
        self.employee = Principal("100", "202", "302", Role.EMPLOYEE)
        self.store = ApprovalStore(self.db_path, clock=lambda: self.now)
        self.store.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def propose(self, *, requester: Principal | None = None, source_revision: str = "rev-1"):
        return self.store.propose(
            requester=requester or self.requester,
            approver=self.approver,
            action_class="ghl_sms",
            target_ids=("location-1", "contact-1", "+15550000001"),
            payload={"body": "Synthetic follow-up", "contact_id": "contact-1"},
            source_revision=source_revision,
            expires_at=self.now + timedelta(minutes=10),
        )

    def test_proposal_records_immutable_bound_fields_and_owner_only_mode(self) -> None:
        proposal = self.propose()
        self.assertEqual(proposal.status, ProposalStatus.PROPOSED)
        self.assertEqual(proposal.version, 1)
        self.assertEqual(proposal.requester, self.requester)
        self.assertEqual(proposal.approver, self.approver)
        self.assertEqual(len(proposal.payload_hash), 64)
        self.assertGreaterEqual(len(proposal.execution_nonce), 32)
        self.assertEqual(os.stat(self.db_path).st_mode & 0o777, 0o600)

        connection = sqlite3.connect(self.db_path)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE proposals SET payload_hash = ? WHERE proposal_id = ?",
                ("0" * 64, proposal.proposal_id),
            )
        connection.close()

    def test_only_exact_approver_tuple_can_approve(self) -> None:
        proposal = self.propose(requester=self.employee)
        wrong = Principal("100", "999", "301", Role.MAIN_OPERATOR)
        with self.assertRaises(ApprovalError):
            self.store.approve(proposal.proposal_id, wrong, expected_version=1)
        approved = self.store.approve(proposal.proposal_id, self.approver, expected_version=1)
        self.assertEqual(approved.status, ProposalStatus.APPROVED)
        self.assertEqual(approved.version, 2)

    def test_employee_cannot_be_recorded_as_approver(self) -> None:
        with self.assertRaises(ApprovalError):
            self.store.propose(
                requester=self.employee,
                approver=self.employee,
                action_class="trello_write",
                target_ids=("board-1", "card-1"),
                payload={"name": "Synthetic card"},
                source_revision="rev-1",
                expires_at=self.now + timedelta(minutes=10),
            )

    def test_expiry_and_source_drift_fail_before_execution(self) -> None:
        proposal = self.propose()
        approved = self.store.approve(proposal.proposal_id, self.approver, 1)
        with self.assertRaisesRegex(ApprovalError, "source revision"):
            self.store.claim_execution(
                approved.proposal_id,
                self.approver,
                expected_version=2,
                execution_nonce=approved.execution_nonce,
                current_source_revision="rev-2",
            )
        self.now += timedelta(minutes=11)
        with self.assertRaisesRegex(ApprovalError, "expired"):
            self.store.claim_execution(
                approved.proposal_id,
                self.approver,
                expected_version=2,
                execution_nonce=approved.execution_nonce,
                current_source_revision="rev-1",
            )
        self.assertEqual(self.store.get(approved.proposal_id).status, ProposalStatus.EXPIRED)

    def test_execution_claim_is_single_use_under_race(self) -> None:
        proposal = self.propose()
        approved = self.store.approve(proposal.proposal_id, self.approver, 1)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def claim() -> None:
            contender = ApprovalStore(self.db_path, clock=lambda: self.now)
            barrier.wait()
            try:
                contender.claim_execution(
                    approved.proposal_id,
                    self.approver,
                    expected_version=2,
                    execution_nonce=approved.execution_nonce,
                    current_source_revision="rev-1",
                )
                result = "claimed"
            except ApprovalError:
                result = "rejected"
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=claim), threading.Thread(target=claim)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, ["claimed", "rejected"])

    def test_crash_and_ambiguous_effect_never_reopen_approval(self) -> None:
        proposal = self.propose()
        approved = self.store.approve(proposal.proposal_id, self.approver, 1)
        executing = self.store.claim_execution(
            approved.proposal_id,
            self.approver,
            expected_version=2,
            execution_nonce=approved.execution_nonce,
            current_source_revision="rev-1",
        )
        self.assertEqual(executing.status, ProposalStatus.EXECUTING)
        self.assertEqual(self.store.recover_interrupted(), 1)
        unknown = self.store.get(executing.proposal_id)
        self.assertEqual(unknown.status, ProposalStatus.UNKNOWN)
        with self.assertRaises(ApprovalError):
            self.store.claim_execution(
                unknown.proposal_id,
                self.approver,
                expected_version=unknown.version,
                execution_nonce=unknown.execution_nonce,
                current_source_revision="rev-1",
            )
        verified = self.store.reconcile(
            unknown.proposal_id,
            ProposalStatus.VERIFIED,
            expected_version=unknown.version,
            receipt={"provider_id": "synthetic-message-1"},
        )
        self.assertEqual(verified.status, ProposalStatus.VERIFIED)

    def test_illegal_transitions_and_replay_are_rejected(self) -> None:
        proposal = self.propose()
        denied = self.store.deny(proposal.proposal_id, self.approver, 1)
        with self.assertRaises(ApprovalError):
            self.store.approve(denied.proposal_id, self.approver, denied.version)
        with self.assertRaises(ApprovalError):
            self.store.deny(denied.proposal_id, self.approver, denied.version)


if __name__ == "__main__":
    unittest.main()
