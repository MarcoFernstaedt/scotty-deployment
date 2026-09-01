"""Exactly one process consumes Discord, proven under contention.

Two consumers on one bot token double every message and every effect. The lease
that was supposed to prevent that read the file, decided, and then wrote — three
steps with nothing holding them together, so two processes starting together
both read "unheld", both decided yes, and both wrote. A probe won that race two
hundred times out of two hundred.

What replaces it is one atomic operation the kernel arbitrates: an exclusive
create, or nothing. And every lease carries a fencing generation that only ever
increases, so a process that was paused long enough to lose its lease can be
told it is stale even if it wakes up believing otherwise.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from assistant.scotty_business.supervisor import LEASE_SECONDS, ConsumerLease

MOMENT = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


class LeaseFixture(unittest.TestCase):
    def lease(self, **kwargs) -> ConsumerLease:
        directory = tempfile.TemporaryDirectory(prefix="scotty-lease-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "consumer.lease"
        return ConsumerLease(self.path, **kwargs)


class ContentionTests(LeaseFixture):
    def test_two_processes_starting_together_do_not_both_win(self) -> None:
        """The race the old lease lost, run enough times to be sure."""

        for _ in range(50):
            results = self.race(self.lease(), 2)
            self.assertEqual(sum(results), 1, results)

    @staticmethod
    def race(lease: ConsumerLease, contenders: int) -> list[bool]:
        """Start that many claimants at the same instant, and see who wins."""

        start = threading.Barrier(contenders)
        results: list[bool] = []
        guard = threading.Lock()

        def contend(name: str) -> None:
            start.wait(timeout=5)
            won = lease.claim(name)
            with guard:
                results.append(won)

        threads = [
            threading.Thread(target=contend, args=(f"p{index}",)) for index in range(contenders)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        return results

    def test_many_contenders_produce_exactly_one_holder(self) -> None:
        lease = self.lease()
        self.assertEqual(sum(self.race(lease, 8)), 1)
        self.assertTrue(lease.holder())


class RenewalTests(LeaseFixture):
    def test_the_holder_renews_and_nobody_else_takes_it(self) -> None:
        lease = self.lease()
        self.assertTrue(lease.claim("holder", at=MOMENT))
        self.assertTrue(lease.claim("holder", at=MOMENT + timedelta(seconds=30)))
        self.assertFalse(lease.claim("other", at=MOMENT + timedelta(seconds=31)))

    def test_a_lease_nobody_renewed_is_taken_after_its_lifetime(self) -> None:
        lease = self.lease()
        self.assertTrue(lease.claim("gone", at=MOMENT))
        expired = MOMENT + timedelta(seconds=LEASE_SECONDS + 1)
        self.assertTrue(lease.claim("next", at=expired))
        self.assertEqual(lease.holder(), "next")

    def test_only_the_holder_releases_it(self) -> None:
        lease = self.lease()
        lease.claim("holder", at=MOMENT)
        self.assertFalse(lease.release("other"))
        self.assertEqual(lease.holder(), "holder")
        self.assertTrue(lease.release("holder"))
        self.assertEqual(lease.holder(), "")


class FencingTests(LeaseFixture):
    def test_the_generation_only_ever_increases(self) -> None:
        lease = self.lease()
        lease.claim("first", at=MOMENT)
        first = lease.generation()
        expired = MOMENT + timedelta(seconds=LEASE_SECONDS + 1)
        lease.claim("second", at=expired)
        self.assertGreater(lease.generation(), first)

    def test_a_holder_that_lost_its_lease_learns_it_is_stale(self) -> None:
        """A process paused past its lease must not act as though it still holds one."""

        lease = self.lease()
        lease.claim("first", at=MOMENT)
        held = lease.generation()
        expired = MOMENT + timedelta(seconds=LEASE_SECONDS + 1)
        lease.claim("second", at=expired)
        # The first process wakes up still believing it is the consumer.
        self.assertFalse(lease.still_held("first", held, at=expired))
        self.assertTrue(lease.still_held("second", lease.generation(), at=expired))

    def test_a_renewal_keeps_the_generation_so_a_holder_stays_valid(self) -> None:
        lease = self.lease()
        lease.claim("holder", at=MOMENT)
        generation = lease.generation()
        lease.claim("holder", at=MOMENT + timedelta(seconds=10))
        self.assertEqual(lease.generation(), generation)
        self.assertTrue(lease.still_held("holder", generation, at=MOMENT + timedelta(seconds=11)))


class CorruptionTests(LeaseFixture):
    def test_an_unreadable_lease_is_not_an_unheld_one(self) -> None:
        lease = self.lease()
        lease.claim("holder", at=MOMENT)
        self.path.write_text("{ this is not json", encoding="utf-8")
        # Not readable is not the same as not held, so it is not simply taken.
        self.assertFalse(lease.claim("other", at=MOMENT + timedelta(seconds=1)))
        self.assertEqual(lease.holder(), "unreadable")

    def test_an_unreadable_lease_older_than_its_lifetime_is_recoverable(self) -> None:
        import os

        lease = self.lease()
        lease.claim("holder", at=MOMENT)
        self.path.write_text("{ this is not json", encoding="utf-8")
        stale = MOMENT.timestamp() - (LEASE_SECONDS + 60)
        os.utime(self.path, (stale, stale))
        # Otherwise a corrupt byte would lock the deployment out until somebody
        # deleted a file by hand.
        self.assertTrue(lease.claim("other", at=MOMENT))


if __name__ == "__main__":
    unittest.main()
