"""Deciding whether to start the container again, and when to stop trying.

Nothing inside a container can restart the container it is part of, so this runs
on the host as root. Docker's own restart policy is deliberately off, because a
restart policy cannot tell a transient death from a crash loop, cannot decide
that restarting has stopped helping, and cannot hand the problem to a person.

The decisions are all here and all deterministic: start a container that has
died, wait out a backoff rather than hammering, give up once restarting is
plainly not recovering anything, refuse to start into a failed integrity check
at all, and honour an operator's hold. One incident is reported when it gives
up, and one recovery when the container is healthy again — not one per look.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

#: More restart attempts than this inside the window below is a crash loop.
MAX_RESTARTS = 5
RESTART_WINDOW = timedelta(minutes=10)

#: How long to leave the container alone after starting it, so a container that
#: dies immediately is not restarted several times a second.
BACKOFF = timedelta(seconds=30)

#: What to hand a person when restarting has stopped helping. Starting it once
#: more is what the supervisor has already tried five times; the step that is
#: actually left is going back to a release somebody accepted.
OPERATOR_RECOVERY_STEP = "sudo /usr/local/sbin/scotty-supervisor rollback"

#: The fixed vocabulary a health check answers in. `unknown` is never reported
#: as healthy, and a check that fails is unknown rather than fine.
HEALTHY = "healthy"
DEGRADED = "degraded"
BLOCKED = "blocked"
UNKNOWN = "unknown"
HEALTH_STATES = frozenset({HEALTHY, DEGRADED, BLOCKED, UNKNOWN})


class Container(Protocol):
    """The exact managed container, and nothing else on the host."""

    def is_running(self) -> bool: ...
    def present(self) -> bool: ...
    def start(self) -> bool: ...
    def stop(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class Decision:
    """What the supervisor did on one pass, and why."""

    action: str
    reason: str = ""

    def as_json(self) -> dict[str, object]:
        return {"action": self.action, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class SupervisorState:
    """What earlier passes established. Survives a supervisor restart."""

    restarts: tuple[str, ...] = ()
    incident_open: bool = False
    hold_reason: str = ""
    last_start: str = ""
    #: The file exists and could not be understood. Never written; only ever
    #: the answer to a read, and always a reason to stop rather than proceed.
    unreadable: bool = False

    def as_json(self) -> dict[str, object]:
        return {
            "restarts": list(self.restarts),
            "incident_open": self.incident_open,
            "hold_reason": self.hold_reason,
            "last_start": self.last_start,
        }


Alert = Callable[[str, str], None]
Integrity = Callable[[], bool]
Activated = Callable[[], bool]

#: What a health check answers: one word from the fixed vocabulary, and why.
#: `unknown` is never reported as healthy.
Health = Callable[[], tuple[str, str]]


def _moment(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class Supervisor:
    """One managed container, watched from outside it."""

    def __init__(
        self,
        container: Container,
        state_dir: Path,
        *,
        alert: Alert,
        integrity: Integrity | None = None,
        activated: Activated | None = None,
        health: Health | None = None,
    ) -> None:
        self.container = container
        self.path = state_dir / "supervisor.json"
        self.alert = alert
        # Answers "is this release still the one we accepted?". A false answer
        # is never restarted into: that would be looping on a broken release.
        self.integrity = integrity
        # Answers "has anybody accepted this deployment into service?". A
        # reboot surfaces a stopped container whether or not setup was ever
        # finished, and starting that one is how a half-installed deployment
        # goes live without anybody deciding it should.
        self.activated = activated
        # Answers "is it actually working?", which is not the same question as
        # "is the process up". Absent one, a running container is reported as
        # running and nothing stronger is claimed.
        self.health = health

    # -- state -----------------------------------------------------------

    def state(self) -> SupervisorState:
        """Read what earlier passes recorded, trusting nothing malformed.

        A file that is simply absent is a deployment that has not been
        supervised yet, which is empty history. A file that is present and
        unreadable is different, and used to be treated the same: the restart
        count started again from zero, so a corrupt byte handed the supervisor
        a fresh budget to keep restarting a container that was crash-looping.
        That case is now `unreadable`, and the pass blocks on it.
        """

        if self.path.is_symlink() or not self.path.is_file():
            return SupervisorState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return SupervisorState(unreadable=True)
        if not isinstance(raw, Mapping):
            return SupervisorState(unreadable=True)
        restarts = raw.get("restarts")
        return SupervisorState(
            restarts=tuple(item for item in restarts if type(item) is str)
            if isinstance(restarts, list)
            else (),
            incident_open=raw.get("incident_open") is True,
            hold_reason=str(raw.get("hold_reason", "")),
            last_start=str(raw.get("last_start", "")),
        )

    def _write(self, state: SupervisorState) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(json.dumps(state.as_json(), sort_keys=True).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    # -- operator control ------------------------------------------------

    def hold(self, reason: str) -> None:
        """Stop supervising until someone lifts it. The operator's own switch."""

        self._write(replace_hold(self.state(), reason or "held by the operator"))

    def release(self) -> None:
        self._write(replace_hold(self.state(), ""))

    # -- the decision ----------------------------------------------------

    def _recent(self, state: SupervisorState, now: datetime) -> list[datetime]:
        stamps = [_moment(item) for item in state.restarts]
        return [item for item in stamps if item is not None and now - item < RESTART_WINDOW]

    def tick(self, *, at: datetime | None = None) -> Decision:
        """One pass. Everything it may do is one of the actions below."""

        now = (at or datetime.now(UTC)).astimezone(UTC)
        state = self.state()

        if state.unreadable:
            # Not empty history: history nobody can read. Proceeding would hand
            # out a restart budget that may already be spent.
            self._open_incident(
                state,
                "the supervisor's own state file cannot be read, so nothing was started.",
            )
            return Decision("blocked", "the supervision state cannot be read")

        if state.hold_reason:
            return Decision("held", state.hold_reason)

        if self.container.is_running():
            verdict, detail = self._health()
            if verdict != HEALTHY:
                # Running is not working. A gateway that never connected, a
                # database that will not open, a lease nobody holds: the
                # process is up and the deployment is not serving anybody.
                self._open_incident(state, f"the assistant is running but {verdict}: {detail}")
                return Decision(verdict, detail)
            if state.restarts or state.incident_open:
                # It went down and came back. That is worth exactly one line:
                # the history is cleared, so a healthy container stays quiet.
                self.alert("recovery", "the assistant is running again.")
                self._write(SupervisorState())
            return Decision("none", "the container is running")

        if not self.container.present():
            # There is nothing to start. Creating one is an install decision,
            # not a supervision decision.
            return Decision("absent", "the managed container is not installed")

        if self.activated is not None and not self.activated():
            # A stopped container a reboot surfaced, in a deployment nobody has
            # finished setting up. Supervision restarts what was accepted; it
            # does not put something into service for the first time.
            return Decision("blocked", "this deployment has not been accepted into service yet")

        if self.integrity is not None and not self.integrity():
            self._open_incident(
                state,
                "the installed release does not match what was accepted, so it was not restarted.",
            )
            return Decision("blocked", "integrity check failed")

        recent = self._recent(state, now)
        if len(recent) >= MAX_RESTARTS:
            self._open_incident(
                state,
                "the assistant has stopped repeatedly and restarting is not "
                f"recovering it. Next step: {OPERATOR_RECOVERY_STEP}",
            )
            return Decision("gave_up", "restarting is not recovering this")

        last = _moment(state.last_start)
        if last is not None and now - last < BACKOFF:
            return Decision("waiting", "waiting out the backoff before trying again")

        # Recorded before the attempt, not after. A crash in between used to
        # lose the fact that a restart had been tried at all, which is how a
        # bounded number of restarts becomes an unbounded one.
        stamps = [*(item.isoformat() for item in recent), now.isoformat()]
        self._write(
            SupervisorState(
                restarts=tuple(stamps),
                incident_open=state.incident_open,
                hold_reason="",
                last_start=now.isoformat(),
            )
        )
        started = self.container.start()
        if not started:
            return Decision("start_failed", "the container did not start")
        return Decision("started", "the container was not running and was started")

    def _health(self) -> tuple[str, str]:
        """What the deployment's own checks say, or the honest minimum."""

        if self.health is None:
            return HEALTHY, ""
        try:
            verdict, detail = self.health()
        except Exception as exc:  # noqa: BLE001 - a failed check is not a pass
            return UNKNOWN, f"the health check itself failed: {type(exc).__name__}"
        if verdict not in HEALTH_STATES:
            return UNKNOWN, "the health check answered something unrecognised"
        return verdict, detail

    def _open_incident(self, state: SupervisorState, message: str) -> None:
        if state.incident_open:
            return
        self.alert("incident", message)
        self._write(
            SupervisorState(
                restarts=state.restarts,
                incident_open=True,
                hold_reason=state.hold_reason,
                last_start=state.last_start,
            )
        )


def replace_hold(state: SupervisorState, reason: str) -> SupervisorState:
    return SupervisorState(
        restarts=state.restarts,
        incident_open=state.incident_open,
        hold_reason=reason,
        last_start=state.last_start,
    )


@dataclass(frozen=True, slots=True)
class DockerContainer:
    """The exact managed container, addressed by name and never by pattern."""

    name: str
    run: Callable[[Sequence[str]], tuple[int, str]]

    def _inspect(self, template: str) -> tuple[int, str]:
        return self.run(["docker", "inspect", "--format", template, self.name])

    def present(self) -> bool:
        status, _ = self._inspect("{{.Id}}")
        return status == 0

    def is_running(self) -> bool:
        status, output = self._inspect("{{.State.Running}}")
        return status == 0 and output.strip() == "true"

    def start(self) -> bool:
        status, _ = self.run(["docker", "start", self.name])
        return status == 0

    def stop(self) -> bool:
        status, _ = self.run(["docker", "stop", self.name])
        return status == 0


__all__ = [
    "BACKOFF",
    "MAX_RESTARTS",
    "OPERATOR_RECOVERY_STEP",
    "RESTART_WINDOW",
    "Container",
    "Decision",
    "DockerContainer",
    "Supervisor",
    "SupervisorState",
]
