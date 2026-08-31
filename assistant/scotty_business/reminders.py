from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .adapters import AmbiguousEffectError, ProviderError
from .policy import Principal, Role


class ReminderError(RuntimeError):
    pass


class ReminderStatus(StrEnum):
    SCHEDULED = "scheduled"
    DISPATCHING = "dispatching"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Reminder:
    reminder_id: str
    principal: Principal
    channel_id: str
    text: str
    due_at: datetime
    status: ReminderStatus
    version: int
    attempt_nonce: str | None
    receipt: Mapping[str, object] | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _principal_json(principal: Principal) -> str:
    return json.dumps(principal.as_tuple(), separators=(",", ":"))


def _principal_from_json(raw: str) -> Principal:
    try:
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 4:
            raise ValueError
        if any(type(item) is not str or not item for item in value):
            raise ValueError
        return Principal(value[0], value[1], value[2], Role(value[3]))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ReminderError("stored reminder principal is malformed") from exc


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    reminder_id TEXT PRIMARY KEY,
    principal_json TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('scheduled','dispatching','delivered','cancelled','failed','unknown')),
    version INTEGER NOT NULL CHECK (version >= 1),
    attempt_nonce TEXT,
    receipt_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, due_at);
CREATE TRIGGER IF NOT EXISTS reminders_immutable_fields
BEFORE UPDATE OF principal_json, channel_id, text, due_at, created_at
ON reminders
BEGIN
    SELECT RAISE(ABORT, 'immutable reminder field');
END;
"""


class ReminderStore:
    def __init__(self, path: str | os.PathLike[str], *, clock: Callable[[], datetime] = _utc_now):
        self.path = Path(path)
        self.clock = clock

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _now(self) -> datetime:
        now = self.clock()
        if type(now) is not datetime or now.tzinfo is None:
            raise ReminderError("clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        old_umask = os.umask(0o077)
        try:
            connection = self._connect()
            try:
                connection.executescript(_SCHEMA)
                now = self._now().isoformat()
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """UPDATE reminders SET status='unknown', version=version+1,
                       updated_at=? WHERE status='dispatching'""",
                    (now,),
                )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)

    def create(self, principal: Principal, text: str, due_at: datetime) -> Reminder:
        if type(text) is not str or not text.strip() or len(text) > 1000:
            raise ReminderError("reminder text must contain 1-1000 characters")
        if type(due_at) is not datetime or due_at.tzinfo is None:
            raise ReminderError("reminder due time must be timezone-aware")
        now = self._now()
        due = due_at.astimezone(UTC)
        if due <= now:
            raise ReminderError("reminder due time must be in the future")
        reminder_id = uuid.uuid4().hex
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO reminders (
                    reminder_id, principal_json, channel_id, text, due_at, status,
                    version, attempt_nonce, receipt_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'scheduled', 1, NULL, NULL, ?, ?)""",
                (
                    reminder_id,
                    _principal_json(principal),
                    principal.channel_id,
                    text,
                    due.isoformat(),
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
        return self.get(reminder_id)

    def _from_row(self, row: sqlite3.Row) -> Reminder:
        try:
            due = datetime.fromisoformat(row["due_at"])
            if due.tzinfo is None:
                raise ValueError
            receipt = json.loads(row["receipt_json"]) if row["receipt_json"] else None
            if receipt is not None and not isinstance(receipt, dict):
                raise ValueError
            return Reminder(
                reminder_id=row["reminder_id"],
                principal=_principal_from_json(row["principal_json"]),
                channel_id=row["channel_id"],
                text=row["text"],
                due_at=due.astimezone(UTC),
                status=ReminderStatus(row["status"]),
                version=row["version"],
                attempt_nonce=row["attempt_nonce"],
                receipt=receipt,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReminderError("stored reminder is malformed") from exc

    def get(self, reminder_id: str) -> Reminder:
        if type(reminder_id) is not str or not reminder_id:
            raise ReminderError("reminder_id must be a non-empty string")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM reminders WHERE reminder_id=?", (reminder_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ReminderError("reminder not found")
        return self._from_row(row)

    def list_for(self, principal: Principal) -> tuple[Reminder, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT * FROM reminders WHERE principal_json=?
                   ORDER BY due_at, reminder_id""",
                (_principal_json(principal),),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._from_row(row) for row in rows)

    def cancel(self, principal: Principal, reminder_id: str) -> Reminder:
        now = self._now().isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE reminders SET status='cancelled', version=version+1, updated_at=?
                   WHERE reminder_id=? AND principal_json=? AND status='scheduled'""",
                (now, reminder_id, _principal_json(principal)),
            ).rowcount
            if changed != 1:
                raise ReminderError("reminder is unavailable to this principal")
            connection.commit()
        except ReminderError:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(reminder_id)

    def recover_interrupted(self) -> int:
        """Move reminders interrupted mid-dispatch to `unknown`, never retried."""

        now = self._now().isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE reminders SET status='unknown', version=version+1, updated_at=?
                   WHERE status='dispatching'""",
                (now,),
            ).rowcount
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_due(self, *, limit: int = 20) -> tuple[Reminder, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ReminderError("claim limit must be an integer from 1 to 100")
        now = self._now()
        connection = self._connect()
        claimed_ids: list[str] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT reminder_id, version FROM reminders
                   WHERE status='scheduled' AND due_at<=?
                   ORDER BY due_at, reminder_id LIMIT ?""",
                (now.isoformat(), limit),
            ).fetchall()
            for row in rows:
                nonce = secrets.token_hex(24)
                changed = connection.execute(
                    """UPDATE reminders SET status='dispatching', version=version+1,
                       attempt_nonce=?, updated_at=?
                       WHERE reminder_id=? AND status='scheduled' AND version=?""",
                    (nonce, now.isoformat(), row["reminder_id"], row["version"]),
                ).rowcount
                if changed == 1:
                    claimed_ids.append(row["reminder_id"])
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return tuple(self.get(reminder_id) for reminder_id in claimed_ids)

    def finish(
        self,
        reminder_id: str,
        attempt_nonce: str,
        status: ReminderStatus,
        receipt: Mapping[str, object],
    ) -> Reminder:
        if status not in {ReminderStatus.DELIVERED, ReminderStatus.FAILED, ReminderStatus.UNKNOWN}:
            raise ReminderError("illegal reminder outcome")
        if not isinstance(receipt, Mapping):
            raise ReminderError("reminder receipt must be an object")
        receipt_json = json.dumps(dict(receipt), sort_keys=True, separators=(",", ":"))
        now = self._now().isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE reminders SET status=?, version=version+1, receipt_json=?, updated_at=?
                   WHERE reminder_id=? AND status='dispatching' AND attempt_nonce=?""",
                (status.value, receipt_json, now, reminder_id, attempt_nonce),
            ).rowcount
            if changed != 1:
                raise ReminderError("reminder attempt is stale or already terminal")
            connection.commit()
        except ReminderError:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(reminder_id)


class ReminderWorker:
    """Plugin-owned reminder loop; it never registers or invokes native cron."""

    def __init__(self, store: ReminderStore, sender: Callable[[str, str], Mapping[str, object]]):
        self.store = store
        self.sender = sender

    def run_once(self) -> int:
        claimed = self.store.claim_due()
        for reminder in claimed:
            assert reminder.attempt_nonce is not None
            try:
                receipt = self.sender(reminder.channel_id, reminder.text)
                if (
                    not isinstance(receipt, Mapping)
                    or receipt.get("channel_id") != reminder.channel_id
                    or type(receipt.get("message_id")) is not str
                    or not receipt.get("message_id")
                ):
                    raise AmbiguousEffectError("delivery acknowledgement is malformed")
            except AmbiguousEffectError:
                self.store.finish(
                    reminder.reminder_id,
                    reminder.attempt_nonce,
                    ReminderStatus.UNKNOWN,
                    {"verified": False, "reason": "ambiguous reminder delivery"},
                )
            except ProviderError as exc:
                self.store.finish(
                    reminder.reminder_id,
                    reminder.attempt_nonce,
                    ReminderStatus.FAILED,
                    {"verified": False, "reason": str(exc)[:200]},
                )
            else:
                self.store.finish(
                    reminder.reminder_id,
                    reminder.attempt_nonce,
                    ReminderStatus.DELIVERED,
                    dict(receipt),
                )
        return len(claimed)

    def serve(self, stop_event: threading.Event, *, poll_seconds: float = 5.0) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(poll_seconds)
