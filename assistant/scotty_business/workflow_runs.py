"""Running a declared workflow, and keeping a durable record of the running.

A workflow that can be saved and activated but never executed is a document.
This is the part that runs one, and the reason it is a database rather than a
loop is that the interesting cases are all about interruption: the container
dies between calling Trello and hearing back, the provider times out, someone
pauses a run halfway. A process that held its progress in memory would come
back with no idea which step was in flight, and the only safe thing it could do
is nothing.

So every run and every step is written down before it happens. The rules that
matter are about not doing something twice:

- one trigger makes one run, keyed by whatever the author said makes a run
  unique, so the same lead arriving three times is one piece of work;
- a step that finished is never claimed again;
- a step that was in flight when the process stopped comes back `unknown` and
  stops the run, because retrying an effect you cannot see is how one message
  to a seller becomes two.

Nothing here decides what a step is allowed to do. Execution goes back through
the same authorization, approvals, and per-actor provider identity that serve a
person asking directly; a workflow is a way to ask, not a way to be allowed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from .policy import Principal, Role
from .workflows import Workflow

#: How long a run may take before it stops rather than carrying on. A workflow
#: still going a day after its trigger is not working, and the effects it would
#: produce are no longer the ones anybody asked for.
DEFAULT_DEADLINE = timedelta(hours=24)

#: An upper bound on what a definition may ask for, so a workflow cannot hold a
#: run open indefinitely by declaring a very long one.
MAX_DEADLINE = timedelta(days=7)

MAX_DETAIL_CHARS = 500


class RunError(RuntimeError):
    """A run cannot proceed, so nothing is executed and nothing is recorded."""


class RunState(StrEnum):
    """Where a run is. The three terminal states are the honest ones."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: Something was in flight and we cannot say whether it happened.
    UNKNOWN = "unknown"


class StepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    FAILED = "failed"
    UNKNOWN = "unknown"


_TERMINAL_RUNS = frozenset(
    {RunState.CANCELLED, RunState.SUCCEEDED, RunState.FAILED, RunState.UNKNOWN}
)

_SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    owner TEXT NOT NULL,
    actor TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    attempts_allowed INTEGER NOT NULL,
    stop_rule TEXT NOT NULL,
    trigger_hash TEXT NOT NULL,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_steps (
    run_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    operation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    approval_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, step_index)
);
CREATE INDEX IF NOT EXISTS runs_by_owner ON runs (owner, started_at);
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), default=str)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RunStep:
    """One step of one run, with the payload frozen when the run started."""

    index: int
    operation: str
    arguments: Mapping[str, object]
    payload_hash: str
    state: StepState
    attempts: int = 0
    detail: str = ""
    approval_id: str = ""

    def preview(self) -> dict[str, object]:
        """What happened, without the argument values.

        A run summary is read in a chat channel. The operation and the outcome
        are what somebody needs; a phone number in the arguments is not.
        """

        return {
            "step": self.index,
            "operation": self.operation,
            "state": self.state.value,
            "attempts": self.attempts,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Run:
    """One execution of one workflow, as it stands on disk."""

    run_id: str
    workflow_id: str
    workflow_version: int
    owner: Role
    state: RunState
    steps: tuple[RunStep, ...]
    started_at: datetime
    deadline_at: datetime
    attempts_allowed: int
    stop_rule: str
    reason: str = ""

    def preview(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "state": self.state.value,
            "reason": self.reason,
            "started_at": self.started_at.isoformat(),
            "deadline_at": self.deadline_at.isoformat(),
            "steps": [step.preview() for step in self.steps],
        }


def idempotency_key(workflow: Workflow, trigger: Mapping[str, object]) -> str:
    """What makes this run the same run, or a different one.

    The author names the field: "lead_id", or "card_id+list_id". A trigger that
    does not carry it is refused rather than run, because without it there is no
    way to tell a repeat from something new, and guessing wrong means doing the
    work twice.
    """

    named = [part.strip() for part in str(workflow.idempotency.get("key", "")).split("+")]
    parts: list[str] = []
    for field in named:
        if not field or field not in trigger:
            raise RunError(
                f"this trigger does not carry {field or 'an identifier'}, so a repeat "
                "of it could not be told from new work"
            )
        parts.append(f"{field}={trigger[field]!r}")
    return _digest(f"{workflow.workflow_id}:{workflow.version}:" + "|".join(parts))


def due_trigger(workflow: Workflow, now: datetime) -> dict[str, object] | None:
    """The trigger for the window this workflow is in, or nothing to do.

    A window rather than a timestamp is what makes a schedule safe to fire from
    a loop: every pass inside one window produces the same trigger, so the
    ledger recognises it as the run it already started rather than a new one.
    """

    if str(workflow.trigger.get("kind")) != "schedule":
        return None
    every = workflow.trigger.get("every_minutes")
    if type(every) is not int or every <= 0:
        return None
    moment = now.astimezone(UTC)
    quiet = workflow.schedule.get("quiet_hours")
    if isinstance(quiet, list | tuple) and len(quiet) == 2:
        start, end = quiet
        if type(start) is int and type(end) is int:
            hour = moment.hour
            inside = start <= hour or hour < end if start > end else start <= hour < end
            if inside:
                # Quiet hours are not a window at all, so nothing is owed for
                # them later either: the schedule resumes, it does not catch up.
                return None
    minutes = int(moment.timestamp() // 60)
    window = datetime.fromtimestamp((minutes - minutes % every) * 60, UTC)
    return {"window": window.isoformat()}


def _attempts(workflow: Workflow) -> int:
    """How many tries a step gets, as the definition declared it."""

    raw = workflow.retries.get("attempts", 0)
    return raw if type(raw) is int else 0


def _deadline(workflow: Workflow, started: datetime) -> datetime:
    raw = workflow.schedule.get("deadline_seconds")
    if type(raw) is not int or raw <= 0:
        return started + DEFAULT_DEADLINE
    return started + min(timedelta(seconds=raw), MAX_DEADLINE)


class RunLedger:
    """Every run this deployment started, and what became of each step.

    Ownership is checked on every read and every change, so one client user's
    runs are invisible to the other rather than merely unlisted.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        owner_uid: int | None = 10000,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.path = Path(path)
        self.owner_uid = owner_uid
        self.clock = clock

    # -- plumbing --------------------------------------------------------

    def _now(self) -> datetime:
        moment = self.clock()
        if moment.tzinfo is None:
            raise RunError("the clock must return a timezone-aware moment")
        return moment.astimezone(UTC)

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise RunError("the run ledger path is unsafe")
        connection = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript(_SCHEMA)
        finally:
            connection.close()
        # The ledger names what a client user asked for. Nobody but the runtime
        # account has any business reading it.
        os.chmod(self.path, 0o600)
        if self.owner_uid is not None:
            # Not being privileged enough to change it is not a reason to
            # refuse to run: the mode above still holds either way.
            with suppress(OSError, AttributeError):
                os.chown(self.path, self.owner_uid, self.owner_uid)

    # -- reading ---------------------------------------------------------

    def _steps(self, connection: sqlite3.Connection, run_id: str) -> tuple[RunStep, ...]:
        rows = connection.execute(
            "SELECT * FROM run_steps WHERE run_id = ? ORDER BY step_index", (run_id,)
        ).fetchall()
        return tuple(
            RunStep(
                index=int(row["step_index"]),
                operation=str(row["operation"]),
                arguments=json.loads(row["payload_json"]),
                payload_hash=str(row["payload_hash"]),
                state=StepState(str(row["state"])),
                attempts=int(row["attempts"]),
                detail=str(row["detail"]),
                approval_id=str(row["approval_id"]),
            )
            for row in rows
        )

    def _run(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Run:
        return Run(
            run_id=str(row["run_id"]),
            workflow_id=str(row["workflow_id"]),
            workflow_version=int(row["workflow_version"]),
            owner=Role(str(row["owner"])),
            state=RunState(str(row["state"])),
            steps=self._steps(connection, str(row["run_id"])),
            started_at=datetime.fromisoformat(str(row["started_at"])),
            deadline_at=datetime.fromisoformat(str(row["deadline_at"])),
            attempts_allowed=int(row["attempts_allowed"]),
            stop_rule=str(row["stop_rule"]),
            reason=str(row["reason"]),
        )

    def get(self, run_id: str, owner: Role) -> Run:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None or str(row["owner"]) != owner.value:
                # Same answer either way: whether somebody else's run exists is
                # not something to disclose by the shape of the error.
                raise RunError("that is not one of your runs")
            return self._run(connection, row)
        finally:
            connection.close()

    def list(self, owner: Role, *, limit: int = 25) -> tuple[Run, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM runs WHERE owner = ? ORDER BY started_at DESC LIMIT ?",
                (owner.value, max(1, min(limit, 100))),
            ).fetchall()
            return tuple(self._run(connection, row) for row in rows)
        finally:
            connection.close()

    def open_runs(self, owner: Role, *, limit: int = 50) -> tuple[Run, ...]:
        """Runs that still have somewhere to go, oldest first.

        Separate from `list` because that one is a recent-activity view: a run
        stuck behind twenty-five newer ones would never be carried forward by a
        pass that only looked at the newest page.
        """

        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT * FROM runs WHERE owner = ? AND state IN (?, ?)
                   ORDER BY started_at LIMIT ?""",
                (
                    owner.value,
                    RunState.PENDING.value,
                    RunState.RUNNING.value,
                    max(1, min(limit, 200)),
                ),
            ).fetchall()
            return tuple(self._run(connection, row) for row in rows)
        finally:
            connection.close()

    # -- starting --------------------------------------------------------

    def find(self, workflow: Workflow, trigger: Mapping[str, object]) -> Run | None:
        """The run this trigger already made, if it made one.

        `start` is idempotent and hands back the existing run, which is right
        for a caller acting on a trigger. A supervision pass needs to know the
        difference, so that "already started for this window" is not counted
        and reported as having started something.
        """

        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM runs WHERE idempotency_key = ?",
                (idempotency_key(workflow, trigger),),
            ).fetchone()
            return None if row is None else self._run(connection, row)
        finally:
            connection.close()

    def start(
        self,
        workflow: Workflow,
        actor: Principal,
        trigger: Mapping[str, object],
    ) -> Run:
        """Record a run and its steps, or hand back the run this already is."""

        if actor.role is not workflow.owner:
            raise RunError("that is not one of your workflows")
        key = idempotency_key(workflow, trigger)
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM runs WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._run(connection, existing)
            allowed = int(workflow.limits.get("runs_per_day", 0))
            since = (now - timedelta(days=1)).isoformat()
            today = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE workflow_id = ? AND started_at >= ?",
                (workflow.workflow_id, since),
            ).fetchone()[0]
            if today >= allowed:
                raise RunError(
                    f"this workflow has already run {today} times today, which is its own limit"
                )
            run_id = uuid.uuid4().hex
            deadline = _deadline(workflow, now)
            connection.execute(
                """INSERT INTO runs (
                    run_id, workflow_id, workflow_version, owner, actor, state, reason,
                    idempotency_key, attempts_allowed, stop_rule, trigger_hash,
                    started_at, updated_at, deadline_at
                ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    workflow.workflow_id,
                    workflow.version,
                    workflow.owner.value,
                    actor.user_id,
                    RunState.PENDING.value,
                    key,
                    _attempts(workflow) + 1,
                    str(workflow.retries.get("stop_rule", "on_unknown")),
                    _digest(_canonical(trigger)),
                    now.isoformat(),
                    now.isoformat(),
                    deadline.isoformat(),
                ),
            )
            for index, step in enumerate(workflow.steps):
                payload = _canonical(step.arguments)
                connection.execute(
                    """INSERT INTO run_steps (
                        run_id, step_index, operation, payload_json, payload_hash,
                        state, attempts, detail, approval_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, '', '', ?)""",
                    (
                        run_id,
                        index,
                        step.operation,
                        payload,
                        # The payload is hashed with its position, so two
                        # identical steps in one workflow are still two steps
                        # and an approval for one is not an approval for both.
                        _digest(f"{run_id}:{index}:{payload}"),
                        StepState.PENDING.value,
                        now.isoformat(),
                    ),
                )
            connection.commit()
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return self._run(connection, row)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    # -- executing -------------------------------------------------------

    def claim(self, run_id: str) -> RunStep | None:
        """Take the next step of this run, or say why there is not one.

        Claiming is the write that makes a step this process's to do. It is one
        transaction, so two processes cannot both come away holding it, and it
        refuses on a run that is paused, cancelled, finished, or past its
        deadline rather than leaving that to the caller to remember.
        """

        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunError("that run is not in the ledger")
            state = RunState(str(row["state"]))
            if state in _TERMINAL_RUNS or state is RunState.PAUSED:
                connection.commit()
                return None
            if now >= datetime.fromisoformat(str(row["deadline_at"])):
                connection.execute(
                    "UPDATE runs SET state=?, reason=?, updated_at=? WHERE run_id=?",
                    (
                        RunState.FAILED.value,
                        "this run passed its deadline before it finished",
                        now.isoformat(),
                        run_id,
                    ),
                )
                connection.commit()
                return None
            in_flight = connection.execute(
                """SELECT COUNT(*) FROM run_steps WHERE run_id = ? AND state IN (?, ?)""",
                (run_id, StepState.RUNNING.value, StepState.AWAITING_APPROVAL.value),
            ).fetchone()[0]
            if in_flight:
                # Steps are sequential and one at a time. Handing out the next
                # one while the last is still in flight would let a workflow
                # act on a card it has not been told exists yet.
                connection.commit()
                return None
            step = connection.execute(
                """SELECT * FROM run_steps WHERE run_id = ? AND state = ?
                   ORDER BY step_index LIMIT 1""",
                (run_id, StepState.PENDING.value),
            ).fetchone()
            if step is None:
                connection.commit()
                return None
            attempts = int(step["attempts"]) + 1
            connection.execute(
                """UPDATE run_steps SET state=?, attempts=?, updated_at=?
                   WHERE run_id=? AND step_index=? AND state=?""",
                (
                    StepState.RUNNING.value,
                    attempts,
                    now.isoformat(),
                    run_id,
                    int(step["step_index"]),
                    StepState.PENDING.value,
                ),
            )
            connection.execute(
                "UPDATE runs SET state=?, updated_at=? WHERE run_id=?",
                (RunState.RUNNING.value, now.isoformat(), run_id),
            )
            connection.commit()
            return RunStep(
                index=int(step["step_index"]),
                operation=str(step["operation"]),
                arguments=json.loads(step["payload_json"]),
                payload_hash=str(step["payload_hash"]),
                state=StepState.RUNNING,
                attempts=attempts,
                detail=str(step["detail"]),
                approval_id=str(step["approval_id"]),
            )
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def record(
        self,
        run_id: str,
        index: int,
        state: StepState,
        *,
        detail: str = "",
        approval_id: str | None = None,
    ) -> None:
        """Write down what became of one step.

        Recording `pending` again is a retry: the attempt is already counted, so
        a step that keeps failing runs out of attempts rather than forever.
        """

        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            fields = [state.value, detail[:MAX_DETAIL_CHARS], now.isoformat()]
            statement = "UPDATE run_steps SET state=?, detail=?, updated_at=?"
            if approval_id is not None:
                statement += ", approval_id=?"
                fields.append(approval_id)
            statement += " WHERE run_id=? AND step_index=?"
            fields.extend([run_id, str(index)])
            changed = connection.execute(statement, tuple(fields)).rowcount
            if changed != 1:
                raise RunError("that step is not part of this run")
            if state is StepState.UNKNOWN:
                # An effect nobody can see is the one case that stops the run
                # rather than moving on: the next step may depend on it, and
                # retrying it could do it twice.
                connection.execute(
                    "UPDATE runs SET state=?, reason=?, updated_at=? WHERE run_id=?",
                    (
                        RunState.UNKNOWN.value,
                        f"step {index} left an effect we cannot see; reconcile before retrying",
                        now.isoformat(),
                        run_id,
                    ),
                )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def wait_for_approval(self, run_id: str, index: int, approval_id: str) -> None:
        """Park the run on an approval bound to this exact step payload."""

        self.record(run_id, index, StepState.AWAITING_APPROVAL, approval_id=approval_id)
        self._set_state(
            run_id,
            RunState.WAITING_APPROVAL,
            f"step {index} needs an approval before it can happen",
        )

    def finish(self, run_id: str, state: RunState, reason: str = "") -> None:
        if state not in _TERMINAL_RUNS:
            raise RunError("a run finishes in a terminal state or not at all")
        self._set_state(run_id, state, reason)

    def _set_state(self, run_id: str, state: RunState, reason: str) -> None:
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE runs SET state=?, reason=?, updated_at=? WHERE run_id=?",
                (state.value, reason[:MAX_DETAIL_CHARS], now.isoformat(), run_id),
            ).rowcount
            if changed != 1:
                raise RunError("that run is not in the ledger")
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    # -- the owner's own controls ---------------------------------------

    def _owned(self, run_id: str, owner: Role) -> Run:
        return self.get(run_id, owner)

    def pause(self, run_id: str, owner: Role) -> Run:
        run = self._owned(run_id, owner)
        if run.state in _TERMINAL_RUNS:
            raise RunError("that run has already finished")
        self._set_state(run_id, RunState.PAUSED, "paused by its owner")
        return self.get(run_id, owner)

    def resume(self, run_id: str, owner: Role) -> Run:
        run = self._owned(run_id, owner)
        if run.state is not RunState.PAUSED:
            # A cancelled or unknown run is not resumed: one needs a new
            # trigger, the other needs a person to establish what happened.
            raise RunError("only a paused run can be resumed")
        self._set_state(run_id, RunState.PENDING, "")
        return self.get(run_id, owner)

    def cancel(self, run_id: str, owner: Role, reason: str = "") -> Run:
        run = self._owned(run_id, owner)
        if run.state in _TERMINAL_RUNS:
            raise RunError("that run has already finished")
        self._set_state(run_id, RunState.CANCELLED, reason or "cancelled by its owner")
        return self.get(run_id, owner)

    # -- coming back after an interruption -------------------------------

    def recover_interrupted(self) -> int:
        """Every step that was in flight when we stopped becomes `unknown`.

        This runs at startup. A step recorded as running is one nobody watched
        finish: the card may exist, the message may have gone. Marking it
        unknown stops the run and hands it to a person, which is the only
        answer that cannot double an effect.
        """

        now = self._now().isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            interrupted = [
                str(row["run_id"])
                for row in connection.execute(
                    "SELECT DISTINCT run_id FROM run_steps WHERE state = ?",
                    (StepState.RUNNING.value,),
                ).fetchall()
            ]
            changed = connection.execute(
                "UPDATE run_steps SET state=?, detail=?, updated_at=? WHERE state=?",
                (
                    StepState.UNKNOWN.value,
                    "this step was in flight when the assistant stopped",
                    now,
                    StepState.RUNNING.value,
                ),
            ).rowcount
            for run_id in interrupted:
                connection.execute(
                    "UPDATE runs SET state=?, reason=?, updated_at=? WHERE run_id=?",
                    (
                        RunState.UNKNOWN.value,
                        "a step was in flight when the assistant stopped; reconcile before retrying",
                        now,
                        run_id,
                    ),
                )
            connection.commit()
            return changed
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def prune(self, *, retention_days: int = 90) -> int:
        """Drop runs older than the retention the workflows declare."""

        cutoff = (self._now() - timedelta(days=max(1, retention_days))).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            stale = [
                str(row["run_id"])
                for row in connection.execute(
                    "SELECT run_id FROM runs WHERE started_at < ? AND state NOT IN (?, ?, ?)",
                    (
                        cutoff,
                        RunState.PENDING.value,
                        RunState.RUNNING.value,
                        RunState.WAITING_APPROVAL.value,
                    ),
                ).fetchall()
            ]
            for run_id in stale:
                connection.execute("DELETE FROM run_steps WHERE run_id = ?", (run_id,))
                connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            connection.commit()
            return len(stale)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()


class StepOutcome(NamedTuple):
    """What a dispatch made of one step.

    `AWAITING_APPROVAL` carries the proposal it raised: a consequence step is
    not executed by a workflow at all, it is proposed, and a person with the
    authority approves and executes it through the ordinary approval path.
    """

    state: StepState
    detail: str = ""
    approval_id: str = ""


#: How a step is actually carried out. The runner knows nothing about providers,
#: authorization, or approvals; it hands the operation and the frozen arguments
#: to the same dispatch that serves a person asking directly.
Dispatch = Callable[[Principal, str, Mapping[str, object]], StepOutcome]


class Runner:
    """Carries a run forward as far as it can honestly go in one pass.

    Every pass ends in a state somebody can read: succeeded, failed, waiting on
    an approval, paused, or unknown. It never ends with work half-done and no
    record of it, because the ledger is written before and after every step.
    """

    def __init__(self, ledger: RunLedger, dispatch: Dispatch) -> None:
        self.ledger = ledger
        self.dispatch = dispatch

    def advance(self, run_id: str, workflow: Workflow, actor: Principal) -> Run:
        """Run steps until something says stop, then say what state it is in."""

        if actor.role is not workflow.owner:
            raise RunError("that is not one of your workflows")
        run = self.ledger.get(run_id, actor.role)
        if run.workflow_id != workflow.workflow_id:
            raise RunError("that run belongs to a different workflow")
        while True:
            step = self.ledger.claim(run_id)
            if step is None:
                return self._settle(run_id, actor)
            try:
                outcome = self.dispatch(actor, step.operation, step.arguments)
            except Exception as exc:  # noqa: BLE001 - every failure is recorded
                # A dispatch that raised may or may not have reached a provider.
                # Treating that as unknown rather than failed is the difference
                # between stopping and sending a second message.
                self.ledger.record(run_id, step.index, StepState.UNKNOWN, detail=str(exc)[:200])
                return self.ledger.get(run_id, actor.role)
            if outcome.state is StepState.AWAITING_APPROVAL:
                self.ledger.wait_for_approval(run_id, step.index, outcome.approval_id)
                return self.ledger.get(run_id, actor.role)
            if outcome.state is StepState.UNKNOWN:
                self.ledger.record(run_id, step.index, StepState.UNKNOWN, detail=outcome.detail)
                return self.ledger.get(run_id, actor.role)
            if outcome.state is StepState.FAILED:
                if step.attempts < run.attempts_allowed and run.stop_rule != "on_failure":
                    # Back to pending: the attempt is already counted, so this
                    # runs out rather than going round forever.
                    self.ledger.record(run_id, step.index, StepState.PENDING, detail=outcome.detail)
                    continue
                self.ledger.record(run_id, step.index, StepState.FAILED, detail=outcome.detail)
                self.ledger.finish(run_id, RunState.FAILED, f"step {step.index} did not succeed")
                return self.ledger.get(run_id, actor.role)
            self.ledger.record(run_id, step.index, StepState.DONE, detail=outcome.detail)

    def _settle(self, run_id: str, actor: Principal) -> Run:
        """Nothing left to claim: say why, without inventing an outcome."""

        run = self.ledger.get(run_id, actor.role)
        if run.state in _TERMINAL_RUNS or run.state in {
            RunState.PAUSED,
            RunState.WAITING_APPROVAL,
        }:
            return run
        if all(step.state is StepState.DONE for step in run.steps):
            self.ledger.finish(run_id, RunState.SUCCEEDED, "every step is done")
            return self.ledger.get(run_id, actor.role)
        return run


__all__ = [
    "DEFAULT_DEADLINE",
    "Dispatch",
    "MAX_DEADLINE",
    "Run",
    "RunError",
    "RunLedger",
    "RunState",
    "RunStep",
    "Runner",
    "due_trigger",
    "StepOutcome",
    "StepState",
    "idempotency_key",
]
