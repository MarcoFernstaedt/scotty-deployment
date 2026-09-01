"""Deterministic supervision of the parts that must hold.

Reliability cannot depend on the model noticing something. These are the checks
that run whether or not anyone is talking to the assistant: exactly one process
consuming Discord, restarts that stop when they stop helping, and one alert to
Marco per incident rather than one per look.

Nothing here repairs anything by itself. It establishes what is true, decides
whether another restart is worth attempting, and hands the operator a fixed
next step when it is not.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

#: How long a consumer lease stays valid without renewal. Long enough that a
#: busy process does not lose it; short enough that a dead one frees it.
LEASE_SECONDS = 300

#: More restarts than this inside the window is a loop, not a recovery.
CRASH_LOOP_THRESHOLD = 5
CRASH_LOOP_WINDOW = timedelta(minutes=10)

#: An incident stays "already reported" until it recovers, so a provider that
#: is down for an hour produces one message rather than sixty.
MAX_TRACKED_INCIDENTS = 64

OPERATOR_RECOVERY_STEP = "sudo /usr/local/sbin/scotty-start"


class HealthState(StrEnum):
    """Fixed vocabulary. `unknown` is never reported as healthy."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    NOT_CONFIGURED = "not configured"


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Replace one small state file atomically, owner-only."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(json.dumps(dict(payload), sort_keys=True).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


class UnreadableState(RuntimeError):
    """A state file exists but cannot be read, so nothing is assumed about it."""


def _read_json(path: Path, *, strict: bool = False) -> dict[str, object]:
    """Read one small state file.

    `strict` is for state whose absence is permissive. A corrupt lease that read
    as "unheld" would hand a second consumer the singleton, so an unreadable
    file is raised rather than treated as empty.
    """

    if path.is_symlink() or not path.is_file():
        return {}
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if strict:
            raise UnreadableState(str(path.name)) from exc
        return {}
    if not isinstance(body, Mapping):
        if strict:
            raise UnreadableState(str(path.name))
        return {}
    return dict(body)


class ConsumerLease:
    """Exactly one process consumes Discord at a time.

    Two consumers on one bot token double every message and every effect, so
    this is the invariant a restart, a rollback, or a stray container must not
    break. The lease is time-bounded, so a process that dies without releasing
    it does not lock the deployment out forever.
    """

    def __init__(self, path: Path, *, ttl_seconds: int = LEASE_SECONDS):
        self.path = path
        self.ttl = timedelta(seconds=ttl_seconds)

    def _current(self) -> tuple[str, datetime | None]:
        body = _read_json(self.path, strict=True)
        holder = body.get("holder")
        renewed = body.get("renewed_at")
        if type(holder) is not str or type(renewed) is not str:
            return "", None
        try:
            return holder, datetime.fromisoformat(renewed)
        except ValueError:
            return "", None

    def claim(self, process_id: str, *, at: datetime | None = None) -> bool:
        """Take or renew the lease. False means someone else holds it."""

        moment = (at or datetime.now(UTC)).astimezone(UTC)
        try:
            holder, renewed = self._current()
        except UnreadableState:
            # An unreadable lease is not an unheld one, so it is not simply
            # taken. But refusing forever would need an operator to clear a
            # file, so a lease nobody has renewed for longer than its lifetime
            # is treated as abandoned: a live holder renews, which keeps its
            # modification time fresh.
            if not self._stale(moment):
                return False
            holder, renewed = "", None
        held_by_other = holder and holder != process_id and renewed is not None
        if held_by_other and moment - renewed < self.ttl:  # type: ignore[operator]
            return False
        _write_json(self.path, {"holder": process_id, "renewed_at": moment.isoformat()})
        return True

    def _stale(self, moment: datetime) -> bool:
        """Whether an unreadable lease has gone untouched past its lifetime."""

        try:
            touched = datetime.fromtimestamp(self.path.stat().st_mtime, tz=UTC)
        except OSError:
            return True
        return moment - touched >= self.ttl

    def holder(self) -> str:
        try:
            return self._current()[0]
        except UnreadableState:
            return "unreadable"

    def release(self, process_id: str) -> bool:
        """Give up the lease. Only its own holder may."""

        try:
            holder, _ = self._current()
        except UnreadableState:
            return False
        if holder != process_id:
            return False
        with suppress(OSError):
            self.path.unlink(missing_ok=True)
        return True


@dataclass(frozen=True, slots=True)
class RestartState:
    restarts: int
    crash_looping: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RestartDecision:
    allowed: bool
    reason: str = ""
    proposal: str = ""


class Supervisor:
    """Bounded restart supervision with crash-loop detection."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "restarts.json"

    def _recent(self, moment: datetime) -> list[datetime]:
        body = _read_json(self.path)
        recorded = body.get("restarts")
        stamps: list[datetime] = []
        for value in recorded if isinstance(recorded, list) else []:
            if type(value) is not str:
                continue
            try:
                stamp = datetime.fromisoformat(value)
            except ValueError:
                continue
            if moment - stamp < CRASH_LOOP_WINDOW:
                stamps.append(stamp)
        return stamps

    def record_restart(self, *, at: datetime | None = None) -> RestartState:
        moment = (at or datetime.now(UTC)).astimezone(UTC)
        stamps = [*self._recent(moment), moment]
        _write_json(self.path, {"restarts": [stamp.isoformat() for stamp in stamps]})
        looping = len(stamps) >= CRASH_LOOP_THRESHOLD
        return RestartState(
            restarts=len(stamps),
            crash_looping=looping,
            reason=(
                f"{len(stamps)} restart attempts inside "
                f"{int(CRASH_LOOP_WINDOW.total_seconds() // 60)} minutes"
                if looping
                else ""
            ),
        )

    def should_restart(self, *, at: datetime | None = None) -> RestartDecision:
        """Whether another restart is worth attempting.

        Past the threshold it is not: restarting into the same failure is not a
        recovery, so the supervisor stops and hands the operator a fixed step.
        """

        moment = (at or datetime.now(UTC)).astimezone(UTC)
        stamps = self._recent(moment)
        if len(stamps) >= CRASH_LOOP_THRESHOLD:
            return RestartDecision(
                allowed=False,
                reason="restarting is not recovering this",
                proposal=OPERATOR_RECOVERY_STEP,
            )
        return RestartDecision(allowed=True)


class IncidentLog:
    """One alert per material incident, and one when it recovers."""

    def __init__(self, path: Path):
        self.path = path

    def _state(self) -> dict[str, object]:
        body = _read_json(self.path)
        open_incidents = body.get("open")
        return {"open": dict(open_incidents) if isinstance(open_incidents, Mapping) else {}}

    def should_alert(self, incident: str, *, at: datetime | None = None) -> bool:
        """True the first time this incident is seen, and not again until it clears."""

        moment = (at or datetime.now(UTC)).astimezone(UTC)
        state = self._state()
        open_incidents = state["open"]
        assert isinstance(open_incidents, dict)  # noqa: S101 - shape held by _state
        if incident in open_incidents:
            return False
        if len(open_incidents) >= MAX_TRACKED_INCIDENTS:
            # Keep the newest; an unbounded list is its own failure mode.
            for key in sorted(open_incidents, key=lambda name: str(open_incidents[name]))[:1]:
                del open_incidents[key]
        open_incidents[incident] = moment.isoformat()
        _write_json(self.path, {"open": open_incidents})
        return True

    def should_alert_recovery(self, incident: str, *, at: datetime | None = None) -> bool:
        """True once, for an incident that was actually reported as open."""

        del at
        state = self._state()
        open_incidents = state["open"]
        assert isinstance(open_incidents, dict)  # noqa: S101 - shape held by _state
        if incident not in open_incidents:
            # Nobody was told it broke, so nobody needs telling it is fine.
            return False
        del open_incidents[incident]
        _write_json(self.path, {"open": open_incidents})
        return True

    def open_incidents(self) -> tuple[str, ...]:
        state = self._state()
        open_incidents = state["open"]
        assert isinstance(open_incidents, dict)  # noqa: S101 - shape held by _state
        return tuple(sorted(open_incidents))


__all__ = [
    "CRASH_LOOP_THRESHOLD",
    "CRASH_LOOP_WINDOW",
    "LEASE_SECONDS",
    "OPERATOR_RECOVERY_STEP",
    "ConsumerLease",
    "HealthState",
    "IncidentLog",
    "RestartDecision",
    "RestartState",
    "Supervisor",
    "UnreadableState",
]
